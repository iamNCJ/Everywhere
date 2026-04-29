---
name: everywhere
description: Use when the user invokes /everywhere-setup, /session-save, /recall, /memory on, or /memory off. Also use when the user asks to search past sessions, access historical context, or save the current session to memory.
---

# Everywhere

> *Your agent sessions, accessible everywhere.*

Everywhere persists Claude Code sessions to `~/agent-memory/` synced to GitHub. Run `/everywhere-setup` once to register hooks — after that, Stop and SessionEnd hooks auto-save every session. Use these commands for manual control and retrieval.

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

Read the current `~/.claude/settings.json` (create it if missing). Add Stop and SessionEnd hooks using `type: "agent"` with the Haiku model. Merge carefully — preserve all existing settings.

The hooks should read the prompt from the plugin's installed prompt files. Find the installed plugin directory first:

```bash
find ~/.claude/plugins -name "stop-snapshot-prompt.md" 2>/dev/null | head -1
```

If found, use `cat` on that path as the prompt source. Write the hooks to settings.json:

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "agent",
        "model": "claude-haiku-4-5-20251001",
        "async": true,
        "timeout": 120,
        "statusMessage": "Everywhere: saving snapshot...",
        "prompt": "<contents of stop-snapshot-prompt.md>"
      }]
    }],
    "SessionEnd": [{
      "hooks": [{
        "type": "agent",
        "model": "claude-haiku-4-5-20251001",
        "async": true,
        "timeout": 180,
        "statusMessage": "Everywhere: finalizing session memory...",
        "prompt": "<contents of session-end-prompt.md>"
      }]
    }]
  }
}
```

Use the Read tool to load `~/.claude/settings.json`, merge the hooks section (preserve existing hooks), then write back with the Edit tool. Validate JSON after writing:

```bash
python3 -c "import json; json.load(open('$HOME/.claude/settings.json')); print('valid')"
```

Tell the user: "Hooks registered. They will take effect after restarting Claude Code (or run `/hooks` to reload)."

#### 5. Print setup summary

Report:
- Memory repo location: `~/agent-memory/`
- GitHub repo: output of `gh repo view everywhere-memory --json url -q .url`
- Hooks: registered in `~/.claude/settings.json` (Stop + SessionEnd, Haiku model, async)
- What happens next: hooks fire automatically on every session; use `/session-save` to trigger manually.

### `/session-save`

Manually trigger a full session save right now, following the same logic as the `session-end.sh` hook.

#### 1. Find the current session transcript

```bash
ls -t ~/.claude/projects/$(echo "$PWD" | sed 's|^/||; s|/|-|g')/*.jsonl 2>/dev/null | head -1
```

This returns the most recently modified session for the current directory. If multiple sessions exist, this picks the newest — which is usually correct. If no file is found, tell the user no transcript was found for the current directory and exit.

#### 2. Extract project info from the transcript

Read the first lines of the JSONL file to find a line with a `cwd` field:
- `project_path` = the value of `cwd`
- `project_name` = `basename` of `project_path`
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
basename "$PWD"
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
