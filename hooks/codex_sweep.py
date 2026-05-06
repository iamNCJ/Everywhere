"""Codex session sweeper — runs from launchd every 5 minutes.

See docs/specs/2026-05-06-codex-support-design.md for design.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


DEBOUNCE_SECONDS = 600
FINALITY_IDLE_SECONDS = 600
MIN_USER_MESSAGES = 3
MAX_RETRY_COUNT = 3
SCAN_DAYS = 7


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
