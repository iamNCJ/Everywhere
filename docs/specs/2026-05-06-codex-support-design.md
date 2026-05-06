# Everywhere — Codex Support Design

**Date:** 2026-05-06
**Status:** Approved (awaiting implementation plan)
**Predecessor:** `docs/specs/2026-04-29-session-memory-design.md`

## Goal

Extend Everywhere so that Codex CLI sessions are auto-captured into the same `~/agent-memory/` repo as Claude Code sessions, and so that the existing Everywhere commands (`/everywhere-setup`, `/recall`, `/memory on`, `/memory off`, `/session-save`) work from inside Codex.

The user-facing value is unchanged: one searchable cross-agent memory. Codex sessions show up alongside Claude sessions under the same project, with `meta.yaml.agent` distinguishing them.

## Non-goals

- Realtime / per-turn parity with Claude's `Stop` hook. A 5–10 minute snapshot delay is acceptable.
- Wrapping or replacing the `codex` binary.
- Touching `~/.codex/config.toml` `notify` (already taken by Codex Computer Use on this machine; risk of breaking other tools).
- A second summary pipeline. Codex sessions are summarized by Codex itself; Claude sessions remain summarized by `claude -p`. Both produce JSON in the same contract.

## Constraints discovered during brainstorming

1. **Codex has no `SessionEnd` hook.** Finality must be inferred from file mtime idleness.
2. **`notify` is a single-command field** in `~/.codex/config.toml` and is already in use on this machine. Cannot be safely overwritten by setup.
3. **Codex rollouts** live at `~/.codex/sessions/YYYY/MM/DD/rollout-<ISO>-<uuid>.jsonl`. First line is `type: session_meta` with `payload.{id, cwd, timestamp}`. Conversation is in `type: response_item, payload.type: "message"` entries.
4. **`codex exec --ephemeral`** is required when invoking Codex as a summarizer, otherwise the summarizer call itself produces a rollout that the sweeper would re-summarize on the next pass.
5. **Codex skills use the same `SKILL.md` format** as Claude Code (verified at `~/.codex/skills/*/SKILL.md`). The existing skill file can be reused with minimal changes.

## Architecture

```
Claude Code session ──→ Stop / SessionEnd hook ─────→ snapshot.py            (unchanged)
                                                       │
Codex session ──→ launchd every 5 min ──→ snapshot.py --codex-sweep
                                                       │
                                                       └── codex exec --ephemeral -m <cheap>  (summarizer)

  shared write target: ~/agent-memory/projects/<project>/sessions/<date>-<6char>/
    meta.yaml ── agent: claude-code | codex
    summary.md / decisions.md / artifacts.md / excerpts.md  (same five-file contract)
    PROJECT.md interleaves both agents by date
    INDEX.md and git push: unchanged
```

**Invariants preserved:**

- Five-file session output contract (`summary.md`, `decisions.md`, `artifacts.md`, `excerpts.md`, `meta.yaml`).
- Directory layout `projects/<project>/sessions/<date>-<short>/`.
- `PROJECT.md` / `INDEX.md` formats and update logic.
- Commit message format `session: <project> <short> - <headline>`.
- `/recall` query path (no schema change → no rebuild).

## Components

### C1. `snapshot.py --codex-sweep`

A new mode for the existing snapshot script. Does not read stdin (no hook payload). Logic:

1. **State file:** `~/agent-memory/.snapshots/.codex-cursor.json`
   ```json
   { "<session_id>": {
       "last_mtime": <unix>,
       "last_snapshot_at": <unix>,
       "is_final": <bool>,
       "retry_count": <int>
   } }
   ```
   Atomic write (tmp + rename). `retry_count` is incremented when summarization yields invalid JSON; after 3 retries the session is finalized with a stub summary (see C3 failure handling).
