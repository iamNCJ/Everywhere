---
name: everywhere
description: Use when the user invokes /everywhere-setup, /everywhere-codex-setup, /everywhere-codex-uninstall, /session-save, /recall, /memory on, or /memory off. Also use when the user asks to search past sessions, access historical context across Claude Code or Codex CLI, save the current session to memory, or set up automatic Codex session capture.
---

# Everywhere

> *Your agent sessions, accessible everywhere.*

Everywhere persists Claude Code and Codex CLI sessions to `~/agent-memory/` synced to GitHub. For Claude, run `/everywhere-setup` to register Stop/SessionEnd hooks. For Codex, run `/everywhere-codex-setup` to register Codex `Stop` and `SessionStart` hooks that capture each turn and finalize idle sessions on the next launch. Both feed the same memory repo; `meta.yaml.agent` distinguishes the source. Use the commands below for manual control and retrieval.

## Commands

### `/everywhere-setup`

First-time setup. Run this once to initialize the memory repo and connect it to GitHub.

#### 1. Check prerequisites

```bash
git --version
gh auth status
claude --version
```

- If `gh auth status` fails: tell the user to run `gh auth login` and re-invoke `/everywhere-setup`.
- If `git` or `claude` are missing: tell the user to install them before continuing.

#### 2. Initialize the memory repo

Check whether `~/agent-memory/.git` already exists:

```bash
test -d ~/agent-memory/.git && echo "exists" || echo "missing"
```

If missing, run:

```bash
mkdir -p ~/agent-memory && git -C ~/agent-memory init && git -C ~/agent-memory branch -m main
mkdir -p ~/agent-memory/.snapshots ~/agent-memory/global ~/agent-memory/projects
```

Then create the following files (skip if they already exist):

**`~/agent-memory/.gitignore`:**
```
.snapshots/
.DS_Store
```

**`~/agent-memory/INDEX.md`:**
```markdown
# Everywhere — Global Memory Index

> Auto-maintained by Everywhere. Last updated by SessionEnd hook.

## Projects

```

**`~/agent-memory/global/cross-session-insights.md`:**
```markdown
# Cross-Session Insights

> Patterns and learnings that span multiple projects or sessions.

```

Then make the initial commit:

```bash
git -C ~/agent-memory add -A
git -C ~/agent-memory commit -m "init: everywhere memory repo"
```

#### 3. Create GitHub repo and configure remote

Check if the remote is already configured:

```bash
git -C ~/agent-memory remote get-url origin 2>/dev/null && echo "configured" || echo "missing"
```

If not configured:

```bash
gh repo create everywhere-memory --private --description "Everywhere session memory" 2>/dev/null || true
REMOTE_URL=$(gh repo view everywhere-memory --json sshUrl -q .sshUrl)
git -C ~/agent-memory remote add origin "$REMOTE_URL"
git -C ~/agent-memory push -u origin main
```

If `gh repo create` fails because the repo already exists, just get the URL and set the remote.

#### 4. Register hooks in ~/.claude/settings.json

The plugin ships its own `hooks/hooks.json` registering `Stop` and `SessionEnd` as `type: command + async: true`. **If the user has installed Everywhere via the plugin marketplace, they don't need to register hooks manually — enabling the plugin loads `hooks.json` automatically.** Detect that case first:

```bash
python3 -c "
import json, sys
try:
    with open('$HOME/.claude/plugins/installed_plugins.json') as f:
        d = json.load(f)
    plugins = d.get('plugins', {})
    everywhere_keys = [k for k in plugins if k.startswith('everywhere@')]
    if everywhere_keys:
        print('plugin-installed:' + everywhere_keys[0])
    else:
        print('not-installed')
except FileNotFoundError:
    print('not-installed')
"
```

If `plugin-installed`, tell the user: "Plugin already enabled — hooks load automatically from `hooks/hooks.json`. Just run `/hooks` to reload, or start a fresh session."

Otherwise (development install or not yet on a marketplace), register the hooks directly in `~/.claude/settings.json` using **absolute paths** to this plugin's `snapshot.py`. Find the script first:

