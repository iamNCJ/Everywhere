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


def test_install_recovers_from_corrupt_json(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text("{ not valid json")

    install_hooks_json(hooks_path, staged_dir="/home/u/.everywhere/hooks")

    data = _read(hooks_path)
    assert "Stop" in data["hooks"]
    assert "SessionStart" in data["hooks"]


def test_install_recovers_from_malformed_event_value(tmp_path):
    """Event value is a string instead of a list — install treats as empty."""
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text(json.dumps({"hooks": {"Stop": "bad"}}))

    install_hooks_json(hooks_path, staged_dir="/home/u/.everywhere/hooks")

    data = _read(hooks_path)
    assert isinstance(data["hooks"]["Stop"], list)
    assert len(data["hooks"]["Stop"]) == 1


def test_uninstall_skips_malformed_event_value(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text(json.dumps({"hooks": {"Stop": "bad", "Other": [
        {"hooks": [{"type": "command", "command": "/usr/local/bin/x"}]}
    ]}}))

    # Must not raise.
    uninstall_hooks_json(hooks_path)

    data = _read(hooks_path)
    # Malformed value is preserved as-is (not our concern).
    assert data["hooks"]["Stop"] == "bad"
    assert data["hooks"]["Other"][0]["hooks"][0]["command"] == "/usr/local/bin/x"
