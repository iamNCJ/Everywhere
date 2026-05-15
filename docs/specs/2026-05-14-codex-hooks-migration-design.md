# Codex hooks migration design

**Date:** 2026-05-14
**Status:** Draft
**Replaces:** the launchd-sweeper portion of `2026-05-06-codex-support-design.md`

## Background

Codex CLI 0.130.0 promoted `hooks` to a stable feature. The set of supported
events is `SessionStart` (matchers: `startup`, `resume`), `UserPromptSubmit`,
`PreToolUse`, `PostToolUse`, `PermissionRequest`, `Stop`. There is **no
`SessionEnd`** equivalent (confirmed in OpenAI's migrate-to-codex differences
doc and the embedded `HookEventNameWire` enum). Hooks declared `async: true`
are silently skipped — all hooks run synchronously.

The current Everywhere Codex capture path is a launchd user agent
(`StartInterval = 300`) that polls `~/.codex/sessions/` every 5 minutes,
decides per-rollout whether to skip / snapshot / finalize, and finalizes
after 10 min of idle. This gives ~5 min capture latency and depends on
macOS-only `launchd` + a `~/.everywhere/hooks/` TCC workaround.

## Goal

Replace the launchd sweeper with native Codex hooks. Outcome:

- Sub-second latency for incremental snapshots (Stop fires at end of every turn).
- Finalization deferred to the next Codex launch — no scheduler needed.
- No `launchd` dependency → Linux / Windows installs become feasible.
- `~/.codex/config.toml` remains untouched (we never own `notify`).

## Non-goals

- Changing the Claude Code capture path (Stop + SessionEnd hooks stay as-is).
- Changing the memory repo layout, summarizer interface, or summarizer model.
- Removing `~/.everywhere/hooks/` staging — kept for stable absolute paths in
  dev-mode installs (where `${CODEX_PLUGIN_ROOT}` is not defined).
- Closing the gap that finalize doesn't run until the next Codex launch.
  Accepted trade-off; users who go offline for days won't see push until they
  return.

## Architecture

### Filesystem after migration

```
~/agent-memory/                                       (unchanged)
~/.everywhere/hooks/                                  (still staged)
  ├── snapshot.py                                     Claude entry (unchanged)
  ├── codex_hook.py                                   NEW — Codex hook entry
  ├── codex_sweep.py                                  reused logic, no launchd entry
  └── summarizer.py
~/.codex/hooks.json                                   NEW — merged on setup
~/Library/LaunchAgents/dev.everywhere.codex-sweeper.plist   REMOVED
```

### Hook registration

`~/.codex/hooks.json` after `/everywhere-codex-setup`. The `<HOME>` placeholder
below is replaced at setup time with the user's resolved home directory
(via `Path.home()` in Python). We bake in the absolute path rather than rely
on `$HOME` expansion because it's unclear whether Codex expands env vars
inside the `command` field.

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "nohup python3 -u <HOME>/.everywhere/hooks/codex_hook.py stop >/dev/null 2>&1 &",
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
            "command": "nohup python3 -u <HOME>/.everywhere/hooks/codex_hook.py finalize-sweep >/dev/null 2>&1 &",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

Both commands fork a detached process and return immediately. The hook itself
is therefore sync-safe; Codex's `async: true` skip-behavior does not affect us.

`timeout: 5` is a safety net — the fork itself takes milliseconds.

### `codex_hook.py` entry

Two subcommands. Both fail silently (errors → stderr, exit 0).

#### `stop`

1. Read hook payload from stdin (Codex emits `session_id`, `transcript_path`,
   `cwd`, `hook_event_name`).
2. Reuse `codex_sweep._handle_rollout` with a forced `action="incremental"`
   (skip the `decide_action` finality branch; debounce via the same
   `.codex-cursor.json`).
3. Update cursor; do **not** commit / push (finalize handles those).

#### `finalize-sweep`

