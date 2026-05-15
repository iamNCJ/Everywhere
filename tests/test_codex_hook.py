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
