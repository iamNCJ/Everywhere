from __future__ import annotations

import json
from pathlib import Path

from hooks.codex_repair import (
    backfill_candidate,
    collect_missing_codex_rollouts,
)
from hooks.codex_sweep import load_cursor


FIXTURE_ROLLOUT = Path(__file__).parent / "fixtures" / "codex-rollout-normal.jsonl"


def _stub_summary_obj():
    return {
        "summary": "Backfilled session.",
        "decisions": "- decision",
        "artifacts": "## Files\n\n- file.txt - touched",
        "excerpts": "## Exchange 1: test\n\n**User:** hello\n\n**Assistant:** hi",
        "tags": ["backfill", "test"],
    }


def _write_rollout(root: Path, session_id: str, cwd: str = "/Users/test/proj") -> Path:
    date_dir = root / "2026" / "05" / "06"
    date_dir.mkdir(parents=True, exist_ok=True)
    rollout = date_dir / f"rollout-{session_id}.jsonl"
    text = FIXTURE_ROLLOUT.read_text()
    text = text.replace("019de111-1111-2222-3333-444444444444", session_id)
    text = text.replace("/Users/test/proj", cwd)
    rollout.write_text(text)
    return rollout


def _write_existing_meta(memory_repo: Path, session_id: str) -> None:
    session_dir = memory_repo / "projects" / "proj-e8faf588" / "sessions" / "2026-05-06-existing"
    session_dir.mkdir(parents=True)
    (session_dir / "meta.yaml").write_text(
        f'session_id: "{session_id}"\nagent: "codex"\n'
    )
    for name in ("summary.md", "decisions.md", "artifacts.md", "excerpts.md"):
        (session_dir / name).write_text("existing\n")


def _write_stub_meta(memory_repo: Path, session_id: str) -> None:
    session_dir = memory_repo / "projects" / "proj-e8faf588" / "sessions" / "2026-05-06-stub"
    session_dir.mkdir(parents=True)
    (session_dir / "meta.yaml").write_text(
        f'session_id: "{session_id}"\nagent: "codex"\ntags: ["codex", "stub-summary"]\n'
    )
    for name in ("summary.md", "decisions.md", "artifacts.md", "excerpts.md"):
        (session_dir / name).write_text("stub\n")


def _write_partial_meta(memory_repo: Path, session_id: str) -> None:
    session_dir = memory_repo / "projects" / "proj-e8faf588" / "sessions" / "2026-05-06-partial"
    session_dir.mkdir(parents=True)
    (session_dir / "meta.yaml").write_text(f'session_id: "{session_id}"\nagent: "codex"\n')
    (session_dir / "summary.md").write_text("partial\n")


def test_collect_missing_codex_rollouts_skips_existing_and_short_sessions(tmp_path):
    memory_repo = tmp_path / "memory"
    sessions_root = tmp_path / "sessions"
    memory_repo.mkdir()

    existing_sid = "019e777a-1111-7000-8000-aaaaaaaaaaaa"
    missing_sid = "019e777a-2222-7000-8000-bbbbbbbbbbbb"
    short_sid = "019e777a-3333-7000-8000-cccccccccccc"
    _write_existing_meta(memory_repo, existing_sid)
    _write_rollout(sessions_root, existing_sid)
    _write_rollout(sessions_root, missing_sid)

    short_rollout = _write_rollout(sessions_root, short_sid)
    lines = short_rollout.read_text().splitlines()
    # Fixture has 3 user messages. Keep only the first two user turns so it is
    # below the Everywhere persistence threshold.
    short_rollout.write_text("\n".join(lines[:6]) + "\n")

    candidates = collect_missing_codex_rollouts(memory_repo, sessions_root)

    assert [c.session_id for c in candidates] == [missing_sid]
    assert candidates[0].session_short == "019e777a2222"


def test_backfill_candidate_writes_long_session_dir_and_cursor(tmp_path):
    memory_repo = tmp_path / "memory"
    sessions_root = tmp_path / "sessions"
    memory_repo.mkdir()
    (memory_repo / ".snapshots").mkdir()
    session_id = "019e777a-2222-7000-8000-bbbbbbbbbbbb"
    _write_rollout(sessions_root, session_id)
    candidate = collect_missing_codex_rollouts(memory_repo, sessions_root)[0]
    cursor = {}

    session_dir = backfill_candidate(
        candidate,
        memory_repo,
        cursor,
        summarize_fn=lambda *args, **kwargs: _stub_summary_obj(),
        now=1_000_000,
    )

    assert session_dir.name == "2026-05-06-019e777a2222"
    meta = (session_dir / "meta.yaml").read_text()
    assert f'session_id: "{session_id}"' in meta
    assert 'session_id_short: "019e777a2222"' in meta
    assert 'is_final: true' in meta
    assert cursor[session_id]["is_final"] is True

    cursor_path = memory_repo / ".snapshots" / ".codex-cursor.json"
    assert cursor_path.exists()
    saved_cursor = load_cursor(cursor_path)
    assert saved_cursor[session_id]["is_final"] is True

    index = memory_repo / "INDEX.md"
    assert "proj-e8faf588" in index.read_text()


def test_collect_missing_can_include_stub_summaries_for_repair(tmp_path):
    memory_repo = tmp_path / "memory"
    sessions_root = tmp_path / "sessions"
    memory_repo.mkdir()
    session_id = "019e777a-2222-7000-8000-bbbbbbbbbbbb"
    _write_stub_meta(memory_repo, session_id)
    _write_rollout(sessions_root, session_id)

    assert collect_missing_codex_rollouts(memory_repo, sessions_root) == []

    candidates = collect_missing_codex_rollouts(
        memory_repo, sessions_root, include_stub_summaries=True
    )
    assert [c.session_id for c in candidates] == [session_id]


def test_collect_missing_includes_partial_session_records(tmp_path):
    memory_repo = tmp_path / "memory"
    sessions_root = tmp_path / "sessions"
    memory_repo.mkdir()
    session_id = "019e777a-2222-7000-8000-bbbbbbbbbbbb"
    _write_partial_meta(memory_repo, session_id)
    _write_rollout(sessions_root, session_id)

    candidates = collect_missing_codex_rollouts(memory_repo, sessions_root)

    assert [c.session_id for c in candidates] == [session_id]


def test_backfill_candidate_normalizes_non_string_summary_fields(tmp_path):
    memory_repo = tmp_path / "memory"
    sessions_root = tmp_path / "sessions"
    memory_repo.mkdir()
    (memory_repo / ".snapshots").mkdir()
    session_id = "019e777a-2222-7000-8000-bbbbbbbbbbbb"
    _write_rollout(sessions_root, session_id)
    candidate = collect_missing_codex_rollouts(memory_repo, sessions_root)[0]

    session_dir = backfill_candidate(
        candidate,
        memory_repo,
        {},
        summarize_fn=lambda *args, **kwargs: {
            "summary": "Normalized session.",
            "decisions": {"items": ["a", "b"]},
            "artifacts": ["file one", "file two"],
            "excerpts": {"exchange": "hello"},
            "tags": ["backfill"],
        },
    )

    assert "items" in (session_dir / "decisions.md").read_text()
    assert "file one" in (session_dir / "artifacts.md").read_text()
    assert "exchange" in (session_dir / "excerpts.md").read_text()
