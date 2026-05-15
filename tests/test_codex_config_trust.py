"""Tests for ~/.codex/config.toml [hooks.state] pre-trust logic."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from hooks.codex_hook import (
    install_hooks_json,
    trust_hooks_in_config,
    untrust_hooks_in_config,
    _hash_command,
)

STAGED = "/home/u/.everywhere/hooks"


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    hooks_path = tmp_path / "hooks.json"
    config_path = tmp_path / "config.toml"
    install_hooks_json(hooks_path, staged_dir=STAGED)
    return config_path, hooks_path


def test_trust_creates_blocks_when_config_missing(tmp_path):
    config_path, hooks_path = _setup(tmp_path)
    assert not config_path.exists()

    trust_hooks_in_config(config_path, hooks_path)

    text = config_path.read_text()
    # Both events get a state block.
    assert f'[hooks.state."{hooks_path}:stop:0:0"]' in text
    assert f'[hooks.state."{hooks_path}:session_start:0:0"]' in text
    # Each block carries enabled + trusted_hash.
    assert text.count("enabled = true") >= 2
    assert text.count("trusted_hash = \"sha256:") == 2
    # Parent namespace header exists.
    assert "[hooks.state]" in text


def test_trust_hash_matches_sha256_of_command(tmp_path):
    config_path, hooks_path = _setup(tmp_path)
    trust_hooks_in_config(config_path, hooks_path)

    hooks = json.loads(hooks_path.read_text())
    cfg_text = config_path.read_text()
    for event, key_suffix in (("Stop", "stop:0:0"), ("SessionStart", "session_start:0:0")):
        cmd = hooks["hooks"][event][0]["hooks"][0]["command"]
        expected = "sha256:" + hashlib.sha256(cmd.encode()).hexdigest()
        # Find the block for this key and verify trusted_hash inside it.
        block_re = re.compile(
            rf'\[hooks\.state\."{re.escape(str(hooks_path))}:{key_suffix}"\][^\[]*?trusted_hash = "([^"]+)"',
            re.DOTALL,
        )
        m = block_re.search(cfg_text)
        assert m is not None, f"missing trust block for {event}"
        assert m.group(1) == expected


def test_trust_is_idempotent(tmp_path):
    config_path, hooks_path = _setup(tmp_path)
    trust_hooks_in_config(config_path, hooks_path)
    first = config_path.read_text()

    trust_hooks_in_config(config_path, hooks_path)
    second = config_path.read_text()

    assert first == second


def test_trust_preserves_other_config_keys(tmp_path):
    config_path, hooks_path = _setup(tmp_path)
    config_path.write_text(
        'model = "gpt-5.5"\n'
        'notify = ["/path/to/notify", "turn-ended"]\n'
        "\n"
        "[features]\n"
        "multi_agent = true\n"
        "\n"
        '[projects."/some/path"]\n'
        'trust_level = "trusted"\n'
    )

    trust_hooks_in_config(config_path, hooks_path)

    text = config_path.read_text()
    assert 'model = "gpt-5.5"' in text
    assert 'notify = ["/path/to/notify", "turn-ended"]' in text
    assert "[features]" in text
    assert "multi_agent = true" in text
    assert '[projects."/some/path"]' in text
    assert 'trust_level = "trusted"' in text


def test_trust_preserves_unrelated_hooks_state_entries(tmp_path):
    config_path, hooks_path = _setup(tmp_path)
    other_key = "/some/other/hooks.json:pretool_use:0:0"
    config_path.write_text(
        "[hooks.state]\n"
        "\n"
        f'[hooks.state."{other_key}"]\n'
        "enabled = true\n"
        'trusted_hash = "sha256:abcdef0123"\n'
    )

    trust_hooks_in_config(config_path, hooks_path)

    text = config_path.read_text()
    # User's entry survived.
    assert f'[hooks.state."{other_key}"]' in text
    assert 'trusted_hash = "sha256:abcdef0123"' in text
    # Our entries were added.
    assert f'[hooks.state."{hooks_path}:stop:0:0"]' in text


def test_trust_refreshes_hash_when_command_changes(tmp_path):
    config_path, hooks_path = _setup(tmp_path)
    trust_hooks_in_config(config_path, hooks_path)
    first = config_path.read_text()

    # Re-stage with a different staged_dir (must still contain the marker
    # so install_hooks_json's idempotency check recognizes it as ours).
    install_hooks_json(hooks_path, staged_dir="/different/parent/.everywhere/hooks")
    trust_hooks_in_config(config_path, hooks_path)
    second = config_path.read_text()

    assert first != second
    # New hash matches new command.
    hooks = json.loads(hooks_path.read_text())
    cmd = hooks["hooks"]["Stop"][0]["hooks"][0]["command"]
    new_hash = _hash_command(cmd)
    assert new_hash in second
    # Exactly two trusted_hash lines (no stale duplicates left behind).
    assert second.count("trusted_hash = \"sha256:") == 2


def test_untrust_removes_our_entries_only(tmp_path):
    config_path, hooks_path = _setup(tmp_path)
    other_key = "/some/other/hooks.json:pretool_use:0:0"
    # Seed config with one of ours + one unrelated.
    config_path.write_text(
        'model = "gpt-5.5"\n'
        "\n"
        "[hooks.state]\n"
        "\n"
        f'[hooks.state."{other_key}"]\n'
        "enabled = true\n"
        'trusted_hash = "sha256:abcdef0123"\n'
    )
    trust_hooks_in_config(config_path, hooks_path)
    assert f'"{hooks_path}:stop:0:0"' in config_path.read_text()

    untrust_hooks_in_config(config_path, hooks_path)

    text = config_path.read_text()
    assert 'model = "gpt-5.5"' in text
    assert f'[hooks.state."{other_key}"]' in text
    assert 'trusted_hash = "sha256:abcdef0123"' in text
    assert f'"{hooks_path}:stop:0:0"' not in text
    assert f'"{hooks_path}:session_start:0:0"' not in text


def test_untrust_drops_namespace_header_when_no_entries_remain(tmp_path):
    config_path, hooks_path = _setup(tmp_path)
    config_path.write_text('model = "gpt-5.5"\n')
    trust_hooks_in_config(config_path, hooks_path)
    assert "[hooks.state]" in config_path.read_text()

    untrust_hooks_in_config(config_path, hooks_path)

    text = config_path.read_text()
    assert "[hooks.state]" not in text
    assert 'model = "gpt-5.5"' in text


def test_untrust_missing_config_is_noop(tmp_path):
    config_path = tmp_path / "missing.toml"
    hooks_path = tmp_path / "h.json"
    untrust_hooks_in_config(config_path, hooks_path)  # must not raise
    assert not config_path.exists()


def test_trust_noop_when_hooks_file_missing(tmp_path):
    config_path = tmp_path / "config.toml"
    hooks_path = tmp_path / "missing.json"
    trust_hooks_in_config(config_path, hooks_path)
    assert not config_path.exists()
