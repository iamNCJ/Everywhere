"""Codex session sweeper — runs from launchd every 5 minutes.

See docs/specs/2026-05-06-codex-support-design.md for design.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


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
