"""Tests for codex_hook.py and the parameterized _handle_rollout."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hooks import codex_sweep
from hooks.codex_sweep import _handle_rollout, FINALITY_IDLE_SECONDS


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