```bash
# 1. If the plugin lives under ~/.claude/plugins/cache/, prefer that path:
find ~/.claude/plugins/cache -path "*everywhere*/hooks/snapshot.py" 2>/dev/null | head -1
# 2. Otherwise, ask the user where they cloned the repo and use that hooks/snapshot.py path.
```

Read `~/.claude/settings.json` (create if missing), merge the hook block below (preserving every other setting), and write back. **Use absolute paths** in the `command` field — `${CLAUDE_PLUGIN_ROOT}` only resolves for plugins loaded via marketplace.

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "python3 \"<ABS_PATH>/hooks/snapshot.py\"",
        "async": true,
        "timeout": 120,
        "statusMessage": "Everywhere: snapshotting session..."
      }]
    }],
    "SessionEnd": [{
      "hooks": [{
        "type": "command",
        "command": "python3 \"<ABS_PATH>/hooks/snapshot.py\"",
        "async": true,
        "timeout": 180,
        "statusMessage": "Everywhere: finalizing session memory..."
      }]
    }]
  }
}
```

Replace `<ABS_PATH>` with the actual plugin directory. Validate JSON after writing:

```bash
python3 -c "import json; json.load(open('$HOME/.claude/settings.json')); print('valid')"
```

Tell the user: "Hooks registered (`type: command`, `async: true`). They take effect on the next session or after running `/hooks` to reload."

**Why this design:** `type: agent` is documented as experimental. `type: command` runs a deterministic shell call to `snapshot.py`, which uses `claude -p` only for the summary-generation step. `async: true` makes the hook survive parent CC exit (so `/exit` and Ctrl-C reliably get a final snapshot).

#### 5. Print setup summary

Report:
- Memory repo location: `~/agent-memory/`
- GitHub repo: output of `gh repo view everywhere-memory --json url -q .url`
- Hooks: registered in `~/.claude/settings.json` (Stop + SessionEnd, Haiku model, async)
- What happens next: hooks fire automatically on every session; use `/session-save` to trigger manually.

### `/everywhere-codex-setup`

Registers Codex CLI `Stop` and `SessionStart` hooks that auto-capture sessions into the same memory repo. Cross-platform (anywhere Codex CLI runs).

#### 1. Check prerequisites

```bash
codex --version
codex features list 2>/dev/null | grep -q "^hooks .*true" && echo "hooks-ok" || echo "hooks-missing"
test -d ~/agent-memory/.git && echo "repo-ok" || echo "repo-missing"
```

If `codex` is missing: tell the user to install Codex CLI first and re-invoke.
If `hooks-missing`: Codex < 0.130.0 — tell the user to upgrade (`codex update`) and re-invoke.
If `repo-missing`: tell the user to run `/everywhere-setup` first and exit.

#### 2. Migrate from legacy launchd install (best-effort)

If the user previously installed Everywhere via the launchd sweeper, unload it:

```bash
LABEL=dev.everywhere.codex-sweeper
if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "gui/$UID" "$HOME/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || true
  echo "[migrate] removed legacy launchd job $LABEL"
fi
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
```

If the plist was found, tell the user the legacy launchd job was removed in favor of native Codex hooks.

#### 3. Locate the plugin directory

```bash
find ~/.claude/plugins/cache -path "*everywhere*/hooks/snapshot.py" 2>/dev/null | head -1
```

If the search returns a path: that's `<ABS_PLUGIN_ROOT>` (drop the trailing `/hooks/snapshot.py`).
Else: ask the user where they cloned the plugin and use that path.

#### 4. Stage hooks to `~/.everywhere/hooks/`

Codex hooks fire under the user (no TCC restrictions), but staging keeps the registered `command` strings in `~/.codex/hooks.json` pointed at a stable absolute path even in dev-mode installs.

```bash
PLUGIN=<ABS_PLUGIN_ROOT>
mkdir -p ~/.everywhere/hooks
cp "$PLUGIN/hooks/snapshot.py" "$PLUGIN/hooks/codex_hook.py" \
   "$PLUGIN/hooks/codex_sweep.py" "$PLUGIN/hooks/summarizer.py" \
   "$PLUGIN/hooks/__init__.py" \
   ~/.everywhere/hooks/
