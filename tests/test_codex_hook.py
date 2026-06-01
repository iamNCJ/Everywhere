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
from hooks.snapshot import project_slug


FIXTURE_ROLLOUT = Path(__file__).parent / "fixtures" / "codex-rollout-normal.jsonl"


def _stub_summary_obj():
    return {
        "summary": "stub.",
        "decisions": "- s",
        "artifacts": "- a",
        "excerpts": "e",
        "tags": ["t"],
    }


def _rollout_with_session_id(tmp_path: Path, session_id: str) -> Path:
    rollout = tmp_path / f"rollout-{session_id}.jsonl"
    text = FIXTURE_ROLLOUT.read_text()
    text = text.replace("019de111-1111-2222-3333-444444444444", session_id)
    rollout.write_text(text)
    return rollout


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


def test_handle_rollout_uses_collision_resistant_session_short(tmp_path, memory_repo):
    """Codex UUIDv7 session ids commonly share the first 6-8 chars.

    Regression: using session_id[:6] made multiple sessions from the same
    project/date overwrite the same memory directory.
    """
    sid_a = "019e777a-1111-7000-8000-aaaaaaaaaaaa"
    sid_b = "019e777a-2222-7000-8000-bbbbbbbbbbbb"
    rollout_a = _rollout_with_session_id(tmp_path, sid_a)
    rollout_b = _rollout_with_session_id(tmp_path, sid_b)

    cursor = {}
    with patch.object(codex_sweep, "summarize", return_value=_stub_summary_obj()):
        assert _handle_rollout(
            rollout_a, cursor, now=time.time(), memory_repo=memory_repo,
            allow_incremental=True, allow_finalize=False,
        )
        assert _handle_rollout(
            rollout_b, cursor, now=time.time(), memory_repo=memory_repo,
            allow_incremental=True, allow_finalize=False,
        )

    project_name = project_slug("/Users/test/proj")
    sessions_dir = memory_repo / "projects" / project_name / "sessions"
    assert (sessions_dir / "2026-05-06-019e777a1111").is_dir()
    assert (sessions_dir / "2026-05-06-019e777a2222").is_dir()


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


def test_finalize_sweep_calls_handle_with_finalize_only(tmp_path, memory_repo, monkeypatch):
    """finalize-sweep iterates rollouts with allow_incremental=False, allow_finalize=True."""
    fake_root = tmp_path / "codex" / "sessions"
    date_dir = fake_root / "2026" / "05" / "14"
    date_dir.mkdir(parents=True)
    rollout = date_dir / "rollout-aaa.jsonl"
    rollout.write_bytes(FIXTURE_ROLLOUT.read_bytes())

    monkeypatch.setattr(codex_hook, "MEMORY_REPO", memory_repo)
    monkeypatch.setattr(codex_hook, "CODEX_SESSIONS_ROOT", fake_root)
    monkeypatch.setattr(codex_hook, "_iter_rollouts", lambda root, days: iter([rollout]))

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


def test_stop_never_calls_git_commit_push(tmp_path, memory_repo, monkeypatch):
    """End-to-end: Stop path runs through real _handle_rollout but must never push."""
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(FIXTURE_ROLLOUT.read_bytes())
    payload = {
        "session_id": "test-session-no-push",
        "transcript_path": str(rollout),
        "cwd": "/tmp/proj",
        "hook_event_name": "Stop",
    }

    monkeypatch.setattr(codex_hook, "MEMORY_REPO", memory_repo)
    monkeypatch.setattr(codex_sweep, "summarize", lambda *a, **kw: _stub_summary_obj())
    with patch.object(codex_sweep, "git_commit_push") as push_mock:
        codex_hook.run_stop(payload)
    assert push_mock.call_count == 0


def test_stop_debounce_skips_second_call(tmp_path, memory_repo, monkeypatch):
    """Two Stop invocations on the same session within DEBOUNCE_SECONDS:
    the second is debounced (decide_action returns 'skip')."""
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(FIXTURE_ROLLOUT.read_bytes())
    payload = {
        "session_id": "debounce-test",
        "transcript_path": str(rollout),
        "cwd": "/tmp/proj",
        "hook_event_name": "Stop",
    }

    monkeypatch.setattr(codex_hook, "MEMORY_REPO", memory_repo)
    monkeypatch.setattr(codex_sweep, "summarize", lambda *a, **kw: _stub_summary_obj())
    with patch.object(codex_sweep, "git_commit_push"):
        codex_hook.run_stop(payload)
        # Real session_id from fixture is what gets keyed in the cursor.
        # Second call within debounce window should be a no-op write.
        from hooks.codex_sweep import parse_codex_transcript
        sid, _, _, _ = parse_codex_transcript(rollout)
        cursor_path = memory_repo / ".snapshots" / ".codex-cursor.json"
        first_state = json.loads(cursor_path.read_text())[sid]

        codex_hook.run_stop(payload)
        second_state = json.loads(cursor_path.read_text())[sid]
    # Debounce: last_snapshot_at unchanged on the second call (decide_action returned "skip").
    assert second_state["last_snapshot_at"] == first_state["last_snapshot_at"]
