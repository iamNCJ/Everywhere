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
from pathlib import Path


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
    """Process one rollout file. Returns True if any state changed."""
    # Stub for Task 7 — real work added in Task 8.
    mtime = rollout.stat().st_mtime
    # We need the session_id to key the cursor. Read just the first line.
    try:
        with open(rollout) as f:
            first = f.readline()
        meta = json.loads(first)
        if meta.get("type") != "session_meta":
            return False
        session_id = meta.get("payload", {}).get("id")
        if not session_id:
            return False
    except (json.JSONDecodeError, OSError):
        return False

    action = decide_action(mtime, now, cursor.get(session_id))
    _log(f"{session_id[:8]} mtime_age={int(now-mtime)}s action={action}")
    if action == "skip":
        return False
    # Real per-action logic in Task 8.
    return False