2. **Discovery:** Walk `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` for the **last 7 days** of date directories. (Avoids re-stat'ing thousands of old files every 5 minutes.)
3. **For each rollout file** with `mtime > cursor[id].last_snapshot_at` (or no cursor entry):
   - Parse → `(session_id, cwd, started_at, messages)` via the new Codex parser (C2).
   - Skip if `user_count < MIN_USER_MESSAGES` (3, same as Claude path).
   - Skip if `cursor[id].is_final == true` (already finalized).
   - **Finality:** if `now - mtime > FINALITY_IDLE_SECONDS` (600s) → write `is_final: true`, then `git commit + push` for **this session only** (same convention as the Claude path: one session per commit, headline = first sentence of summary). Set cursor `is_final = true`.
   - **Incremental:** else if `mtime > cursor[id].last_snapshot_at + DEBOUNCE_SECONDS` (600s) → write `is_final: false`, no commit/push, update cursor. The incremental files sit unstaged in the working tree until that session later finalizes (its own finalizing commit will pick them up via `git add -A`).
   - Else → skip.
4. **Error isolation:** any per-session failure logs to stderr (then to `codex-sweep.err`) but does not abort the sweep. Exit code is always 0.

**Single-instance guard:** acquire an exclusive `flock` on `~/agent-memory/.snapshots/.codex-sweep.lock` at start; if held, exit silently. Prevents two sweeps overlapping if one is slow.

### C2. Codex transcript parser

New function `parse_codex_transcript(path) -> (session_id, cwd, started_at, messages)`. Sits next to the existing `parse_transcript()` and returns the same shape so downstream code is agent-agnostic.

- First line: `type == "session_meta"` →
  - `session_id = payload.id`
  - `cwd = payload.cwd`
  - `started_at = payload.timestamp`
  If absent or malformed, skip the file.
- Subsequent lines: keep `type == "response_item"` AND `payload.type == "message"`.
  - `role = payload.role` (must be `user` or `assistant`).
  - Text = `" ".join(c["text"] for c in payload.content if c.get("type") in {"input_text", "output_text"})`.
- **Filter slash-command stubs:** drop user messages whose text matches `^\s*<command-name>` or `^\s*<local-command-stdout>`. These are Codex's slash-command bookkeeping, not real user input.
- Drop entries with empty text after extraction.

The existing `format_conversation()` and `MAX_CONVERSATION_CHARS` cap apply unchanged.

### C3. Summarizer abstraction

Refactor the current `call_claude(conversation, model) -> dict` into:

```python
summarize(conversation: str, agent: str, model: str | None = None) -> dict
```

Dispatch:

- `agent == "claude-code"` → existing `claude -p --output-format json` path. Default model `claude-haiku-4-5` (current `DEFAULT_MODEL`).
- `agent == "codex"` → new path:
  ```
  codex exec --ephemeral --skip-git-repo-check \
    -m <model> \
    --output-last-message <tmpfile> \
    "<SUMMARY_PROMPT>\n\n=== BEGIN_TRANSCRIPT ===\n<conversation>\n=== END_TRANSCRIPT ===\n\nNow produce the JSON summary..."
  ```
  Read `tmpfile` to get the assistant's final plain-text message. Strip ```json``` fences (reuse existing regex). `json.loads` → dict. Validate same `{summary, decisions, artifacts, excerpts, tags}` contract.
- `--ephemeral` is **required**. Without it, this very call writes a rollout into `~/.codex/sessions/`, which the next sweep would pick up and try to summarize.

**Default Codex summarizer model:** `gpt-5-mini`. Override via env var `EVERYWHERE_CODEX_MODEL` or via `~/agent-memory/.config.toml` key `codex_summarizer_model`. (Config file is read once at process start; not present → defaults apply.)

`SUMMARY_PROMPT` is reused verbatim — both summarizers must produce JSON with exactly `{summary, decisions, artifacts, excerpts, tags}` and no fences/prose.

### C4. launchd agent

Path: `~/Library/LaunchAgents/dev.everywhere.codex-sweeper.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.everywhere.codex-sweeper</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>python3</string>
    <string><ABS_PLUGIN_ROOT>/hooks/snapshot.py</string>
    <string>--codex-sweep</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string><HOME>/agent-memory/.snapshots/codex-sweep.log</string>
  <key>StandardErrorPath</key><string><HOME>/agent-memory/.snapshots/codex-sweep.err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict></plist>
```

The setup command substitutes `<ABS_PLUGIN_ROOT>` and `<HOME>` with absolute paths. `PATH` includes the homebrew prefixes so `codex` and `claude` are findable from launchd's reduced environment.

Load: `launchctl bootstrap gui/$UID <plist>`. Unload: `launchctl bootout gui/$UID/dev.everywhere.codex-sweeper`. Manual kick: `launchctl kickstart gui/$UID/dev.everywhere.codex-sweeper`.

(Linux note: the plist is macOS-only. A Linux equivalent — systemd user timer — is out of scope for this spec; the setup command will detect platform and tell Linux users the feature is not yet supported.)

### C5. `/everywhere-codex-setup` command

A new section appended to the existing `skills/everywhere/SKILL.md`. Steps:

1. **Prerequisites.** Check `codex --version` and `test -d ~/agent-memory/.git`. If memory repo missing, instruct user to run `/everywhere-setup` first and exit.
2. **Locate the plugin directory.** Same logic as the current `/everywhere-setup` (try `~/.claude/plugins/cache/*everywhere*/hooks/snapshot.py`, else ask).
3. **Write the plist** to `~/Library/LaunchAgents/dev.everywhere.codex-sweeper.plist` with `<ABS_PLUGIN_ROOT>` and `<HOME>` substituted.
4. **Bootstrap:** `launchctl bootstrap gui/$UID <plist>`. If a previous label exists, `launchctl bootout` first then re-bootstrap.
5. **Verify:** `launchctl print gui/$UID/dev.everywhere.codex-sweeper | head` — surface the result to the user.
6. **Kick once:** `launchctl kickstart gui/$UID/dev.everywhere.codex-sweeper` so the first sweep runs immediately and the user sees output in `codex-sweep.log`.
7. **Report:** plist path, log path, scan interval (5 min), idle threshold for finality (10 min).

A symmetric `/everywhere-codex-uninstall` section: `launchctl bootout` and delete the plist.

### C6. Codex-side command support

The existing `skills/everywhere/SKILL.md` is platform-neutral except for the hook-registration steps in `/everywhere-setup`. Plan:

- During `/everywhere-setup`, after handling the Claude side, detect `codex --version`. If present, ask the user (default yes) whether to install the Codex-side skill.
- If yes: copy `skills/everywhere/SKILL.md` to `~/.codex/skills/everywhere/SKILL.md`. (Codex skills format is verified compatible.)
- The skill's `description` line is broadened to include Codex command names and historical-search triggers, so both agents activate it correctly.
- `/recall`, `/memory on`, `/memory off` semantics are identical in either agent — they only read `~/agent-memory/` files.
- `/session-save` in Codex: locate the most recent `~/.codex/sessions/.../rollout-*.jsonl` whose `payload.cwd` matches `$PWD`, then run a single-session pass through the same C1 logic with debounce bypassed and `is_final: true` forced.

## Data flow — incremental Codex snapshot (happy path)

1. User has a Codex session open in `/Users/ncj/Documents/workspace/dev/foo`. Codex has written 12 turns to `rollout-...-abc123.jsonl`. File mtime updates after each turn.
2. launchd fires `snapshot.py --codex-sweep` at minute 5.
3. flock acquired. Cursor file loaded.
4. Walk last 7 days of `~/.codex/sessions/`. `abc123.jsonl` mtime is 30s old → newer than cursor's `last_snapshot_at` (unset).
5. Parse: 12 messages, 6 user, `cwd = .../foo`, `started_at = 2026-05-06T...`.
6. `now - mtime = 30s` → not idle enough for final. `mtime > last_snapshot_at + 600s` (vacuously true) → incremental snapshot.
7. `summarize(..., agent="codex", model="gpt-5-mini")` → JSON.
8. Write five files into `projects/foo/sessions/2026-05-06-abc123/`. `meta.yaml` has `agent: codex`, `is_final: false`, `snapshot_count: 0`.
9. Update `PROJECT.md` and `INDEX.md`.
10. Update cursor: `last_mtime`, `last_snapshot_at = now`, `is_final: false`. **No git commit/push** this sweep — files sit modified in the working tree until the next sweep finalizes something.
11. flock released.

## Data flow — finality (later sweep)

1. User closed Codex an hour ago. `abc123.jsonl` mtime is 1h old.
2. Sweeper finds the file. `now - mtime > 600s` → finality branch.
3. Re-parse + re-summarize at the final state. Write five files (overwrites incremental). `meta.yaml` now has `is_final: true`, `ended_at = now`, `snapshot_count: 1` (mirrors Claude path's accounting).
4. `git add -A && git commit -m "session: <project> <short> - <headline>" && git push` — same format as the Claude path. `git add -A` here also picks up any incremental files of *other* still-open Codex sessions; that's intentional and harmless (those will be overwritten when their own finalizing commit lands later — the diff just walks forward).
5. Update cursor `is_final: true`. Future sweeps skip this file forever.

## Failure modes and behavior

| Scenario | Behavior |
|---|---|
| `codex exec` not on PATH from launchd | Log to `.err`, skip session; user sees error in `codex-sweep.err`. Setup command pre-flights this. |
| `codex exec` returns non-JSON or wrong shape | Log, skip session, leave cursor unchanged so a later sweep can retry once Codex output stabilizes. After 3 retries (tracked in cursor), mark as `is_final: true` with a stub summary so we don't loop forever on a broken file. |
| Sweep runs while previous sweep still going | flock denies, second invocation exits silently. |
| Cursor file corrupted | Log, rename to `.codex-cursor.json.bad-<ts>`, start fresh. Already-snapshotted sessions get re-snapshotted on this one sweep — annoying but not destructive (overwrite is idempotent). |
| `~/.codex/sessions/` missing | Empty walk, no-op. |
| Memory repo `.git` missing | Same as Claude path: log error, exit. Setup command pre-flights. |
| Network down during final push | Same as current Claude path: commit kept locally, retried on next final push. |
| User uninstalls plugin without `/everywhere-codex-uninstall` | plist still loaded, points at deleted snapshot.py → fails harmlessly to `.err`. Document this in the uninstall section. |

## Configuration surface

`~/agent-memory/.config.toml` (new file, optional):

```toml
codex_summarizer_model = "gpt-5-mini"  # default if omitted
codex_sweep_interval_seconds = 300     # only used if user regenerates the plist
codex_finality_idle_seconds = 600
```

Env var `EVERYWHERE_CODEX_MODEL` overrides the model. `EVERYWHERE_DEBUG=1` (existing) verboses logging from the sweep.

## Out-of-scope / follow-ups

- Linux (`systemd --user` timer equivalent of the launchd plist).
- A `notify`-based realtime path (only worth doing if a future user has no `notify` collision).
- Importing historical Codex sessions older than 7 days at install time. Setup could optionally do a one-time deep walk; deferred.
- Cross-agent thread linking (e.g., a Codex session and a Claude session that worked on the same project on the same day). The current PROJECT.md interleaving is enough for `/recall` to surface both; deeper joining is future work.
- A Codex-native plugin package (instead of dropping the SKILL.md into `~/.codex/skills/`). The skill copy is the simplest viable path for now.

## Acceptance criteria

1. With Codex installed and `/everywhere-codex-setup` run, opening a Codex session in a workspace and exchanging ≥3 user messages results, within ~10 minutes of the last turn, in a finalized session directory under `~/agent-memory/projects/<project>/sessions/`. `meta.yaml.agent == "codex"`.
2. The session is committed and pushed to the same remote as Claude sessions, with the same commit message format.
3. `/recall <topic>` (run from either Claude Code or Codex) surfaces matching Codex sessions alongside Claude sessions, ranked by the same logic.
4. Running the sweeper a second time on an already-finalized session does not re-summarize or re-commit it (cursor `is_final: true` short-circuits).
5. `~/.codex/config.toml`'s `notify` setting is **unchanged** by the setup process. The existing Computer Use binding still fires.
6. `/everywhere-codex-uninstall` removes the plist and unloads the launchd job; subsequent `launchctl print` shows the label is gone.
