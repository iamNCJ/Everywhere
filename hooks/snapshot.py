#!/usr/bin/env python3
"""Everywhere session snapshot hook.

Runs as Stop or SessionEnd hook. Reads hook payload from stdin,
generates snapshot files via `claude -p` (Haiku), and on SessionEnd
commits + pushes the memory repo.

Designed to fail silently — errors go to stderr and exit code is always 0
so a misbehaving hook never blocks the user.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from hooks.summarizer import summarize

MEMORY_REPO = Path.home() / "agent-memory"
SNAPSHOTS_DIR = MEMORY_REPO / ".snapshots"
DEBOUNCE_SECONDS = 600
MIN_USER_MESSAGES = 3
DEFAULT_MODEL = "claude-haiku-4-5"
PROJECT_ENTRY_CAP = 10
MAX_CONVERSATION_CHARS = 60000

HOOK_SESSION_END = "SessionEnd"

DEBUG = os.environ.get("EVERYWHERE_DEBUG", "0") == "1"


def log(msg: str) -> None:
    if DEBUG:
        print(f"[everywhere] {msg}", file=sys.stderr, flush=True)


def err(msg: str) -> None:
    print(f"[everywhere] ERROR: {msg}", file=sys.stderr, flush=True)


def project_slug(cwd: str) -> str:
    """Stable project identifier for the memory repo: ``{basename}-{hash8}``.

    The 8-char hash of the absolute path disambiguates folders that share a
    basename across the filesystem (e.g. ``~/work/foo`` vs ``~/personal/foo``).
    """
    abs_path = os.path.abspath(cwd) if cwd else os.path.abspath(".")
    basename = os.path.basename(abs_path) or "unknown"
    h = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:8]
    return f"{basename}-{h}"


def session_short_id(session_id: str) -> str:
    """Collision-resistant short id used in session directory names.

    Codex session ids are UUIDv7-like and often share the first 6-8
    characters when created close together. Strip separators and keep 12
    alphanumeric chars so paths stay compact without overwriting sessions.
    """
    compact = re.sub(r"[^A-Za-z0-9]", "", session_id or "")
    return (compact or "unknown")[:12]


# ---------- transcript parsing ----------

def parse_transcript(path: Path):
    """Return (started_at_iso_or_None, messages_list)."""
    started_at = None
    messages = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if started_at is None:
                ts = entry.get("timestamp")
                if isinstance(ts, str) and ts:
                    started_at = ts
            t = entry.get("type")
            msg = entry.get("message", {}) or {}
            if t == "user":
                content = msg.get("content", "")
                text = _extract_text(content)
                if text:
                    messages.append({"role": "user", "text": text})
            elif t == "assistant":
                content = msg.get("content", [])
                text = _extract_text(content)
                if text:
                    messages.append({"role": "assistant", "text": text})
    return started_at, messages


def parse_codex_transcript(path: Path):
    """Parse a Codex CLI rollout JSONL.

    Returns (session_id, cwd, started_at, messages) or (None, None, None, [])
    if the file lacks a session_meta header.
    """
    session_id = cwd = started_at = None
    messages = []
    with open(path) as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = entry.get("type")
            payload = entry.get("payload") or {}
            if i == 0 and t == "session_meta":
                session_id = payload.get("id")
                cwd = payload.get("cwd")
                started_at = payload.get("timestamp")
                continue
            if t != "response_item":
                continue
            if payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue
            text = " ".join(
                c.get("text", "")
                for c in (payload.get("content") or [])
                if isinstance(c, dict)
                and c.get("type") in ("input_text", "output_text")
            ).strip()
            if not text:
                continue
            if role == "user" and _is_slash_stub(text):
                continue
            messages.append({"role": role, "text": text})
    if session_id is None:
        return None, None, None, []
    return session_id, cwd, started_at, messages


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return " ".join(parts).strip()
    return ""


_SLASH_STUB_RE = re.compile(r"^\s*<(?:command-name|local-command-stdout)\b")


def _is_slash_stub(text: str) -> bool:
    return bool(_SLASH_STUB_RE.match(text))


def format_conversation(messages) -> str:
    # Cap from the tail — recent context is what matters for the headline.
    kept, total, truncated = [], 0, False
    for m in reversed(messages):
        prefix = "USER" if m["role"] == "user" else "ASSISTANT"
        chunk = f"--- {prefix} ---\n{m['text']}"
        if total + len(chunk) > MAX_CONVERSATION_CHARS:
            truncated = True
            break
        kept.append(chunk)
        total += len(chunk)
    kept.reverse()
    if truncated:
        kept.insert(0, "[...earlier conversation truncated for length...]")
    return "\n\n".join(kept)


# ---------- writers ----------

def yaml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in v) + "]"
    if isinstance(v, (int, float)):
        return str(v)
    if v is None:
        return "null"
    return json.dumps(v, ensure_ascii=False)


def write_session_files(session_dir: Path, meta: dict, summary: dict) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    yaml_lines = [f"{k}: {yaml_value(v)}" for k, v in meta.items()]
    (session_dir / "meta.yaml").write_text("\n".join(yaml_lines) + "\n")
    (session_dir / "summary.md").write_text(summary["summary"].strip() + "\n")
    (session_dir / "decisions.md").write_text(summary["decisions"].strip() + "\n")
    (session_dir / "artifacts.md").write_text(summary["artifacts"].strip() + "\n")
    (session_dir / "excerpts.md").write_text(summary["excerpts"].strip() + "\n")


PROJECT_TEMPLATE = """\
# {project_name}