```

Re-running `/everywhere-codex-setup` refreshes these copies (run after plugin updates).

#### 5. Merge entries into `~/.codex/hooks.json`

```bash
PYTHONPATH="$HOME/.everywhere" python3 -c "
from pathlib import Path
from hooks.codex_hook import install_hooks_json
install_hooks_json(Path.home() / '.codex' / 'hooks.json',
                   staged_dir=str(Path.home() / '.everywhere' / 'hooks'))
print('ok')
"
```

Verify:

```bash
python3 -c "import json; print(json.dumps(json.load(open('$HOME/.codex/hooks.json')), indent=2))" | head -40
```

Expected: two entries under `"hooks"` (`Stop` and `SessionStart`), each command line pointing at `~/.everywhere/hooks/codex_hook.py`. Any pre-existing user-owned hook entries are preserved.

#### 6. Smoke test

Ask the user to open a Codex session and run at least one turn (then `/exit` or just leave it). After a turn fires, this file should appear:

```bash
ls -lt $HOME/agent-memory/.snapshots/.codex-cursor.json 2>/dev/null
```

If you want to see hook activity in real time, set `EVERYWHERE_DEBUG=1` in the shell where Codex runs.

#### 7. Optionally install the Codex-side skill

```bash
mkdir -p ~/.codex/skills/everywhere
cp $PLUGIN/skills/everywhere/SKILL.md ~/.codex/skills/everywhere/SKILL.md
```

This lets `/recall`, `/memory on`, etc. work from inside Codex too. Skip if the user prefers Claude-only invocation.

#### 8. Report to the user

Tell the user:
- Staged hooks: `~/.everywhere/hooks/` (re-run setup after plugin updates)
- Registered events: `Stop` (per-turn debounced snapshot) + `SessionStart` (finalize idle rollouts on next launch)
- Hooks file: `~/.codex/hooks.json` (merged — your other hook entries preserved)
- Finalization runs on the **next** Codex launch (Codex has no `SessionEnd` event); if you stop using Codex for a while, the last in-progress rollout won't push until you launch Codex again
- Codex `notify` setting and `~/.codex/config.toml` were **not** modified
- Codex skill: installed at `~/.codex/skills/everywhere/SKILL.md` (if step 7 ran)

### `/everywhere-codex-uninstall`

Removes Codex hook registrations and staged scripts. The memory repo and existing session files are untouched.

#### 1. Remove hook entries from `~/.codex/hooks.json`

```bash
PYTHONPATH="$HOME/.everywhere" python3 -c "
from pathlib import Path
from hooks.codex_hook import uninstall_hooks_json
uninstall_hooks_json(Path.home() / '.codex' / 'hooks.json')
print('ok')
"
```

Entries whose command does **not** reference `~/.everywhere/hooks/codex_hook.py` are preserved. Empty events are dropped; if `hooks.json` ends up empty, the file is deleted.

#### 2. Best-effort launchd cleanup (for users upgrading from the old install)

```bash
LABEL=dev.everywhere.codex-sweeper
launchctl bootout "gui/$UID" "$HOME/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
```

#### 3. Remove staged scripts

```bash
rm -rf ~/.everywhere/hooks
```

#### 4. Optionally remove the Codex-side skill

```bash
rm -rf ~/.codex/skills/everywhere
```

Tell the user that re-running `/everywhere-codex-setup` will re-register and resume capture. The cursor in `~/agent-memory/.snapshots/.codex-cursor.json` is preserved across uninstall/reinstall (no re-summarization of past sessions).

### `/session-save`

Manually trigger a full session save right now, following the same logic as the `session-end.sh` hook.

#### 1. Find the current session transcript

Detect which agent we're running in. If the environment variable `CODEX_HOME` is set, or `~/.codex/sessions/` exists and a recent rollout matches the current `cwd`, treat this as a Codex session. Otherwise, treat it as a Claude Code session.

**Claude Code:**
```bash
ls -t ~/.claude/projects/$(echo "$PWD" | sed 's|^/||; s|/|-|g')/*.jsonl 2>/dev/null | head -1
```

**Codex CLI:**
```bash
python3 - <<'PY'
import json, os, sys
from pathlib import Path

cwd = os.environ["PWD"]
root = Path.home() / ".codex" / "sessions"
candidates = sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
for p in candidates[:50]:
    try:
        with open(p) as f:
            first = f.readline()
        meta = json.loads(first)
        if meta.get("type") == "session_meta" and meta.get("payload", {}).get("cwd") == cwd:
            print(p)
            sys.exit(0)
    except Exception:
        continue
sys.exit(1)
PY
```

If Codex case: kick the finalize sweep across all recent rollouts (it will
finalize idle ones, including the current session if you've already exited
the turn). For the current still-active session, this acts like an
incremental snapshot — the next Codex `SessionStart` will finalize it.

```bash
PYTHONPATH="$HOME/.everywhere" EVERYWHERE_DEBUG=1 \
  python3 "$HOME/.everywhere/hooks/codex_hook.py" finalize-sweep
```

If `~/.everywhere/hooks/` doesn't exist (the user hasn't run
`/everywhere-codex-setup` yet), fall back to running directly from the
plugin source:

```bash
PYTHONPATH=<ABS_PLUGIN_ROOT> EVERYWHERE_DEBUG=1 \
  python3 <ABS_PLUGIN_ROOT>/hooks/codex_hook.py finalize-sweep
```

Then verify the latest finalized session for this project:

```bash
SLUG=$(python3 -c "import hashlib, os; p = os.path.abspath(os.environ['PWD']); print(f\"{os.path.basename(p) or 'unknown'}-{hashlib.sha256(p.encode()).hexdigest()[:8]}\")")
ls -t ~/agent-memory/projects/"$SLUG"/sessions/ | head -1
```

For the Claude case, continue with the original step-by-step logic below.

#### 2. Extract project info from the transcript

Read the first lines of the JSONL file to find a line with a `cwd` field:
- `project_path` = the value of `cwd`
- `project_name` = `{basename}-{hash8}` where `basename` is `basename(project_path)` and `hash8` is the first 8 hex chars of `sha256(abspath(project_path))` — disambiguates folders sharing a basename
- `session_id` = stem of the JSONL filename (filename without `.jsonl`)
- `started_at` = ISO 8601 timestamp from the first JSONL entry that has a `timestamp` field
- `session_id_short` = first 6 characters of `session_id`
- `date_str` = first 10 characters of `started_at` (e.g. `2026-04-29`)

#### 3. Parse the conversation

Read the full JSONL. Collect messages:
- `user` entries: `message.content` as a string (or join text blocks from a list)
- `assistant` entries: join text blocks from `message.content`

If fewer than 3 user messages are found, tell the user the session is too short to save and exit.

#### 4. Write session files

Directory: `~/agent-memory/projects/{project_name}/sessions/{date_str}-{session_id_short}/`

Create the directory. Write these files (overwrite if they exist):

**`meta.yaml`:** Quote all string values. Use `snapshot_count: 1` if a prior `meta.yaml` exists for this session, else `0`.
```yaml
session_id: "{session_id}"
session_id_short: "{session_id_short}"
project_path: "{project_path}"
project_name: "{project_name}"
agent: claude-code
started_at: "{started_at}"
ended_at: "{current ISO 8601 timestamp}"
is_final: true
snapshot_count: {0 or 1}
tags: [{5-8 relevant tags as YAML inline sequence}]
```

**`summary.md`:** 3–5 sentences covering: what problem was solved, approach taken, outcome, anything left incomplete.

**`decisions.md`:** All technical decisions and conclusions. Group under sub-headings if more than 5.

**`artifacts.md`:**
```markdown
## Files
- `path/to/file` — what was done

## Commands
- `command` — what it accomplished

## References
- PRs, issues, docs, or URLs mentioned
```

**`excerpts.md`:** The 3–5 most valuable Q&A exchanges, verbatim. Quote the user's message and assistant's response exactly — do not paraphrase.

#### 5. Update PROJECT.md

File: `~/agent-memory/projects/{project_name}/PROJECT.md`

Create with this template if it doesn't exist:
```markdown
# {project_name}

**Path:** {project_path}

## Recent Sessions

<!-- Sessions listed below, newest first. Maintained by Everywhere. -->

## Project Overview

<!-- Updated manually or by /recall when patterns emerge -->
```

Prepend a new entry under `## Recent Sessions` (after the comment line):
```markdown
- [{date_str} — {first sentence of summary}](sessions/{date_str}-{session_id_short}/summary.md)
```

If there are more than 10 session entries, remove the oldest so only 10 remain.

#### 6. Update INDEX.md

File: `~/agent-memory/INDEX.md`

Create if it doesn't exist:
```markdown
# Everywhere — Global Memory Index

> Auto-maintained by Everywhere. Last updated by SessionEnd hook.

## Projects

```

If `{project_name}` is not already listed under `## Projects`, append:
```markdown
- [{project_name}](projects/{project_name}/PROJECT.md) — {project_path}
```

#### 7. Git commit and push

```bash
git -C ~/agent-memory add -A
git -C ~/agent-memory commit -m "session: {project_name} {session_id_short} - {first sentence of summary}"
git -C ~/agent-memory push origin main
```

If push fails (no remote or network error), commit locally and report the error — do not fail noisily.

#### 8. Report to user

Tell the user: "Session saved. Files written to `~/agent-memory/projects/{project_name}/sessions/{date_str}-{session_id_short}/` and pushed to GitHub."

### `/recall [query]`

Search the memory repo for sessions matching the query and return ranked results.

#### 1. Search session content

```bash
rg -l "{query}" ~/agent-memory/projects/ --glob="*.md" 2>/dev/null | head -20
```

#### 2. Search tags

```bash
rg "{query}" ~/agent-memory/projects/ --glob="meta.yaml" 2>/dev/null | head -20
```

#### 3. Deduplicate and rank results

Combine both result sets. Deduplicate by session directory. Prefer results where the match appeared in `summary.md` or `meta.yaml` tags over body matches in `decisions.md` or `artifacts.md`.

#### 4. Read top results

For the top 5 matching sessions, read their `summary.md` and `meta.yaml` (for date, project, and tags).

For the top 2 most relevant sessions, also read `decisions.md` and `excerpts.md`.

#### 5. Return results

Present results ranked by relevance. For each match include:
- Session date (from `meta.yaml` `started_at`)
- Project name
- A short excerpt (1–2 sentences) from `summary.md` showing why it matched
- Path to `summary.md` for the user to explore further

If no results are found, say so and suggest broader search terms.

### `/memory on`

Inject the global INDEX.md and the current project's PROJECT.md into context.

#### 1. Get current project name

```bash
python3 -c "import hashlib, os; p = os.path.abspath(os.environ['PWD']); print(f\"{os.path.basename(p) or 'unknown'}-{hashlib.sha256(p.encode()).hexdigest()[:8]}\")"
```

#### 2. Read INDEX.md

Read `~/agent-memory/INDEX.md`. If it doesn't exist, tell the user the memory repo hasn't been set up yet and suggest running `/everywhere-setup`.

#### 3. Read project PROJECT.md

Read `~/agent-memory/projects/{project_name}/PROJECT.md`. If it doesn't exist, note that no sessions have been saved for this project yet.

#### 4. Summarize for the user

Count the number of `- [` entries under `## Recent Sessions` in PROJECT.md (or 0 if the file doesn't exist).

Report: "Memory loaded. {N} sessions found for **{project_name}**. INDEX.md and PROJECT.md are now in context."

### `/memory off`

Tell the user:

> "Injected memory context cannot be removed from an active session — once read, it remains in context. To start fresh without prior memory context, begin a new Claude Code session and do not run `/memory on`."

Do not proactively reference the previously loaded INDEX.md or PROJECT.md content for the remainder of the session unless the user explicitly asks about past sessions.
