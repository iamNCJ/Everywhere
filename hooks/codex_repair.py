"""Repair and backfill helpers for Codex session memory.

This module is intentionally separate from the live hook path: repair scans
historical rollout files and writes missing sessions regardless of old cursor
state, while the hook path remains debounce/finality driven.
"""
from __future__ import annotations

import re
import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from hooks.codex_sweep import MIN_USER_MESSAGES, load_cursor, save_cursor
from hooks.snapshot import (
    INDEX_TEMPLATE,
    first_sentence,
    format_conversation,
    parse_codex_transcript,
    project_slug,
    session_short_id,
    update_project_md,
    write_session_files,
)
from hooks.summarizer import summarize


@dataclass(frozen=True)
class CodexBackfillCandidate:
    rollout: Path
    session_id: str
    cwd: str
    started_at: str
    messages: list[dict]
    user_count: int
    mtime: float

    @property
    def date_str(self) -> str:
        return (self.started_at or datetime.now(timezone.utc).isoformat())[:10]

    @property
    def project_name(self) -> str:
        return project_slug(self.cwd)

    @property
    def session_short(self) -> str:
        return session_short_id(self.session_id)

    def target_dir(self, memory_repo: Path) -> Path:
        return (
            memory_repo
            / "projects"
            / self.project_name
            / "sessions"
            / f"{self.date_str}-{self.session_short}"
        )


def collect_existing_session_ids(
    memory_repo: Path,
    *,
    include_stub_summaries: bool = False,
) -> set[str]:
    out: set[str] = set()
    for meta in (memory_repo / "projects").glob("*/sessions/*/meta.yaml"):
        session_dir = meta.parent
        if not _session_record_complete(session_dir):
            continue
        text = meta.read_text(errors="replace")
        match = re.search(
            r"^session_id:\s*[\"']?([^\"'\n]+)",
            text,
            re.MULTILINE,
        )
        if match:
            session_id = match.group(1).strip()
            if include_stub_summaries and "stub-summary" in text:
                continue
            out.add(session_id)
    return out


def _session_record_complete(session_dir: Path) -> bool:
    return all(
        (session_dir / name).exists()
        for name in ("meta.yaml", "summary.md", "decisions.md", "artifacts.md", "excerpts.md")
    )


def collect_missing_codex_rollouts(
    memory_repo: Path,
    sessions_root: Path,
    *,
    min_user_messages: int = MIN_USER_MESSAGES,
    include_stub_summaries: bool = False,
) -> list[CodexBackfillCandidate]:
    existing = collect_existing_session_ids(
        memory_repo,
        include_stub_summaries=include_stub_summaries,
    )
    candidates: list[CodexBackfillCandidate] = []
    for rollout in sorted(sessions_root.rglob("rollout-*.jsonl"), key=lambda p: str(p)):
        session_id, cwd, started_at, messages = parse_codex_transcript(rollout)
        if not session_id or not cwd:
            continue
        if session_id in existing:
            continue
        user_count = sum(1 for m in messages if m.get("role") == "user")
        if user_count < min_user_messages:
            continue
        candidates.append(
            CodexBackfillCandidate(
                rollout=rollout,
                session_id=session_id,
                cwd=cwd,
                started_at=started_at or "",
                messages=messages,
                user_count=user_count,
                mtime=rollout.stat().st_mtime,
            )
        )
    candidates.sort(key=lambda c: (c.started_at, c.session_id))
    return candidates


def update_index_md_for_repo(memory_repo: Path, project_name: str, project_path: str) -> None:
    index = memory_repo / "INDEX.md"
    if not index.exists():
        index.write_text(INDEX_TEMPLATE)
    text = index.read_text()
    line = f"- [{project_name}](projects/{project_name}/PROJECT.md) — {project_path}"
    if line in text:
        return
    if not text.endswith("\n"):
        text += "\n"
    index.write_text(text + line + "\n")


