from pathlib import Path

from hooks.snapshot import parse_codex_transcript

FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_session_meta_and_messages():
    session_id, cwd, started_at, messages = parse_codex_transcript(
        FIXTURES / "codex-rollout-normal.jsonl"
    )
    assert session_id == "019de111-1111-2222-3333-444444444444"
    assert cwd == "/Users/test/proj"
    assert started_at == "2026-05-06T10:00:00.000Z"
    assert len(messages) == 5
    assert messages[0] == {"role": "user", "text": "hello codex"}
    assert messages[1] == {"role": "assistant", "text": "hi there"}
    assert messages[-1] == {"role": "user", "text": "third"}


def test_skips_event_msg_and_turn_context():
    _, _, _, messages = parse_codex_transcript(
        FIXTURES / "codex-rollout-normal.jsonl"
    )
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