**Path:** {project_path}

## Recent Sessions

<!-- Sessions listed below, newest first. Maintained by Everywhere. -->

## Project Overview

<!-- Updated manually or by /recall when patterns emerge -->
"""


def first_sentence(text: str) -> str:
    s = text.strip()
    parts = re.split(r"(?<=[.!?。！？])\s", s, maxsplit=1)
    return parts[0].strip() if parts else s


def update_project_md(
    project_dir: Path,
    project_name: str,
    project_path: str,
    date_str: str,
    session_short: str,
    summary_text: str,
) -> None:
    project_md = project_dir / "PROJECT.md"
    if not project_md.exists():
        project_md.parent.mkdir(parents=True, exist_ok=True)
        project_md.write_text(
            PROJECT_TEMPLATE.format(project_name=project_name, project_path=project_path)
        )
    text = project_md.read_text()
    new_entry = (
        f"- [{date_str} — {first_sentence(summary_text)}]"
        f"(sessions/{date_str}-{session_short}/summary.md)"
    )
    dup_re = re.compile(
        rf"^- \[.*\]\(sessions/[\d-]+-{re.escape(session_short)}/summary\.md\)$"
    )

    lines = text.split("\n")
    out, entries, in_recent, flushed = [], [], False, False

    def flush():
        kept = [e for e in entries if not dup_re.match(e)]
        kept = [new_entry] + kept
        kept = kept[:PROJECT_ENTRY_CAP]
        out.extend(kept)

    for line in lines:
        if line.strip() == "## Recent Sessions":
            in_recent = True
            out.append(line)
            continue
        if in_recent and line.startswith("## "):
            flush()
            flushed = True
            out.append("")
            out.append(line)
            in_recent = False
            continue
        if in_recent:
            if line.startswith("- "):
                entries.append(line)
                continue
            if line.startswith("<!--"):
                out.append(line)
                continue
            continue
        out.append(line)
    if in_recent and not flushed:
        flush()

    project_md.write_text("\n".join(out).rstrip() + "\n")


INDEX_TEMPLATE = """\
# Everywhere — Global Memory Index

> Auto-maintained by Everywhere. Last updated by SessionEnd hook.

## Projects