1. Ignore stdin (SessionStart payload not needed for the sweep).
2. Take the existing `fcntl` lock on `.codex-sweep.lock`.
3. Iterate rollouts in the last `SCAN_DAYS=7` window.
4. For each, `decide_action` with mtime — only act on `finalize`; skip
   `incremental` (Stop owns that) and `skip`.
5. Commit + push when finalizing (unchanged from current `_handle_rollout`).

### Shared code

`codex_sweep.py` becomes a pure library:

- `_handle_rollout(rollout, cursor, now, memory_repo, *, allow_incremental, allow_finalize)`
  gains the two flags. `stop` calls it with `(True, False)`; `finalize-sweep`
  with `(False, True)`.
- `run_sweep()` is removed. Sweep semantics now live in
  `codex_hook.py finalize-sweep`.
- `snapshot.py`'s `--codex-sweep` CLI flag (and its `from hooks.codex_sweep
  import run_sweep` line) is removed. Anything that called it previously was
  the launchd plist, which is also removed.
- Cursor I/O (`load_cursor`, `save_cursor`), `_iter_rollouts`, `_log`, `_err`,
  `decide_action`, `_stub_summary` are kept as-is.

### Concurrency

Two cases where two processes could race on the same cursor file:

1. **Two Codex windows open simultaneously** — two Stop hooks fire, both touch
   cursor for *different* session_ids. The per-session-id keying already
   isolates updates; the fcntl lock on `.codex-sweep.lock` serializes the
   write-back.
2. **Stop and finalize-sweep racing** — Stop writes `incremental` state for
   session X; concurrent finalize-sweep sees X is still active (mtime fresh)
   and skips it. Lock prevents corrupt JSON write.

Both processes take the same `fcntl.LOCK_EX | LOCK_NB` lock. Non-blocking: if
held, the process exits 0 (current behavior).

## Setup / uninstall flow

### `/everywhere-codex-setup` (3 steps, down from 5)

1. **Detect & migrate from legacy launchd install.**
   - `launchctl print gui/$UID/dev.everywhere.codex-sweeper` → if present,
     `launchctl bootout gui/$UID ~/Library/LaunchAgents/dev.everywhere.codex-sweeper.plist`,
     then `rm -f` the plist. Log a one-line note that the launchd job was
     removed in favor of hooks.
2. **Stage scripts to `~/.everywhere/hooks/`.**
   - `mkdir -p ~/.everywhere/hooks` and copy `snapshot.py`, `codex_hook.py`,
     `codex_sweep.py`, `summarizer.py`, `__init__.py` from the plugin root.
   - Re-running setup refreshes the stage (overwrites).
3. **Merge into `~/.codex/hooks.json`.**
   - If the file exists, read it; otherwise start with `{"hooks": {}}`.
   - For each of `Stop` and `SessionStart`:
     - Remove any existing entry whose inner command contains
       `/.everywhere/hooks/codex_hook.py` (idempotent re-setup; the leading
       absolute prefix is the user's resolved `$HOME`).
     - Append our entry (template above, with `$HOME` resolved to the
       absolute path at write time).
   - Write back with `json.dump(..., indent=2)`.

### `/everywhere-codex-uninstall`

1. Read `~/.codex/hooks.json`. For each event, drop entries whose command
   contains `~/.everywhere/hooks/codex_hook.py`. If an event ends up with no
   entries, drop the event key. If `hooks` ends up empty, delete the file.
2. Best-effort `launchctl bootout` + `rm -f` the plist (for users upgrading
   from launchd installs who skip setup before uninstall).
3. `rm -rf ~/.everywhere/hooks/`.
4. The memory repo and session files are untouched.

## Plugin manifest (forward-looking)

Add `.codex-plugin/plugin.json` so Everywhere can be installed via Codex's
plugin marketplace in the future:

```json
{
  "name": "everywhere",
  "version": "1.1.0",
  "description": "Automatically persists every Codex CLI session to a searchable global memory repository synced to GitHub",
  "hooks": "./hooks/codex-hooks.json",
  "skills": "./skills/"
}
```

`hooks/codex-hooks.json` is the same template as the `~/.codex/hooks.json`
entries above, but with `$HOME/.everywhere/hooks/codex_hook.py` replaced by
`${CODEX_PLUGIN_ROOT}/hooks/codex_hook.py`. This file is **not used by
`/everywhere-codex-setup`** (which writes to `~/.codex/hooks.json` directly);
it exists solely for Codex plugin-marketplace installs.

## Documentation updates

- `README.md` — "How it works" table:
  - Row "launchd sweeper / Every 5 min" → "Stop hook / End of each turn /
    Debounced (10 min) snapshot".
  - Add row "SessionStart hook / Codex CLI / startup or resume / Finalize idle
    rollouts, commit + push".
  - Remove the "TCC staging" design note's launchd justification (keep the
    note about staging, reframe as "stable absolute path for dev-mode").
  - Drop `EVERYWHERE_CODEX_MODEL` instructions about editing the plist; point
    to `~/.codex/hooks.json`'s `env` field if/when needed (out of scope here —
    user can still set it shell-wide).
- `INSTALL.md` — replace the launchd / TCC / plist section (lines ~220–293)
  with the 3-step hook setup. Keep the "macOS only" caveat removed.
- `skills/everywhere/SKILL.md` — rewrite `/everywhere-codex-setup` and
  `/everywhere-codex-uninstall` sections to match. Delete the plist rendering,
  `launchctl bootstrap`, and troubleshooting subsections that reference
  `codex-sweep.err` / launchd `PATH`.

## Testing

New `tests/test_codex_hook.py`:

- **`stop` debounce** — invoke `codex_hook.py stop` twice in succession with
  the same payload; the second is debounced (cursor `last_snapshot_at`
  unchanged within `DEBOUNCE_SECONDS`).
- **`stop` writes incremental, never finalize** — verify cursor
  `is_final=false` and no `git_commit_push` call (mock the push).
- **`finalize-sweep` finalizes idle rollouts** — fixture rollout with mtime
  >`FINALITY_IDLE_SECONDS` in the past; assert `is_final=true` + push called.
- **`finalize-sweep` skips active rollouts** — fixture mtime within 10 min;
  assert no write.
- **hooks.json merge** — preserve unrelated user entries; idempotent re-run
  produces identical file.
- **hooks.json uninstall** — empty events removed; empty file deleted; user's
  unrelated hooks preserved.

Reuse `tests/fixtures/codex-rollout-*.jsonl` for transcript parsing. Existing
`test_cursor.py` and `test_finality.py` unchanged (they test
`decide_action`, cursor I/O, and `_handle_rollout` directly).

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| User has their own `~/.codex/hooks.json` we overwrite | Merge by command-string match; preserve all entries we don't own. Document the namespacing in SKILL.md. |
| User runs Codex < 0.130.0 (no hooks support) | Setup detects `codex features list \| grep -q "^hooks .* true"`; if missing, abort with a clear error pointing to upgrade. |
| Detached `nohup ... &` orphans on user logout | Acceptable — these are short-lived (a few seconds for incremental, up to a minute for finalize). Subprocess exits naturally; no daemonization. |
| `${CODEX_PLUGIN_ROOT}` not actually exported by current Codex | Plugin manifest path is forward-looking; primary install path uses `$HOME/.everywhere/hooks/` absolute. Verify the variable name in a smoke test before relying on it for marketplace installs. |
| Finalization waits indefinitely if user never reopens Codex | Accepted trade-off (called out in non-goals). Add a one-liner to README's design notes. |

## Open questions

None at the time of writing. Resolved during brainstorming:

- Hooks config location → `~/.codex/hooks.json` with merge, not `config.toml`.
- Async strategy → `nohup ... &` in the hook command, sync wrapper, real work
  in a detached process.
- Finalize trigger without `SessionEnd` → `SessionStart` of the *next* Codex
  launch sweeps idle rollouts.
- Keep or drop `~/.everywhere/hooks/` staging → keep, for dev-mode stable
  paths (no longer for TCC).