def backfill_candidate(
    candidate: CodexBackfillCandidate,
    memory_repo: Path,
    cursor: dict,
    *,
    summarize_fn: Callable[..., dict] = summarize,
    summarizer_agent: str = "codex",
    model: str | None = None,
    now: float | None = None,
    stub_on_failure: bool = True,
) -> Path:
    now = time.time() if now is None else now
    try:
        summary_obj = summarize_fn(
            format_conversation(candidate.messages),
            agent=summarizer_agent,
            model=model,
        )
    except Exception as e:
        if not stub_on_failure:
            raise
        summary_obj = _stub_summary(candidate, e)
    summary_obj = normalize_summary(summary_obj, candidate)

    session_dir = candidate.target_dir(memory_repo)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {
        "session_id": candidate.session_id,
        "session_id_short": candidate.session_short,
        "project_path": candidate.cwd,
        "project_name": candidate.project_name,
        "agent": "codex",
        "started_at": candidate.started_at or now_iso,
        "is_final": True,
        "snapshot_count": 0,
        "tags": summary_obj.get("tags", []),
        "ended_at": now_iso,
    }

    write_session_files(session_dir, meta, summary_obj)
    update_project_md(
        memory_repo / "projects" / candidate.project_name,
        candidate.project_name,
        candidate.cwd,
        candidate.date_str,
        candidate.session_short,
        summary_obj["summary"],
    )
    update_index_md_for_repo(memory_repo, candidate.project_name, candidate.cwd)

    cursor[candidate.session_id] = {
        "last_mtime": candidate.mtime,
        "last_snapshot_at": now,
        "is_final": True,
        "retry_count": 0,
    }
    save_cursor(memory_repo / ".snapshots" / ".codex-cursor.json", cursor)
    return session_dir


def normalize_summary(summary_obj: dict, candidate: CodexBackfillCandidate) -> dict:
    if not isinstance(summary_obj, dict):
        raise TypeError(f"summary must be an object for {candidate.session_id}")
    return {
        "summary": _summary_text(summary_obj.get("summary"), candidate),
        "decisions": _markdown_text(summary_obj.get("decisions"), "No significant decisions recorded."),
        "artifacts": _markdown_text(summary_obj.get("artifacts"), "## References\n\n- No artifacts recorded."),
        "excerpts": _markdown_text(summary_obj.get("excerpts"), "## Exchange 1: unavailable\n\n**User:** (not extracted)\n\n**Assistant:** (not extracted)"),
        "tags": _tags(summary_obj.get("tags")),
    }


def _summary_text(value, candidate: CodexBackfillCandidate) -> str:
    text = _markdown_text(value, "")
    if text:
        return text
    return f"Codex session {candidate.session_short} was backfilled from historical rollout logs."


def _markdown_text(value, fallback: str) -> str:
    if isinstance(value, str):
        return value.strip() or fallback
    if value is None:
        return fallback
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        text = "\n".join(f"- {item}" for item in value).strip()
        return text or fallback
    return json.dumps(value, ensure_ascii=False, indent=2)


def _tags(value) -> list[str]:
    if isinstance(value, list):
        tags = [str(item).strip().lower() for item in value if str(item).strip()]
        if tags:
            return tags[:8]
    return ["codex", "backfill"]


def commit_memory_repo(memory_repo: Path, message: str, *, push: bool = True) -> str:
    subprocess.run(["git", "-C", str(memory_repo), "add", "-A"], check=True)
    diff = subprocess.run(["git", "-C", str(memory_repo), "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        return "no changes"
    subprocess.run(["git", "-C", str(memory_repo), "commit", "-m", message], check=True)
    if push:
        subprocess.run(
            ["git", "-C", str(memory_repo), "pull", "--rebase", "--autostash", "origin", "main"],
            check=True,
            timeout=60,
        )
        subprocess.run(["git", "-C", str(memory_repo), "push", "origin", "main"], check=True, timeout=60)
    return "committed"


def _stub_summary(candidate: CodexBackfillCandidate, error: Exception) -> dict:
    return {
        "summary": (
            f"Codex session {candidate.session_short} was backfilled with a stub "
            f"summary because summarization failed: {type(error).__name__}."
        ),
        "decisions": "- Stub summary: summarization failed during historical backfill.",
        "artifacts": (
            "## References\n\n"
            f"- Rollout: `{candidate.rollout}`\n"
            f"- Error: `{type(error).__name__}: {str(error)[:300]}`"
        ),
        "excerpts": "## Exchange 1: unavailable\n\n**User:** (stub)\n\n**Assistant:** (stub)",
        "tags": ["codex", "backfill", "stub-summary"],
    }
