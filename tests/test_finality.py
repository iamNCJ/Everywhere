from hooks.codex_sweep import decide_action, FINALITY_IDLE_SECONDS, DEBOUNCE_SECONDS


def test_skip_when_already_final():
    assert decide_action(mtime=100, now=200, cursor={"is_final": True, "last_snapshot_at": 100}) == "skip"


def test_finalize_when_idle_long_enough():
    # mtime is FINALITY_IDLE_SECONDS+1 ago, no prior cursor
    now = 1_000_000
    mtime = now - FINALITY_IDLE_SECONDS - 1
    assert decide_action(mtime=mtime, now=now, cursor=None) == "finalize"


def test_incremental_when_active_and_debounce_elapsed():
    now = 1_000_000
    mtime = now - 30  # very recent activity
    cursor = {"is_final": False, "last_snapshot_at": now - DEBOUNCE_SECONDS - 1}
    assert decide_action(mtime=mtime, now=now, cursor=cursor) == "incremental"


def test_skip_when_active_but_within_debounce():
    now = 1_000_000
    mtime = now - 30
    cursor = {"is_final": False, "last_snapshot_at": now - 60}  # snapshotted 1 min ago
    assert decide_action(mtime=mtime, now=now, cursor=cursor) == "skip"


def test_incremental_when_no_prior_cursor_and_active():
    now = 1_000_000
    mtime = now - 30
    assert decide_action(mtime=mtime, now=now, cursor=None) == "incremental"
