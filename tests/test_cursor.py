import json
from pathlib import Path

from hooks.codex_sweep import load_cursor, save_cursor


def test_load_returns_empty_dict_when_missing(tmp_path):
    assert load_cursor(tmp_path / "missing.json") == {}


def test_save_then_load_roundtrip(tmp_path):
    p = tmp_path / "cursor.json"
    data = {"sid-1": {"last_mtime": 100, "last_snapshot_at": 110, "is_final": False, "retry_count": 0}}
    save_cursor(p, data)
    assert load_cursor(p) == data


def test_save_is_atomic_via_tmp_rename(tmp_path):
    p = tmp_path / "cursor.json"
    save_cursor(p, {"a": {"last_mtime": 1, "last_snapshot_at": 1, "is_final": False, "retry_count": 0}})
    # No leftover .tmp file.
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


def test_load_recovers_from_corrupted_file(tmp_path):
    p = tmp_path / "cursor.json"
    p.write_text("{ this is not json")
    # Should NOT raise. Returns {} and renames the bad file out of the way.
    assert load_cursor(p) == {}
    bad_files = list(tmp_path.glob("cursor.json.bad-*"))
    assert len(bad_files) == 1
