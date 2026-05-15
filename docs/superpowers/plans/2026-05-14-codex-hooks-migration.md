# Codex Hooks Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the macOS-only launchd Codex sweeper with native Codex CLI hooks (`Stop` + `SessionStart`).

**Architecture:** Two new entry points in `hooks/codex_hook.py` (`stop`, `finalize-sweep`) share the existing rollout-handling code from `codex_sweep.py`. Each hook command is a `nohup ... &` one-liner that detaches in milliseconds, so Codex's no-async constraint doesn't block turns. `/everywhere-codex-setup` writes a merged `~/.codex/hooks.json`; `/everywhere-codex-uninstall` reverses it. Both perform best-effort cleanup of legacy launchd installs.

**Tech Stack:** Python 3 (stdlib only: `argparse`, `json`, `fcntl`, `subprocess`, `pathlib`), `pytest`, `codex-cli ≥ 0.130.0`.

**Spec:** `docs/specs/2026-05-14-codex-hooks-migration-design.md`

---

## File Structure

**Create:**
- `hooks/codex_hook.py` — new entry point, two subcommands (`stop`, `finalize-sweep`)
- `hooks/codex-hooks.json` — plugin-manifest template (forward-looking, uses `${CODEX_PLUGIN_ROOT}`)
- `.codex-plugin/plugin.json` — Codex plugin manifest
- `tests/test_codex_hook.py` — tests for `stop` debounce, `finalize-sweep` selection
- `tests/test_codex_hooks_install.py` — tests for hooks.json merge / uninstall

**Modify:**
- `hooks/codex_sweep.py` — parameterize `_handle_rollout` with `allow_incremental`/`allow_finalize`; remove `run_sweep()`
- `hooks/snapshot.py` — drop `--codex-sweep` CLI flag and the `run_sweep` import
- `skills/everywhere/SKILL.md` — rewrite `/everywhere-codex-setup` and `/everywhere-codex-uninstall` sections
- `INSTALL.md` — replace launchd/TCC section with 3-step hook flow
- `README.md` — update "How it works" table and design notes

**Delete:**
- `hooks/codex-sweeper.plist.template`

---

## Task 1: Parameterize `_handle_rollout`

**Files:**
- Modify: `hooks/codex_sweep.py:152-248`
- Test: `tests/test_finality.py` (existing, no changes — verifies the regression base case)

- [ ] **Step 1: Read the current `_handle_rollout` signature and body**

The function lives at `hooks/codex_sweep.py:152`. Its current behavior:
- Calls `decide_action(mtime, now, cursor.get(session_id))` → returns `'skip' | 'incremental' | 'finalize'`
- Acts on whatever `decide_action` returned

