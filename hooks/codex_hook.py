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
import hashlib
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


def _detached_command(staged_dir: str, subcommand: str) -> str:
    """Shell snippet that reads stdin synchronously to a tempfile, then runs
    codex_hook.py detached in a background subshell.

    Codex backgrounding via plain `nohup ... &` drops stdin (FD 0 is severed
    when the parent shell forks the background process), so we must capture
    the JSON payload before detaching. The tempfile is removed after the
    subprocess exits.
    """
    script = f"{staged_dir}/codex_hook.py"
    return (
        "T=$(mktemp); cat > \"$T\"; "
        f"(python3 -u \"{script}\" {subcommand} < \"$T\" >/dev/null 2>&1; "
        "rm -f \"$T\") &"
    )


def _our_stop_entry(staged_dir: str) -> dict:
    return {
        "hooks": [
            {
                "type": "command",
                "command": _detached_command(staged_dir, "stop"),
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
                "command": _detached_command(staged_dir, "finalize-sweep"),
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


# ---------- Codex config.toml [hooks.state] pre-trust ----------
#
# Codex prompts the user (TUI) to "trust" each newly-registered hook before
# running it, then records the approval in [hooks.state] blocks in
# ~/.codex/config.toml as sha256(command_string). We pre-write those blocks
# at install time so the user isn't prompted on first launch — the act of
# running /everywhere-codex-setup is the consent.
#
# We only touch [hooks.state.".../hooks.json:..."] keys whose path prefix
# matches our hooks_path; other [hooks.state] entries (for hooks the user
# trusted manually) are preserved.


def _hash_command(cmd: str) -> str:
    return "sha256:" + hashlib.sha256(cmd.encode("utf-8")).hexdigest()


def _event_snake(event: str) -> str:
    """Convert Codex hook event CamelCase ('SessionStart', 'PreToolUse') to
    snake_case ('session_start', 'pre_tool_use') as used in [hooks.state] keys."""
    out = []
    for i, ch in enumerate(event):
        if i > 0 and ch.isupper() and not event[i - 1].isupper():
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _collect_our_trust_entries(hooks_path: Path) -> list[tuple[str, str]]:
    """Return ``[(trust_key, command_hash), ...]`` for every entry of ours
    in ``hooks_path``. Trust key format: ``"<hooks_path>:<event_lower>:<i>:<j>"``."""
    if not hooks_path.exists():
        return []
    try:
        data = json.loads(hooks_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    out: list[tuple[str, str]] = []
    for event, entries in (data.get("hooks") or {}).items():
        if not isinstance(entries, list):
            continue
        for outer_idx, entry in enumerate(entries):
            if not isinstance(entry, dict) or not _entry_is_ours(entry):
                continue
            for inner_idx, h in enumerate(entry.get("hooks", [])):
                cmd = h.get("command", "") if isinstance(h, dict) else ""
                if HOOK_MARKER not in cmd:
                    continue
                key = f"{hooks_path}:{_event_snake(event)}:{outer_idx}:{inner_idx}"
                out.append((key, _hash_command(cmd)))
    return out


def _strip_hooks_state_blocks(text: str) -> str:
    """Remove every ``[hooks.state...]`` block from ``text`` (header line plus
    its body lines, stopping at the next table header). Preserves leading and
    trailing newlines on adjacent content."""
    out: list[str] = []
    in_state = False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("[") and not stripped.startswith("[["):
            in_state = stripped.startswith("[hooks.state")
            if in_state:
                continue
        if in_state:
            continue
        out.append(line)
    # Drop trailing blank lines we may have left behind.
    while out and out[-1].strip() == "":
        out.pop()
    return "".join(out)


def _parse_hooks_state(text: str) -> dict[str, dict]:
    """Pull existing ``[hooks.state."key"]`` entries out of a TOML text and
    return them as ``{key: {enabled, trusted_hash}}``. Tolerant of malformed
    files: skips blocks it can't parse."""
    state: dict[str, dict] = {}
    current_key: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[hooks.state.\"") and stripped.endswith("]"):
            try:
                key = stripped.split('"', 2)[1]
            except IndexError:
                current_key = None
                continue
            current_key = key
            state.setdefault(current_key, {})
        elif stripped.startswith("[") and not stripped.startswith("[["):
            current_key = None
        elif current_key is not None and "=" in stripped:
            field, _, value = stripped.partition("=")
            field = field.strip()
            value = value.strip()
            if field == "enabled":
                state[current_key]["enabled"] = value == "true"
            elif field == "trusted_hash":
                state[current_key]["trusted_hash"] = value.strip('"')
    return state


def _emit_hooks_state(state: dict[str, dict]) -> str:
    """Render a ``hooks.state`` dict back to TOML, ending with a newline."""
    if not state:
        return ""
    lines = ["[hooks.state]"]
    for key in sorted(state):
        entry = state[key]
        lines.append("")
        lines.append(f"[hooks.state.\"{key}\"]")
        if "enabled" in entry:
            lines.append(f"enabled = {'true' if entry['enabled'] else 'false'}")
        if "trusted_hash" in entry:
            lines.append(f"trusted_hash = \"{entry['trusted_hash']}\"")
    return "\n".join(lines) + "\n"


def trust_hooks_in_config(config_path: Path, hooks_path: Path) -> None:
    """Write/refresh ``[hooks.state]`` entries for our hooks in ``config_path``
    so Codex doesn't prompt on first launch. Other config keys are left
    untouched; other users' ``[hooks.state]`` entries are preserved."""
    needed = _collect_our_trust_entries(hooks_path)
    if not needed:
        return

    text = config_path.read_text() if config_path.exists() else ""
    existing = _parse_hooks_state(text)

    # Drop our keys (we're replacing), keep others.
    our_keys = {key for key, _ in needed}
    preserved = {k: v for k, v in existing.items() if k not in our_keys}
    for key, cmd_hash in needed:
        preserved[key] = {"enabled": True, "trusted_hash": cmd_hash}

    stripped = _strip_hooks_state_blocks(text)
    new_text = stripped.rstrip()
    if new_text:
        new_text += "\n\n"
    new_text += _emit_hooks_state(preserved)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(new_text)


def untrust_hooks_in_config(config_path: Path, hooks_path: Path) -> None:
    """Remove ``[hooks.state]`` entries that point at our ``hooks_path``.
    Other entries are preserved; the ``[hooks.state]`` namespace header is
    dropped if no entries remain."""
    if not config_path.exists():
        return
    text = config_path.read_text()
    existing = _parse_hooks_state(text)
    prefix = f"{hooks_path}:"
    preserved = {k: v for k, v in existing.items() if not k.startswith(prefix)}

    stripped = _strip_hooks_state_blocks(text)
    new_text = stripped.rstrip()
    rendered = _emit_hooks_state(preserved)
    if rendered:
        if new_text:
            new_text += "\n\n"
        new_text += rendered
    elif new_text:
        new_text += "\n"

    config_path.write_text(new_text)


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
