#!/usr/bin/env python3
"""Codex CLI hook entry point.

Two subcommands:
  stop            — fired by Codex Stop hook (end of every turn). Reads hook
                    payload from stdin, takes a debounced incremental snapshot
                    of the active rollout.
  finalize-sweep  — fired by Codex SessionStart hook (startup/resume). Walks
                    recent rollouts and finalizes any idle for FINALITY_IDLE_SECONDS.

Designed to fail silently — errors go to stderr and exit code is always 0
so a misbehaving hook never blocks the user.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path

from hooks.codex_sweep import (
    _handle_rollout,
    _iter_rollouts,
    load_cursor,
    save_cursor,
    CODEX_SESSIONS_ROOT,
    SCAN_DAYS,
)

MEMORY_REPO = Path.home() / "agent-memory"

DEBUG = os.environ.get("EVERYWHERE_DEBUG", "0") == "1"


def log(msg: str) -> None:
    if DEBUG:
        print(f"[codex-hook] {msg}", file=sys.stderr, flush=True)


def err(msg: str) -> None:
    print(f"[codex-hook] ERROR: {msg}", file=sys.stderr, flush=True)


def run_stop(payload: dict) -> None:
    session_id = payload.get("session_id") or ""
    transcript_path = payload.get("transcript_path") or ""
    cwd = payload.get("cwd") or ""

    if not session_id or not transcript_path or not cwd:
        err(f"missing fields: session_id={bool(session_id)} "
            f"transcript_path={bool(transcript_path)} cwd={bool(cwd)}")
        return

    if not (MEMORY_REPO / ".git").exists():
        err(f"memory repo not initialized at {MEMORY_REPO}; run /everywhere-setup")
        return

    rollout = Path(transcript_path)
    if not rollout.exists():
        err(f"transcript not found: {rollout}")
        return

    snapshots_dir = MEMORY_REPO / ".snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    lock_path = snapshots_dir / ".codex-sweep.lock"
    cursor_path = snapshots_dir / ".codex-cursor.json"

    with open(lock_path, "w") as lockf:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another codex-hook holds the lock; exiting")
            return

        cursor = load_cursor(cursor_path)
        try:
            handled = _handle_rollout(
                rollout, cursor, time.time(), MEMORY_REPO,
                allow_incremental=True, allow_finalize=False,
            )
            if handled:
                save_cursor(cursor_path, cursor)
        except Exception as e:
            err(f"stop handling failed: {e}")


def run_finalize_sweep() -> None:
    if not (MEMORY_REPO / ".git").exists():
        err(f"memory repo not initialized at {MEMORY_REPO}; run /everywhere-setup")
        return
    if not CODEX_SESSIONS_ROOT.exists():
        log(f"no Codex sessions directory at {CODEX_SESSIONS_ROOT}")
        return

    snapshots_dir = MEMORY_REPO / ".snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    lock_path = snapshots_dir / ".codex-sweep.lock"
    cursor_path = snapshots_dir / ".codex-cursor.json"

    with open(lock_path, "w") as lockf:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another sweep already running; exiting")
            return

        cursor = load_cursor(cursor_path)
        now = time.time()
        seen = 0
        acted = 0
        for rollout in _iter_rollouts(CODEX_SESSIONS_ROOT, SCAN_DAYS):
            seen += 1
            try:
                handled = _handle_rollout(
                    rollout, cursor, now, MEMORY_REPO,
                    allow_incremental=False, allow_finalize=True,
                )
                if handled:
                    acted += 1
                    save_cursor(cursor_path, cursor)
            except Exception as e:
                err(f"{rollout.name}: {e}")
        log(f"finalize-sweep complete: scanned={seen} acted={acted}")


HOOK_MARKER = "/.everywhere/hooks/codex_hook.py"


def _our_stop_entry(staged_dir: str) -> dict:
    return {
        "hooks": [
            {
                "type": "command",
                "command": (
                    f"nohup python3 -u {staged_dir}/codex_hook.py stop "
                    f">/dev/null 2>&1 &"
                ),
                "timeout": 5,
            }
        ]
    }


def _our_sessionstart_entry(staged_dir: str) -> dict:
    return {
        "matcher": "startup|resume",
        "hooks": [
            {
                "type": "command",
                "command": (
                    f"nohup python3 -u {staged_dir}/codex_hook.py finalize-sweep "
                    f">/dev/null 2>&1 &"
                ),
                "timeout": 5,
            }
        ]
    }


def _entry_is_ours(entry: dict) -> bool:
    for h in entry.get("hooks", []):
        if HOOK_MARKER in h.get("command", ""):
            return True
    return False


def install_hooks_json(hooks_path: Path, staged_dir: str) -> None:
    """Merge our Stop and SessionStart entries into hooks_path. Idempotent."""
    if hooks_path.exists():
        try:
            data = json.loads(hooks_path.read_text())
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("hooks", {})

    for event, builder in (
        ("Stop", _our_stop_entry),
        ("SessionStart", _our_sessionstart_entry),
    ):
        existing = data["hooks"].get(event, [])
        if not isinstance(existing, list):
            existing = []
        kept = [e for e in existing if isinstance(e, dict) and not _entry_is_ours(e)]
        kept.append(builder(staged_dir))
        data["hooks"][event] = kept

    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(data, indent=2) + "\n")


def uninstall_hooks_json(hooks_path: Path) -> None:
    """Remove our entries from hooks_path. Drop empty events; delete file if empty."""
    if not hooks_path.exists():
        return
    try:
        data = json.loads(hooks_path.read_text())
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict) or "hooks" not in data:
        return

    for event in list(data["hooks"].keys()):
        value = data["hooks"][event]
        if not isinstance(value, list):
            continue
        kept = [e for e in value if isinstance(e, dict) and not _entry_is_ours(e)]
        if kept:
            data["hooks"][event] = kept
        else:
            del data["hooks"][event]

    if not data["hooks"]:
        hooks_path.unlink()
        return

    hooks_path.write_text(json.dumps(data, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stop")
    sub.add_parser("finalize-sweep")
    args = parser.parse_args()

    if args.cmd == "finalize-sweep":
        run_finalize_sweep()
        return

    if args.cmd == "stop":
        raw = sys.stdin.read()
        if not raw.strip():
            log("stop: empty stdin; nothing to do")
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            err(f"stop: stdin is not JSON: {e}")
            return
        run_stop(payload)


if __name__ == "__main__":
    try:
        main()
    except BaseException as e:
        # Fail-silent: a misbehaving hook must never block the user.
        # BaseException (not Exception) so argparse's SystemExit on bad
        # invocations doesn't surface as a non-zero hook exit.
        err(f"unhandled: {e}")
    sys.exit(0)