We want the same body, but the caller decides which actions are allowed.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_codex_hook.py` (create the file):

```python
"""Tests for codex_hook.py and the parameterized _handle_rollout."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hooks import codex_sweep
from hooks.codex_sweep import _handle_rollout, FINALITY_IDLE_SECONDS, DEBOUNCE_SECONDS


FIXTURE_ROLLOUT = Path(__file__).parent / "fixtures" / "codex-rollout-normal.jsonl"


def _stub_summary_obj():
    return {
        "summary": "stub.",
        "decisions": "- s",
        "artifacts": "- a",
        "excerpts": "e",
        "tags": ["t"],
    }


@pytest.fixture
def memory_repo(tmp_path):
    repo = tmp_path / "memory"
    (repo / ".git").mkdir(parents=True)
    return repo


def test_handle_rollout_finalize_disabled_skips_idle(tmp_path, memory_repo, monkeypatch):
    """When allow_finalize=False, an idle rollout that decide_action says
    'finalize' should be skipped (no write, no commit)."""
    rollout = tmp_path / "rollout-test.jsonl"
    rollout.write_bytes(FIXTURE_ROLLOUT.read_bytes())
    very_old = time.time() - FINALITY_IDLE_SECONDS - 60
    import os
    os.utime(rollout, (very_old, very_old))

    cursor = {}
    with patch.object(codex_sweep, "summarize", return_value=_stub_summary_obj()), \
         patch.object(codex_sweep, "git_commit_push") as push_mock:
        handled = _handle_rollout(
            rollout, cursor, now=time.time(), memory_repo=memory_repo,
            allow_incremental=True, allow_finalize=False,
        )
    assert handled is False
    assert push_mock.call_count == 0


def test_handle_rollout_incremental_disabled_skips_active(tmp_path, memory_repo):
    """When allow_incremental=False, an active rollout that decide_action says
    'incremental' should be skipped."""
    rollout = tmp_path / "rollout-test.jsonl"
    rollout.write_bytes(FIXTURE_ROLLOUT.read_bytes())
    # mtime is "now" → action will be 'incremental'

    cursor = {}
    with patch.object(codex_sweep, "summarize", return_value=_stub_summary_obj()):
        handled = _handle_rollout(
            rollout, cursor, now=time.time(), memory_repo=memory_repo,
            allow_incremental=False, allow_finalize=True,
        )
    assert handled is False
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_codex_hook.py -v` (from the repo root)
Expected: FAIL with `TypeError: _handle_rollout() got an unexpected keyword argument 'allow_incremental'`

- [ ] **Step 4: Update `_handle_rollout` signature**

Edit `hooks/codex_sweep.py:152`. Change:

```python
def _handle_rollout(rollout: Path, cursor: dict, now: float, memory_repo: Path) -> bool:
    mtime = rollout.stat().st_mtime
    session_id, cwd, started_at, messages = parse_codex_transcript(rollout)
    if session_id is None:
        return False

    action = decide_action(mtime, now, cursor.get(session_id))
    _log(f"{session_id[:8]} mtime_age={int(now-mtime)}s action={action}")
    if action == "skip":
        return False
```

to:

```python
def _handle_rollout(
    rollout: Path,
    cursor: dict,
    now: float,
    memory_repo: Path,
    *,
    allow_incremental: bool = True,
    allow_finalize: bool = True,
) -> bool:
    mtime = rollout.stat().st_mtime
    session_id, cwd, started_at, messages = parse_codex_transcript(rollout)
    if session_id is None:
        return False

    action = decide_action(mtime, now, cursor.get(session_id))
    _log(f"{session_id[:8]} mtime_age={int(now-mtime)}s action={action}")
    if action == "skip":
        return False
    if action == "incremental" and not allow_incremental:
        return False
    if action == "finalize" and not allow_finalize:
        return False
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_codex_hook.py tests/test_finality.py -v`
Expected: PASS (both new tests, plus all 5 existing finality tests).

- [ ] **Step 6: Commit**

```bash
git add hooks/codex_sweep.py tests/test_codex_hook.py
git commit -m "refactor(codex_sweep): parameterize _handle_rollout with action gates

Adds allow_incremental and allow_finalize keyword args so the caller decides
which decide_action outcomes are honored. Backward-compatible: both default
to True, matching prior behavior."
```

---

## Task 2: Create `codex_hook.py` `stop` subcommand

**Files:**
- Create: `hooks/codex_hook.py`
- Test: `tests/test_codex_hook.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_codex_hook.py`:

```python
import subprocess
from hooks import codex_hook


def test_stop_reads_payload_and_calls_handle_rollout(tmp_path, memory_repo, monkeypatch):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(FIXTURE_ROLLOUT.read_bytes())

    payload = {
        "session_id": "test-session-abc",
        "transcript_path": str(rollout),
        "cwd": "/tmp/proj",
        "hook_event_name": "Stop",
    }

    monkeypatch.setattr(codex_hook, "MEMORY_REPO", memory_repo)
    captured = {}

    def fake_handle(rollout_arg, cursor, now, memory_repo_arg, *,
                    allow_incremental, allow_finalize):
        captured["allow_incremental"] = allow_incremental
        captured["allow_finalize"] = allow_finalize
        captured["rollout"] = rollout_arg
        return True

    monkeypatch.setattr(codex_hook, "_handle_rollout", fake_handle)
    monkeypatch.setattr(codex_hook, "save_cursor", lambda *a, **kw: None)
    monkeypatch.setattr(codex_hook, "load_cursor", lambda *a, **kw: {})

    codex_hook.run_stop(payload)

    assert captured["allow_incremental"] is True
    assert captured["allow_finalize"] is False
    assert captured["rollout"] == rollout


def test_stop_no_op_when_payload_missing_fields(memory_repo, monkeypatch):
    monkeypatch.setattr(codex_hook, "MEMORY_REPO", memory_repo)
    # Should return without raising. Missing session_id.
    codex_hook.run_stop({"transcript_path": "/x", "cwd": "/y"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_codex_hook.py::test_stop_reads_payload_and_calls_handle_rollout -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hooks.codex_hook'`

- [ ] **Step 3: Create `hooks/codex_hook.py` with `stop` subcommand**

```python
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


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stop")
    sub.add_parser("finalize-sweep")
    args = parser.parse_args()

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
    except Exception as e:
        err(f"unhandled: {e}")
        sys.exit(0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_codex_hook.py -v`
Expected: PASS (4 tests so far).

- [ ] **Step 5: Commit**

```bash
git add hooks/codex_hook.py tests/test_codex_hook.py
git commit -m "feat(codex): add codex_hook.py stop subcommand

New entry point for the Codex CLI Stop hook. Reads hook payload from stdin
and takes a debounced incremental snapshot via the existing _handle_rollout
logic (with allow_finalize=False)."
```

---

## Task 3: Add `finalize-sweep` subcommand

**Files:**
- Modify: `hooks/codex_hook.py`
- Test: `tests/test_codex_hook.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_codex_hook.py`:

```python
def test_finalize_sweep_calls_handle_with_finalize_only(tmp_path, memory_repo, monkeypatch):
    """finalize-sweep iterates rollouts with allow_incremental=False, allow_finalize=True."""
    fake_root = tmp_path / "codex" / "sessions"
    date_dir = fake_root / "2026" / "05" / "14"
    date_dir.mkdir(parents=True)
    rollout = date_dir / "rollout-aaa.jsonl"
    rollout.write_bytes(FIXTURE_ROLLOUT.read_bytes())

    monkeypatch.setattr(codex_hook, "MEMORY_REPO", memory_repo)
    monkeypatch.setattr(codex_hook, "CODEX_SESSIONS_ROOT", fake_root)

    calls = []

    def fake_handle(rollout_arg, cursor, now, memory_repo_arg, *,
                    allow_incremental, allow_finalize):
        calls.append({"rollout": rollout_arg,
                      "allow_incremental": allow_incremental,
                      "allow_finalize": allow_finalize})
        return False

    monkeypatch.setattr(codex_hook, "_handle_rollout", fake_handle)

    codex_hook.run_finalize_sweep()

    assert len(calls) == 1
    assert calls[0]["allow_incremental"] is False
    assert calls[0]["allow_finalize"] is True


def test_finalize_sweep_noop_when_no_sessions_dir(tmp_path, memory_repo, monkeypatch):
    monkeypatch.setattr(codex_hook, "MEMORY_REPO", memory_repo)
    monkeypatch.setattr(codex_hook, "CODEX_SESSIONS_ROOT", tmp_path / "does-not-exist")
    # Must not raise.
    codex_hook.run_finalize_sweep()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_codex_hook.py::test_finalize_sweep_calls_handle_with_finalize_only -v`
Expected: FAIL with `AttributeError: module 'hooks.codex_hook' has no attribute 'run_finalize_sweep'`

- [ ] **Step 3: Add `run_finalize_sweep` to `hooks/codex_hook.py`**

Insert before `def main():`:

```python
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
```

Wire it into `main()`. Replace:

```python
    if args.cmd == "stop":
        raw = sys.stdin.read()
```

with:

```python
    if args.cmd == "finalize-sweep":
        run_finalize_sweep()
        return

    if args.cmd == "stop":
        raw = sys.stdin.read()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_codex_hook.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add hooks/codex_hook.py tests/test_codex_hook.py
git commit -m "feat(codex): add codex_hook.py finalize-sweep subcommand

Fired by Codex SessionStart hook at startup/resume. Walks recent rollouts
under ~/.codex/sessions and finalizes any idle for >FINALITY_IDLE_SECONDS,
committing and pushing the memory repo. Skips incremental candidates
(Stop owns those)."
```

---

## Task 4: Remove `run_sweep` and snapshot.py's `--codex-sweep` flag

**Files:**
- Modify: `hooks/codex_sweep.py:111-149` (delete `run_sweep`)
- Modify: `hooks/snapshot.py:369-375` (delete `--codex-sweep` flag + import)

- [ ] **Step 1: Delete `run_sweep` from `hooks/codex_sweep.py`**

Delete lines 111–149 (the `def run_sweep(memory_repo: Path) -> int:` function). Keep everything else.

- [ ] **Step 2: Delete `--codex-sweep` plumbing from `hooks/snapshot.py`**

In `hooks/snapshot.py:369-375`, remove these lines:

```python
    parser.add_argument("--codex-sweep", action="store_true",
                        help="Run the Codex session sweeper instead of reading a hook payload")
    args = parser.parse_args()

    if args.codex_sweep:
        from hooks.codex_sweep import run_sweep
        sys.exit(run_sweep(MEMORY_REPO))
```

So the function becomes:

```python
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip claude -p; use a stub summary (for testing)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    raw = sys.stdin.read()
```

- [ ] **Step 3: Run all tests to verify nothing broke**

Run: `pytest tests/ -v`
Expected: PASS (all tests; `test_cursor.py` still imports `load_cursor` / `save_cursor` which remain).

- [ ] **Step 4: Commit**

```bash
git add hooks/codex_sweep.py hooks/snapshot.py
git commit -m "refactor(codex): remove run_sweep and snapshot.py --codex-sweep flag

The launchd-driven sweep is replaced by Codex hooks; finalize-sweep lives
in codex_hook.py. Only the cursor/iter/handle helpers remain in
codex_sweep.py as a shared library."
```

---

## Task 5: Add hooks.json merge / uninstall helpers

**Files:**
- Modify: `hooks/codex_hook.py` (add `install_hooks` and `uninstall_hooks`)
- Test: `tests/test_codex_hooks_install.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_codex_hooks_install.py`:

```python
"""Tests for ~/.codex/hooks.json merge / uninstall logic."""
from __future__ import annotations

import json
from pathlib import Path

from hooks.codex_hook import install_hooks_json, uninstall_hooks_json

HOOK_MARKER = "/.everywhere/hooks/codex_hook.py"


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def test_install_creates_file_when_missing(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    install_hooks_json(hooks_path, staged_dir="/home/u/.everywhere/hooks")

    data = _read(hooks_path)
    assert "Stop" in data["hooks"]
    assert "SessionStart" in data["hooks"]
    cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "/home/u/.everywhere/hooks/codex_hook.py stop" in cmd
    cmd2 = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "finalize-sweep" in cmd2
    assert data["hooks"]["SessionStart"][0]["matcher"] == "startup|resume"


def test_install_is_idempotent(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    install_hooks_json(hooks_path, staged_dir="/home/u/.everywhere/hooks")
    install_hooks_json(hooks_path, staged_dir="/home/u/.everywhere/hooks")
    data = _read(hooks_path)
    assert len(data["hooks"]["Stop"]) == 1
    assert len(data["hooks"]["SessionStart"]) == 1


def test_install_preserves_user_entries(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text(json.dumps({
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": "/usr/local/bin/my-other-hook"}]}
            ],
            "PreToolUse": [
                {"matcher": "Bash",
                 "hooks": [{"type": "command", "command": "/usr/local/bin/audit"}]}
            ],
        }
    }))

    install_hooks_json(hooks_path, staged_dir="/home/u/.everywhere/hooks")

    data = _read(hooks_path)
    # User's Stop entry preserved alongside ours.
    stop_cmds = [g["hooks"][0]["command"] for g in data["hooks"]["Stop"]]
    assert "/usr/local/bin/my-other-hook" in stop_cmds
    assert any(HOOK_MARKER in c for c in stop_cmds)
    # Unrelated event untouched.
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "/usr/local/bin/audit"


def test_uninstall_removes_only_our_entries(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    install_hooks_json(hooks_path, staged_dir="/home/u/.everywhere/hooks")
    # Add a user entry alongside.
    data = _read(hooks_path)
    data["hooks"]["Stop"].append(
        {"hooks": [{"type": "command", "command": "/usr/local/bin/user-hook"}]}
    )
    hooks_path.write_text(json.dumps(data))

    uninstall_hooks_json(hooks_path)

    data = _read(hooks_path)
    # User entry preserved, ours gone.
    stop_cmds = [g["hooks"][0]["command"] for g in data["hooks"]["Stop"]]
    assert stop_cmds == ["/usr/local/bin/user-hook"]
    assert "SessionStart" not in data["hooks"]  # Was only ours; key dropped.


def test_uninstall_deletes_file_when_empty(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    install_hooks_json(hooks_path, staged_dir="/home/u/.everywhere/hooks")
    uninstall_hooks_json(hooks_path)
    assert not hooks_path.exists()


def test_uninstall_missing_file_is_noop(tmp_path):
    hooks_path = tmp_path / "missing.json"
    # Must not raise.
    uninstall_hooks_json(hooks_path)
    assert not hooks_path.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_codex_hooks_install.py -v`
Expected: FAIL with `ImportError: cannot import name 'install_hooks_json'`

- [ ] **Step 3: Add `install_hooks_json` and `uninstall_hooks_json` to `hooks/codex_hook.py`**

Append to `hooks/codex_hook.py` (before `def main():`):

```python
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
        kept = [e for e in existing if not _entry_is_ours(e)]
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
        kept = [e for e in data["hooks"][event] if not _entry_is_ours(e)]
        if kept:
            data["hooks"][event] = kept
        else:
            del data["hooks"][event]

    if not data["hooks"]:
        hooks_path.unlink()
        return

    hooks_path.write_text(json.dumps(data, indent=2) + "\n")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_codex_hooks_install.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add hooks/codex_hook.py tests/test_codex_hooks_install.py
git commit -m "feat(codex): hooks.json install/uninstall with namespaced merge

Identifies our entries by '/.everywhere/hooks/codex_hook.py' marker in the
command string. Install is idempotent and preserves user-owned entries;
uninstall drops empty event keys and deletes the file when fully empty."
```

---

## Task 6: Add Codex plugin manifest + plugin-rooted hooks.json

**Files:**
- Create: `.codex-plugin/plugin.json`
- Create: `hooks/codex-hooks.json`

- [ ] **Step 1: Create `.codex-plugin/plugin.json`**

```json
{
  "name": "everywhere",
  "version": "1.1.0",
  "description": "Automatically persists every Codex CLI session to a searchable global memory repository synced to GitHub",
  "author": {
    "name": "ncj",
    "email": "me@ncj.wiki"
  },
  "hooks": "./hooks/codex-hooks.json",
  "skills": "./skills/"
}
```

- [ ] **Step 2: Create `hooks/codex-hooks.json`**

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "nohup python3 -u \"${CODEX_PLUGIN_ROOT}/hooks/codex_hook.py\" stop >/dev/null 2>&1 &",
            "timeout": 5
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          {
            "type": "command",
            "command": "nohup python3 -u \"${CODEX_PLUGIN_ROOT}/hooks/codex_hook.py\" finalize-sweep >/dev/null 2>&1 &",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 3: Validate JSON parses**

Run: `python3 -c "import json; json.load(open('.codex-plugin/plugin.json')); json.load(open('hooks/codex-hooks.json')); print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add .codex-plugin/plugin.json hooks/codex-hooks.json
git commit -m "feat(codex): add plugin manifest + plugin-rooted hooks.json

Forward-looking: enables Codex plugin marketplace installs once distribution
goes that route. Not consumed by /everywhere-codex-setup, which writes
~/.codex/hooks.json with resolved absolute paths."
```

---

## Task 7: Delete legacy launchd plist template

**Files:**
- Delete: `hooks/codex-sweeper.plist.template`

- [ ] **Step 1: Verify nothing references it**

Run: `grep -r "codex-sweeper.plist" --include="*.py" --include="*.md" .`
Expected: matches only in `INSTALL.md` and `skills/everywhere/SKILL.md` (docs — fixed in Task 8) and `README.md` (fixed in Task 8). No code references.

- [ ] **Step 2: Delete the file**

```bash
git rm hooks/codex-sweeper.plist.template
```

- [ ] **Step 3: Commit**

```bash
git commit -m "chore(codex): remove launchd plist template

Migration to Codex hooks (Task 5+8) makes the launchd job obsolete.
Setup/uninstall flows still call 'launchctl bootout' as best-effort
cleanup for users upgrading from the launchd installation."
```

---

## Task 8: Rewrite SKILL.md setup/uninstall flows

**Files:**
- Modify: `skills/everywhere/SKILL.md`

- [ ] **Step 1: Read the existing sections**

Look at `skills/everywhere/SKILL.md` lines 172–283 (the `/everywhere-codex-setup` and `/everywhere-codex-uninstall` blocks).

- [ ] **Step 2: Replace `/everywhere-codex-setup` section**

Find the heading `### \`/everywhere-codex-setup\`` and replace its body (down to the next `### ` heading) with:

````markdown
### `/everywhere-codex-setup`

Registers Codex CLI hooks that auto-capture sessions into `~/agent-memory/`.

**Prerequisite check:**

```bash
codex features list 2>/dev/null | grep -q "^hooks .*true" || {
  echo "Codex hooks not available. Need codex-cli >= 0.130.0 with 'hooks' feature."
  exit 1
}
```

If the check fails, tell the user to upgrade Codex and stop.

#### 1. Migrate from legacy launchd install (best-effort)

```bash
LABEL=dev.everywhere.codex-sweeper
if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "gui/$UID" "$HOME/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || true
fi
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
```

If the plist existed, tell the user the launchd job was removed in favor of Codex hooks.

#### 2. Stage hook scripts to `~/.everywhere/hooks/`

Codex hooks fire under the user (no TCC restrictions), but `~/.everywhere/hooks/` still gives a stable absolute path even in dev-mode installs.

```bash
ABS_PLUGIN_ROOT="$(resolve plugin root; for dev mode this is the repo path)"
mkdir -p "$HOME/.everywhere/hooks"
cp "$ABS_PLUGIN_ROOT/hooks/"{snapshot.py,codex_hook.py,codex_sweep.py,summarizer.py,__init__.py} \
   "$HOME/.everywhere/hooks/"
```

Re-running setup refreshes these copies.

#### 3. Merge into `~/.codex/hooks.json`

```bash
python3 -c "
from pathlib import Path
import sys; sys.path.insert(0, '$HOME/.everywhere/hooks')
sys.path.insert(0, '$HOME/.everywhere')
from hooks.codex_hook import install_hooks_json
install_hooks_json(Path.home() / '.codex' / 'hooks.json',
                   staged_dir=str(Path.home() / '.everywhere' / 'hooks'))
print('ok')
"
```

Verify: `python3 -c "import json; print(json.dumps(json.load(open('$HOME/.codex/hooks.json')), indent=2))"` should show two entries (`Stop` and `SessionStart`) under `hooks`, each pointing at `~/.everywhere/hooks/codex_hook.py`.

#### 4. Smoke test

Tell the user to open a Codex session and run `/exit` (or just wait a turn). Then check:

```bash
ls -lt $HOME/agent-memory/.snapshots/.codex-cursor.json 2>/dev/null
```

The cursor file appears once the first Stop hook fires.
````

- [ ] **Step 3: Replace `/everywhere-codex-uninstall` section**

Find `### \`/everywhere-codex-uninstall\`` and replace its body with:

````markdown
### `/everywhere-codex-uninstall`

Removes Codex hook registrations and staged scripts. The memory repo and existing session files are untouched.

#### 1. Remove hook entries from `~/.codex/hooks.json`

```bash
python3 -c "
from pathlib import Path
import sys; sys.path.insert(0, '$HOME/.everywhere/hooks')
sys.path.insert(0, '$HOME/.everywhere')
from hooks.codex_hook import uninstall_hooks_json
uninstall_hooks_json(Path.home() / '.codex' / 'hooks.json')
print('ok')
"
```

Entries whose command does **not** reference `~/.everywhere/hooks/codex_hook.py` are preserved. Empty events are dropped; if `hooks.json` ends up empty, the file is deleted.

#### 2. Best-effort launchd cleanup (for users upgrading from the old install)

```bash
LABEL=dev.everywhere.codex-sweeper
launchctl bootout "gui/$UID" "$HOME/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
```

#### 3. Remove staged scripts

```bash
rm -rf "$HOME/.everywhere/hooks"
```

Tell the user that re-running `/everywhere-codex-setup` will re-register and resume capture (the existing cursor in `~/agent-memory/.snapshots/.codex-cursor.json` is preserved across uninstall/reinstall).
````

- [ ] **Step 4: Update the `/everywhere-codex-setup` summary line near the top**

In the file, find this line (around line 10):

```
For Codex, run `/everywhere-codex-setup` to install a launchd sweeper that polls `~/.codex/sessions/` every 5 minutes.
```

Replace with:

```
For Codex, run `/everywhere-codex-setup` to register Codex `Stop` and `SessionStart` hooks that capture each turn and finalize idle sessions on the next launch.
```

- [ ] **Step 5: Commit**

```bash
git add skills/everywhere/SKILL.md
git commit -m "docs(skill): rewrite codex setup/uninstall for hook-based capture

Replaces the 5-step launchd flow (stage + plist + bootstrap + verify) with
3 steps: migrate legacy install, stage scripts, merge ~/.codex/hooks.json.
Uninstall now drops hook entries and best-effort cleans the legacy plist."
```

---

## Task 9: Update INSTALL.md

**Files:**
- Modify: `INSTALL.md`

- [ ] **Step 1: Read INSTALL.md around lines 220–293**

Find the `/everywhere-codex-setup` section (starts ~line 223 with "Then invoke `/everywhere-codex-setup`").

- [ ] **Step 2: Replace the Codex install paragraph**

Find:

```
Then invoke `/everywhere-codex-setup` inside Claude Code. The skill walks
through:

1. Stage hooks to `~/.everywhere/hooks/` (TCC-safe location — launchd can't
   read paths under `~/Documents/`).
2. Render `hooks/codex-sweeper.plist.template` with absolute paths and write
   it to `~/Library/LaunchAgents/dev.everywhere.codex-sweeper.plist`.
```

Replace with:

```
Then invoke `/everywhere-codex-setup` inside Claude Code. The skill walks
through:

1. (If upgrading from the launchd version) `launchctl bootout` the old job
   and delete the plist.
2. Stage hooks to `~/.everywhere/hooks/` so the registered `command` strings
   in `~/.codex/hooks.json` use stable absolute paths.
3. Merge two entries (`Stop`, `SessionStart`) into `~/.codex/hooks.json`.
   The merge preserves any existing user-owned hook entries.

Requires `codex-cli >= 0.130.0` (`codex features list | grep ^hooks` must
show `true`).
```

- [ ] **Step 3: Find and replace the uninstall reference**

Find the line that says `rm -f ~/Library/LaunchAgents/dev.everywhere.codex-sweeper.plist` (~line 293) and surrounding context. Replace the uninstall paragraph with:

```
To uninstall Codex capture, run `/everywhere-codex-uninstall`. It removes our
entries from `~/.codex/hooks.json` (preserving any user-owned hooks),
best-effort cleans up the legacy launchd plist, and removes
`~/.everywhere/hooks/`. The memory repo and session files are untouched.
```

- [ ] **Step 4: Commit**

```bash
git add INSTALL.md
git commit -m "docs(install): update codex section for hook-based capture"
```

---

## Task 10: Update README.md

**Files:**
- Modify: `README.md:31-46` (How it works table), `README.md:117-141` (Design notes)

- [ ] **Step 1: Replace the "How it works" table row**

In `README.md`, find the table starting at line 31:

```
| Trigger | Agent | When | Action |
|---|---|---|---|
| `Stop` hook | Claude Code | End of each turn | Debounced (10 min) snapshot. |
| `SessionEnd` hook | Claude Code | `/exit` or session close | Final snapshot, commit, `pull --rebase` + push. |
| `launchd` sweeper | Codex CLI | Every 5 min | Walks `~/.codex/sessions/`, snapshots active rollouts, finalizes idle ones (>10 min no activity). |
```

Replace the launchd row with two rows:

```
| `Stop` hook | Codex CLI | End of each turn | Debounced (10 min) snapshot of the current rollout. |
| `SessionStart` hook | Codex CLI | Codex launch (`startup` or `resume`) | Walks recent rollouts, finalizes any idle >10 min, commits + pushes. |
```

- [ ] **Step 2: Update the "Codex sessions are summarized" paragraph**

Find (around line 41):

```
Sessions with fewer than 3 user messages are skipped on both paths. The Codex
sweeper does **not** modify `~/.codex/config.toml` — it doesn't touch `notify`,
so existing notify integrations (e.g., Computer Use) keep working.
```

Replace with:

```
Sessions with fewer than 3 user messages are skipped on both paths. The Codex
setup does **not** modify `~/.codex/config.toml` — it only touches
`~/.codex/hooks.json` and preserves any pre-existing user-owned hook entries.
Existing `notify` integrations (e.g., Computer Use) keep working.

Note: finalize runs at the **next** Codex launch, not at session exit
(Codex CLI has no `SessionEnd` event as of 0.130.0). If you go a week without
opening Codex, the in-progress rollout from your last session won't push
until you launch Codex again.
```

- [ ] **Step 3: Update the design-notes section**

Find lines 117–141 (the `Design notes` section). Replace the `**Codex trigger:**` and `**TCC staging:**` bullets:

```
- **Codex trigger:** Codex `Stop` and `SessionStart` hooks (stable since
  codex-cli 0.130.0). Stop fires per turn for incremental snapshots;
  SessionStart fires on `startup`/`resume` and runs the finalize sweep.
  Both hook commands fork a detached process (`nohup ... &`) so they return
  in milliseconds — Codex's no-async constraint doesn't block the TUI.
- **Staging directory:** Scripts are still copied to `~/.everywhere/hooks/`,
  but only to give the registered hook commands a stable absolute path.
  The TCC workaround that motivated this in the launchd era is no longer
  needed (Codex hooks run under the user, not launchd).
```

Delete the rest of the `**TCC staging:**` bullet if anything remains.

- [ ] **Step 4: Update Configuration table footnote**

Find (around line 147):

```
| `EVERYWHERE_CODEX_MODEL` | `gpt-5.4-mini` | Codex summarizer model. Set inline in the launchd plist's `EnvironmentVariables` block for persistence. |
```

Replace with:

```
| `EVERYWHERE_CODEX_MODEL` | `gpt-5.4-mini` | Codex summarizer model. Export from your shell rc; the Codex hook commands inherit the parent Codex process's environment. |
```

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs(readme): describe Codex hook-based capture path"
```

---

## Task 11: End-to-end manual verification

**Files:** (no edits — observation only)

- [ ] **Step 1: Run the full test suite**

Run (from the repo root): `pytest tests/ -v`
Expected: all tests pass (12+ tests).

- [ ] **Step 2: Verify Codex hooks feature is enabled**

Run: `codex features list | grep ^hooks`
Expected: `hooks                                   stable             true`

- [ ] **Step 3: Manually run install + uninstall against a tmp hooks file**

Run from the repo root (so `hooks/` is importable):

```bash
TMP=$(mktemp -d)
PYTHONPATH=. python3 -c "
import os
from pathlib import Path
from hooks.codex_hook import install_hooks_json, uninstall_hooks_json
hp = Path(os.environ['TMP']) / 'hooks.json'
install_hooks_json(hp, staged_dir=str(Path.home() / '.everywhere' / 'hooks'))
print(hp.read_text())
print('---')
uninstall_hooks_json(hp)
print('exists after uninstall:', hp.exists())
"
```

Expected: JSON with two entries (the `command` strings expand `Path.home()`
to whatever your `$HOME` is — they do **not** contain `/Users/ncj/...` unless
that happens to be your home); then `exists after uninstall: False`.

- [ ] **Step 4: Smoke-test the `stop` subcommand directly**

Run from the repo root:

```bash
echo '{"session_id":"smoke-test","transcript_path":"/dev/null","cwd":"/tmp","hook_event_name":"Stop"}' | \
  PYTHONPATH=. python3 hooks/codex_hook.py stop
echo "exit=$?"
```

Expected: `exit=0` (transcript not found → err to stderr but exit 0 — fail-silent contract).

- [ ] **Step 5: Smoke-test `finalize-sweep` directly**

Run from the repo root:

```bash
PYTHONPATH=. python3 hooks/codex_hook.py finalize-sweep
echo "exit=$?"
```

Expected: `exit=0`. If `~/agent-memory/` exists and has `.git`, it walks rollouts. Otherwise logs "memory repo not initialized" and exits 0.

- [ ] **Step 6: Verify legacy launchd plist still removable**

```bash
LABEL=dev.everywhere.codex-sweeper
ls -la ~/Library/LaunchAgents/$LABEL.plist 2>/dev/null && echo "legacy still present — run /everywhere-codex-uninstall"
```

Expected: either no output (already gone) or a path you can remove via the uninstall flow.

- [ ] **Step 7: Commit nothing**

Verification produces no diffs. Move on.

---

## Self-Review Notes

**Spec coverage:**
- §Architecture > Filesystem → Tasks 2, 3, 5, 6, 7 (all new files + deletion)
- §Architecture > Hook registration → Tasks 5 (template), 6 (plugin-rooted variant)
- §Architecture > `codex_hook.py` `stop` → Task 2
- §Architecture > `codex_hook.py` `finalize-sweep` → Task 3
- §Architecture > Shared code → Tasks 1 (parameterize) + 4 (remove run_sweep)
- §Architecture > Concurrency → Tasks 2 & 3 reuse the existing `fcntl` lock and per-session cursor keying; no new code needed
- §Setup flow (3 steps) → Task 8 (SKILL.md)
- §Uninstall flow → Task 8 (SKILL.md)
- §Plugin manifest → Task 6
- §Documentation updates → Tasks 8, 9, 10
- §Testing checklist → Tasks 1, 2, 3, 5 (each adds the listed test cases)
- §Risks > codex < 0.130.0 → Task 8 (prerequisite check)
- §Risks > user has own hooks.json → Task 5 (merge tests cover this)

**Placeholder scan:** No `TBD`/`TODO`/"similar to" left in the plan.

**Type consistency:** `install_hooks_json` and `uninstall_hooks_json` signatures match between Task 5 (definition + tests) and Task 8 (SKILL.md invocation). `_handle_rollout`'s new kwargs (`allow_incremental`, `allow_finalize`) used identically in Tasks 1, 2, 3.
