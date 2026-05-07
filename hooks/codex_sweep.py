"""Codex session sweeper — runs from launchd every 5 minutes.

See docs/specs/2026-05-06-codex-support-design.md for design.
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from hooks.snapshot import (
    parse_codex_transcript,
    format_conversation,
    write_session_files,
    update_project_md,
    update_index_md,
    git_commit_push,
    first_sentence,
    project_slug,
)
from hooks.summarizer import summarize, DEFAULT_CODEX_MODEL


DEBOUNCE_SECONDS = 600
FINALITY_IDLE_SECONDS = 600
MIN_USER_MESSAGES = 3
MAX_RETRY_COUNT = 3
SCAN_DAYS = 7

CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"


def decide_action(mtime: float, now: float, cursor: dict | None) -> str:
    """Return one of {'skip', 'incremental', 'finalize'} for a single rollout file.

    Args:
        mtime: file mtime in unix seconds.
        now: current unix seconds.
        cursor: prior state for this session_id, or None if never seen.
    """
    if cursor and cursor.get("is_final"):
        return "skip"
    if (now - mtime) > FINALITY_IDLE_SECONDS:
        return "finalize"
    last = cursor.get("last_snapshot_at", 0) if cursor else 0
    if (now - last) > DEBOUNCE_SECONDS:
        return "incremental"
    return "skip"


def load_cursor(path: Path) -> dict:
    """Load the cursor JSON. Missing → {}. Corrupted → rename out of the way and return {}."""
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("cursor root must be an object")
        return data
    except (json.JSONDecodeError, ValueError, OSError):
        ts = int(time.time())
        try:
            path.rename(path.with_name(path.name + f".bad-{ts}"))
        except OSError:
            pass
        return {}


def save_cursor(path: Path, data: dict) -> None:
    """Atomic write: write to .tmp, rename into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _recent_date_dirs(root: Path, days: int) -> list[Path]:
    """Return YYYY/MM/DD directories under root from the last `days` days."""
    today = _dt.date.today()
    out = []
    for offset in range(days):
        d = today - _dt.timedelta(days=offset)
        candidate = root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}"
        if candidate.is_dir():
            out.append(candidate)
    return out


def _iter_rollouts(root: Path, days: int):
    for date_dir in _recent_date_dirs(root, days):
        for f in date_dir.glob("rollout-*.jsonl"):
            if f.is_file():
                yield f


def _log(msg: str) -> None:
    print(f"[codex-sweep] {msg}", flush=True)


def _err(msg: str) -> None:
    print(f"[codex-sweep] ERROR: {msg}", file=sys.stderr, flush=True)


def run_sweep(memory_repo: Path) -> int:
    """Main sweep entrypoint. Returns process exit code (always 0 in practice)."""
    snapshots_dir = memory_repo / ".snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    lock_path = snapshots_dir / ".codex-sweep.lock"
    cursor_path = snapshots_dir / ".codex-cursor.json"

    with open(lock_path, "w") as lockf:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _log("another sweep already running; exiting")
            return 0

        if not (memory_repo / ".git").exists():
            _err(f"memory repo not initialized at {memory_repo}; run /everywhere-setup")
            return 0

        if not CODEX_SESSIONS_ROOT.exists():
            _log(f"no Codex sessions directory at {CODEX_SESSIONS_ROOT}")
            return 0

        cursor = load_cursor(cursor_path)
        now = time.time()
        seen = 0
        acted = 0
        for rollout in _iter_rollouts(CODEX_SESSIONS_ROOT, SCAN_DAYS):
            seen += 1
            try:
                handled = _handle_rollout(rollout, cursor, now, memory_repo)
                if handled:
                    acted += 1
                    save_cursor(cursor_path, cursor)
            except Exception as e:
                _err(f"{rollout.name}: {e}")

        _log(f"sweep complete: scanned={seen} acted={acted}")
        return 0


def _handle_rollout(rollout: Path, cursor: dict, now: float, memory_repo: Path) -> bool:
    mtime = rollout.stat().st_mtime
    session_id, cwd, started_at, messages = parse_codex_transcript(rollout)
    if session_id is None:
        return False

    action = decide_action(mtime, now, cursor.get(session_id))
    _log(f"{session_id[:8]} mtime_age={int(now-mtime)}s action={action}")
    if action == "skip":
        return False

    user_count = sum(1 for m in messages if m["role"] == "user")
    if user_count < MIN_USER_MESSAGES:
        _log(f"{session_id[:8]} only {user_count} user messages; skip")
        return False

    is_final = action == "finalize"

    # Summarize.
    try:
        conversation = format_conversation(messages)
        model = os.environ.get("EVERYWHERE_CODEX_MODEL", DEFAULT_CODEX_MODEL)
        summary_obj = summarize(conversation, agent="codex", model=model)
    except Exception as e:
        prior = cursor.get(session_id, {})
        retries = int(prior.get("retry_count", 0)) + 1
        _err(f"{session_id[:8]} summarization failed (retry {retries}): {e}")
        if retries >= MAX_RETRY_COUNT and is_final:
            _log(f"{session_id[:8]} max retries reached; finalizing with stub summary")
            summary_obj = _stub_summary(session_id, len(messages), user_count)
        else:
            cursor[session_id] = {
                "last_mtime": mtime,
                "last_snapshot_at": prior.get("last_snapshot_at", 0),
                "is_final": False,
                "retry_count": retries,
            }
            return True

    required = {"summary", "decisions", "artifacts", "excerpts", "tags"}
    missing = required - set(summary_obj.keys())
    if missing:
        _err(f"{session_id[:8]} summary missing keys: {missing}")
        return False

    project_name = project_slug(cwd)
    date_str = (started_at or datetime.now(timezone.utc).isoformat())[:10]
    session_short = session_id[:6]
    session_dir = (
        memory_repo / "projects" / project_name / "sessions" / f"{date_str}-{session_short}"
    )

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
        "agent": "codex",
        "started_at": started_at or now_iso,
        "is_final": is_final,
        "snapshot_count": snapshot_count,
        "tags": summary_obj.get("tags", []),
    }
    if is_final:
        meta["ended_at"] = now_iso

    write_session_files(session_dir, meta, summary_obj)
    update_project_md(
        memory_repo / "projects" / project_name,
        project_name, cwd, date_str, session_short, summary_obj["summary"],
    )
    update_index_md(project_name, cwd)
    _log(f"{session_id[:8]} wrote {session_dir}")

    cursor[session_id] = {
        "last_mtime": mtime,
        "last_snapshot_at": now,
        "is_final": is_final,
        "retry_count": 0,
    }

    if is_final:
        headline = first_sentence(summary_obj["summary"])
        git_commit_push(project_name, session_short, headline)

    return True


def _stub_summary(session_id: str, total_msgs: int, user_msgs: int) -> dict:
    return {
        "summary": (
            f"Codex session {session_id[:8]} captured with summarizer failures. "
            f"{user_msgs} user messages, {total_msgs} total. "
            f"Stub finalization after {MAX_RETRY_COUNT} retry attempts."
        ),
        "decisions": "- Stub summary: summarizer repeatedly failed; full content not extracted.",
        "artifacts": "## Files\n\n- (stub — not extracted)",
        "excerpts": "## Exchange 1: stub\n\n**User:** (stub)\n\n**Assistant:** (stub)",
        "tags": ["stub", "summarizer-failed", "codex"],
    }