"""


def update_index_md(project_name: str, project_path: str) -> None:
    index = MEMORY_REPO / "INDEX.md"
    if not index.exists():
        index.write_text(INDEX_TEMPLATE)
    text = index.read_text()
    line = f"- [{project_name}](projects/{project_name}/PROJECT.md) — {project_path}"
    if line in text:
        return
    if not text.endswith("\n"):
        text += "\n"
    text += line + "\n"
    index.write_text(text)


def git_commit_push(project_name: str, session_short: str, summary_first: str) -> None:
    headline = summary_first[:120]
    msg = f"session: {project_name} {session_short} - {headline}"
    try:
        subprocess.run(
            ["git", "-C", str(MEMORY_REPO), "add", "-A"],
            check=True, capture_output=True,
        )
        diff = subprocess.run(
            ["git", "-C", str(MEMORY_REPO), "diff", "--cached", "--quiet"],
            capture_output=True,
        )
        if diff.returncode == 0:
            log("nothing staged; skipping commit")
            return
        subprocess.run(
            ["git", "-C", str(MEMORY_REPO), "commit", "-m", msg],
            check=True, capture_output=True,
        )
        log(f"committed: {msg}")
    except subprocess.CalledProcessError as e:
        err(f"git commit failed: {(e.stderr or b'').decode()[:300]}")
        return
    has_remote = subprocess.run(
        ["git", "-C", str(MEMORY_REPO), "remote"],
        capture_output=True, text=True,
    ).stdout.strip()
    if not has_remote:
        log("no git remote configured; skipping push")
        return
    try:
        subprocess.run(
            ["git", "-C", str(MEMORY_REPO),
             "pull", "--rebase", "--autostash", "origin", "main"],
            check=True, capture_output=True, timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        stderr = (getattr(e, "stderr", b"") or b"").decode()[:300]
        err(f"pull --rebase failed (commit kept locally, will retry next session): "
            f"{stderr or e}")
        return
    try:
        subprocess.run(
            ["git", "-C", str(MEMORY_REPO), "push", "origin", "main"],
            check=True, capture_output=True, timeout=20,
        )
        log("pushed to remote")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        stderr = (getattr(e, "stderr", b"") or b"").decode()[:300]
        err(f"push failed (commit kept locally): {stderr or e}")


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip claude -p; use a stub summary (for testing)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    raw = sys.stdin.read()
    if not raw.strip():
        log("empty stdin; nothing to do")
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        err(f"stdin is not JSON: {e}")
        return

    session_id = payload.get("session_id") or ""
    transcript_path = payload.get("transcript_path") or ""
    cwd = payload.get("cwd") or ""
    event = payload.get("hook_event_name") or ""

    if not session_id or not transcript_path or not cwd:
        err(f"missing fields: session_id={bool(session_id)} "
            f"transcript_path={bool(transcript_path)} cwd={bool(cwd)}")
        return

    is_final = event == HOOK_SESSION_END
    log(f"event={event} session={session_id[:8]} cwd={cwd}")

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    debounce_file = SNAPSHOTS_DIR / f".last-{session_id}"
    if not is_final:
        try:
            age = time.time() - debounce_file.stat().st_mtime
            if age < DEBOUNCE_SECONDS:
                log(f"debounced ({int(age)}s < {DEBOUNCE_SECONDS}s)")
                return
        except FileNotFoundError:
            pass

    if not (MEMORY_REPO / ".git").exists():
        err(f"memory repo not initialized at {MEMORY_REPO}; run /everywhere-setup")
        return

    transcript = Path(transcript_path)
    try:
        started_at, messages = parse_transcript(transcript)
    except FileNotFoundError:
        err(f"transcript not found: {transcript}")
        return
    user_count = sum(1 for m in messages if m["role"] == "user")
    if user_count < MIN_USER_MESSAGES:
        log(f"only {user_count} user messages; skip (need >= {MIN_USER_MESSAGES})")
        return

    project_name = project_slug(cwd)
    date_str = (started_at or datetime.now(timezone.utc).isoformat())[:10]
    session_short = session_short_id(session_id)
    session_dir = (
        MEMORY_REPO / "projects" / project_name / "sessions" / f"{date_str}-{session_short}"
    )

    if args.dry_run:
        log("DRY RUN — using stub summary")
        summary_obj = {
            "summary": (
                f"Stub summary for session {session_short} in project {project_name}. "
                f"This run captured {user_count} user messages and "
                f"{sum(1 for m in messages if m['role']=='assistant')} assistant messages. "
                f"Generated by --dry-run mode for snapshot.py testing. "
                f"Real summarization is bypassed."
            ),
            "decisions": "- Dry run mode: no decisions extracted.",
            "artifacts": "## Files\n\n- (dry run — not extracted)",
            "excerpts": (
                "## Exchange 1: stub\n\n"
                "**User:** (dry run)\n\n"
                "**Assistant:** (dry run)"
            ),
            "tags": ["dry-run", "test", project_name],
        }
    else:
        try:
            conversation = format_conversation(messages)
            summary_obj = summarize(conversation, agent="claude-code", model=args.model)
        except Exception as e:
            err(f"summarization failed: {e}")
            return

    required = {"summary", "decisions", "artifacts", "excerpts", "tags"}
    missing = required - set(summary_obj.keys())
    if missing:
        err(f"summary JSON missing keys: {missing}; got: {list(summary_obj.keys())}")
        return

    snapshot_count = 0
    if is_final:
        try:
            for line in (session_dir / "meta.yaml").read_text().splitlines():
                if line.startswith("is_final:") and line.split(":", 1)[1].strip() == "false":
                    snapshot_count = 1
                    break
        except FileNotFoundError:
            pass

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {
        "session_id": session_id,
        "session_id_short": session_short,
        "project_path": cwd,
        "project_name": project_name,
        "agent": "claude-code",
        "started_at": started_at or now_iso,
        "is_final": is_final,
        "snapshot_count": snapshot_count,
        "tags": summary_obj.get("tags", []),
    }
    if is_final:
        meta["ended_at"] = now_iso

    write_session_files(session_dir, meta, summary_obj)
    update_project_md(
        MEMORY_REPO / "projects" / project_name,
        project_name, cwd, date_str, session_short, summary_obj["summary"],
    )
    update_index_md(project_name, cwd)
    log(f"wrote {session_dir}")

    # Push on every snapshot (Stop *and* SessionEnd), not just session end.
    # In an ephemeral pod that can be hard-killed (k8s BackoffLimitExceeded,
    # node drain, OOM), SessionEnd frequently never fires — deferring the push
    # to it loses the whole session. async:true keeps this off the user's
    # critical path so it never blocks the next prompt.
    headline = first_sentence(summary_obj["summary"])
    git_commit_push(project_name, session_short, headline)
    if is_final:
        debounce_file.unlink(missing_ok=True)
    else:
        try:
            debounce_file.touch()
        except OSError as e:
            err(f"debounce touch failed: {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        err(f"unhandled: {e}")
        sys.exit(0)
