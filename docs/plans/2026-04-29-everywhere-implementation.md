# Everywhere Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Everywhere — a skill that auto-persists structured memory from every Claude Code session to a local+GitHub-synced Git repo, with cross-session search and optional context injection.

**Architecture:** Two async Haiku agent hooks (Stop for snapshots, SessionEnd for final summary + push) write hierarchical Markdown files to `~/agent-memory/`. A skill SKILL.md provides `/everywhere-setup`, `/session-save`, `/recall`, and `/memory on/off` commands.

**Tech Stack:** Claude Code hooks (agent type, async), Claude Haiku (claude-haiku-4-5-20251001), JSONL transcript parsing, Git + GitHub CLI (gh), ripgrep, Bash, Python 3

---

## File Structure

```
everywhere/                              ← this repo
  docs/specs/2026-04-29-*.md
  docs/plans/2026-04-29-*.md            ← this file
  hooks/
    stop-snapshot-prompt.md             ← source prompt for Stop hook agent
    session-end-prompt.md               ← source prompt for SessionEnd hook agent
  scripts/
    parse-transcript.py                 ← extract text from session JSONL
    setup-memory-repo.sh                ← initialize ~/agent-memory/

~/.claude/skills/everywhere/
  SKILL.md                              ← user-facing skill (all 5 commands)

~/agent-memory/                         ← the memory repo (created by setup)
  .gitignore
  .snapshots/                           ← gitignored debounce timestamps
  INDEX.md
  global/
    cross-session-insights.md
  projects/
    {project-name}/
      PROJECT.md
      sessions/
        {date}-{session_id_short}/
          meta.yaml
          summary.md
          decisions.md
          artifacts.md
          excerpts.md
```

---

## Task 1: Validate Transcript Access

Before writing any prompts, prove the transcript can be read and parsed correctly.

**Files:**
- Create: `scripts/parse-transcript.py`

- [ ] **Step 1: Write parse-transcript.py**

```python
#!/usr/bin/env python3
"""Extract user/assistant conversation from a CC session JSONL."""
import json, sys, pathlib

def parse_transcript(jsonl_path):
    messages = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_type = entry.get("type")
            message = entry.get("message", {})
            if msg_type == "user":
                content = message.get("content", "")
                if isinstance(content, str) and content.strip():
                    messages.append({"role": "user", "text": content.strip()})
                elif isinstance(content, list):
                    text = " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ).strip()
                    if text:
                        messages.append({"role": "user", "text": text})
            elif msg_type == "assistant":
                content = message.get("content", [])
                if isinstance(content, list):
                    text = " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ).strip()
                    if text:
                        messages.append({"role": "assistant", "text": text})
    return messages

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: parse-transcript.py <path-to-session.jsonl>")
        sys.exit(1)
    msgs = parse_transcript(sys.argv[1])
    print(f"Found {len(msgs)} messages")
    for m in msgs[:3]:
        print(f"\n[{m['role'].upper()}] {m['text'][:200]}")
```

- [ ] **Step 2: Make executable and test against current session**

```bash
chmod +x scripts/parse-transcript.py

# Find the current session JSONL
SESSION_JSONL=$(find ~/.claude/projects -name "*.jsonl" | xargs ls -t | head -1)
echo "Testing against: $SESSION_JSONL"
python3 scripts/parse-transcript.py "$SESSION_JSONL"
```

Expected: prints message count ≥ 3, shows first few user/assistant messages correctly.

- [ ] **Step 3: Verify hook stdin format**

The Stop and SessionEnd hooks receive JSON on stdin. Verify the fields available by adding a temporary test hook:

```bash
# Add to ~/.claude/settings.json temporarily (remove after test):
# "Stop": [{"hooks": [{"type": "command", "command": "cat >> /tmp/everywhere-hook-test.txt"}]}]
# Then trigger a stop (send any message) and check:
cat /tmp/everywhere-hook-test.txt
```

Expected output will contain at minimum `{"session_id": "..."}`. Note any additional fields (cwd, etc.).

- [ ] **Step 4: Write the session-finder helper function**

This logic is used in both hook prompts. Document the canonical approach:

Given `session_id` from hook stdin, find the transcript:
```bash
find ~/.claude/projects -name "${SESSION_ID}.jsonl" 2>/dev/null | head -1
```

The directory containing the JSONL encodes the project path. To recover:
```bash
# Path like: /Users/ncj/.claude/projects/-Users-ncj-Documents-workspace-dev/abc.jsonl
# Directory name: -Users-ncj-Documents-workspace-dev
# Project path: replace leading - with /, then replace remaining - with /
# More reliably: read cwd from first line of JSONL that has it
python3 -c "
import json
with open('$TRANSCRIPT') as f:
    for line in f:
        d = json.loads(line.strip())
        if d.get('cwd'):
            print(d['cwd'])
            break
"
```

- [ ] **Step 5: Commit**

```bash
git add scripts/parse-transcript.py
git commit -m "feat: add transcript parser and validate session access"
```

---

## Task 2: Initialize Memory Repo

Set up `~/agent-memory/` as a local Git repo with the correct structure.

**Files:**
- Create: `scripts/setup-memory-repo.sh`

- [ ] **Step 1: Write setup-memory-repo.sh**

```bash
#!/usr/bin/env bash
set -e

MEMORY_DIR="$HOME/agent-memory"

if [ -d "$MEMORY_DIR/.git" ]; then
  echo "~/agent-memory/ already initialized. Skipping."
  exit 0
fi

echo "Initializing ~/agent-memory/..."
mkdir -p "$MEMORY_DIR"
cd "$MEMORY_DIR"

git init
git branch -m main

mkdir -p .snapshots global projects

cat > .gitignore << 'EOF'
.snapshots/
.DS_Store
EOF

cat > INDEX.md << 'EOF'
# Everywhere — Global Memory Index

> Auto-maintained by Everywhere. Last updated by SessionEnd hook.

## Projects

<!-- Projects are added automatically as sessions are recorded -->
EOF

cat > global/cross-session-insights.md << 'EOF'
# Cross-Session Insights

> High-value learnings that span multiple projects or sessions.
> Updated manually or by the /recall workflow when patterns emerge.
EOF

git add -A
git commit -m "init: everywhere memory repo"

echo "✓ ~/agent-memory/ initialized"
echo "Next: run 'gh repo create everywhere-memory --private' and 'git remote add origin <url>'"
```

- [ ] **Step 2: Run the setup script**

```bash
chmod +x scripts/setup-memory-repo.sh
bash scripts/setup-memory-repo.sh
```

Expected: `~/agent-memory/` created with correct structure, initial commit exists.

- [ ] **Step 3: Verify structure**

```bash
ls -la ~/agent-memory/
git -C ~/agent-memory log --oneline
```

Expected output:
```
drwxr-xr-x  .snapshots
-rw-r--r--  .gitignore
-rw-r--r--  INDEX.md
drwxr-xr-x  global
drwxr-xr-x  projects
<hash> init: everywhere memory repo
```

- [ ] **Step 4: Commit**

```bash
git add scripts/setup-memory-repo.sh
git commit -m "feat: add memory repo initialization script"
```

---

## Task 3: Write Stop Hook Agent Prompt

The Stop hook fires after each Claude response. The agent debounces and writes an incremental snapshot.

**Files:**
- Create: `hooks/stop-snapshot-prompt.md`

- [ ] **Step 1: Write stop-snapshot-prompt.md**

```markdown
You are Everywhere, an async memory agent for Claude Code. You run after each Claude Code response to snapshot the ongoing session into the global memory repo. Be efficient — this runs frequently.

You receive JSON on stdin. Extract the session_id field.

## Step 1: Debounce check

Read the file: ~/agent-memory/.snapshots/.last-{session_id}
- If it exists AND its content (a unix timestamp integer) is less than 600 seconds ago → exit immediately, do nothing
- Otherwise proceed

## Step 2: Find the session transcript

Run: find ~/.claude/projects -name "{session_id}.jsonl" 2>/dev/null | head -1

If no file found, exit with no action.

## Step 3: Extract project info

Read the first few lines of the JSONL to find a line with a "cwd" field. That gives you project_path.
project_name = last component of project_path (e.g. "workspace-dev" from "/Users/ncj/Documents/workspace/dev")

## Step 4: Parse the conversation

Read the JSONL. Collect entries where type is "user" or "assistant":
- user: message.content is a string (the user's message)
- assistant: message.content is a list of blocks; join text blocks

If fewer than 3 user messages found, exit with no action (session too short).

## Step 5: Create snapshot directory

Path: ~/agent-memory/projects/{project_name}/sessions/{YYYY-MM-DD}-{session_id[:6]}/
Use today's date. Create if not exists.

## Step 6: Write snapshot files (overwrite any existing)

**meta.yaml:**
```yaml
session_id: {full session_id}
session_id_short: {first 6 chars}
project_path: {project_path}
project_name: {project_name}
agent: claude-code
started_at: {timestamp from first JSONL entry}
is_final: false
tags: [{3-5 relevant tags based on conversation content}]
```

**summary.md:**
3-5 sentences describing what happened in this session so far. Focus on: what problem was being solved, key approach taken, current status.

**decisions.md:**
Bullet list of key technical decisions, choices made, or important conclusions. Include the reasoning when clear. If none, write "No significant decisions yet."

**artifacts.md:**
List of files mentioned as created/modified, commands run, GitHub issues/PRs referenced. Format:
- Files: `path/to/file` — what was done
- Commands: `command` — what it did
- Other: description

**excerpts.md:**
The 2-3 most valuable Q&A exchanges from the conversation, verbatim. Choose exchanges that would be most useful to recall in a future session.

## Step 7: Update debounce timestamp

Write current unix timestamp (integer) to: ~/agent-memory/.snapshots/.last-{session_id}
Create the .snapshots directory if needed.

## Step 8: Do NOT git commit or push

Snapshots are local only. The SessionEnd hook handles git.
```

- [ ] **Step 2: Manually test the prompt against the current session**

Run this in a new CC session to simulate what Haiku will do:

```bash
# Find current session ID from history
SESSION_ID=$(tail -5 ~/.claude/history.jsonl | python3 -c "import json,sys; lines=[l for l in sys.stdin if l.strip()]; print(json.loads(lines[-1])['sessionId'])" 2>/dev/null)
echo "Testing with session: $SESSION_ID"
```

Then in CC: ask Claude to follow the instructions in `hooks/stop-snapshot-prompt.md` treating `$SESSION_ID` as the session_id. Verify the output files are written correctly to `~/agent-memory/`.

- [ ] **Step 3: Inspect the generated files**

```bash
PROJ=$(ls ~/agent-memory/projects/ | head -1)
SESSION=$(ls ~/agent-memory/projects/$PROJ/sessions/ | head -1)
echo "=== meta.yaml ===" && cat ~/agent-memory/projects/$PROJ/sessions/$SESSION/meta.yaml
echo "=== summary.md ===" && cat ~/agent-memory/projects/$PROJ/sessions/$SESSION/summary.md
echo "=== decisions.md ===" && cat ~/agent-memory/projects/$PROJ/sessions/$SESSION/decisions.md
```

Expected: well-formed YAML and Markdown files with actual session content.

- [ ] **Step 4: Commit**

```bash
git add hooks/stop-snapshot-prompt.md
git commit -m "feat: add Stop hook agent prompt for session snapshots"
```

---

## Task 4: Write SessionEnd Hook Agent Prompt

SessionEnd fires once when the session actually ends. This does the final summary, updates PROJECT.md + INDEX.md, and git pushes.

**Files:**
- Create: `hooks/session-end-prompt.md`

- [ ] **Step 1: Write session-end-prompt.md**

```markdown
You are Everywhere, an async memory agent for Claude Code. You run once when a Claude Code session ends. Write the final memory record and sync to GitHub.

You receive JSON on stdin. Extract the session_id field.

## Step 1: Find the session transcript

Run: find ~/.claude/projects -name "{session_id}.jsonl" 2>/dev/null | head -1

If no file found, exit with no action.

## Step 2: Extract project info

Read the first few lines of the JSONL to find a line with a "cwd" field.
project_path = the cwd value
project_name = last path component

## Step 3: Parse the full conversation

Read entire JSONL. Collect user and assistant messages (same format as snapshot agent).
If fewer than 3 user messages, exit with no action.

## Step 4: Write final session files

Path: ~/agent-memory/projects/{project_name}/sessions/{YYYY-MM-DD}-{session_id[:6]}/
(Create directory if not exists; overwrite existing files from snapshots)

**meta.yaml:** (complete final version)
```yaml
session_id: {full session_id}
session_id_short: {first 6 chars}
project_path: {project_path}
project_name: {project_name}
agent: claude-code
started_at: {timestamp from first JSONL entry with timestamp field}
ended_at: {current ISO timestamp}
is_final: true
snapshot_count: {count existing meta.yaml files in this dir before this write, default 0}
tags: [{5-8 relevant tags}]
```

**summary.md:** Thorough 3-5 sentence overview. Cover: what problem was solved, approach taken, outcome, anything left incomplete.

**decisions.md:** Complete list of all technical decisions and important conclusions. Include rationale. Group related decisions under sub-headings if more than 5.

**artifacts.md:** Complete audit trail:
- `## Files` — all files created/modified with descriptions
- `## Commands` — significant commands run and their outcomes
- `## References` — PRs, issues, docs, URLs mentioned

**excerpts.md:** The 3-5 most valuable Q&A exchanges verbatim. Prioritize: novel solutions, debugging breakthroughs, important explanations.

## Step 5: Update PROJECT.md

File: ~/agent-memory/projects/{project_name}/PROJECT.md
Create if not exists with this template, then add entry:

```markdown
# {project_name}

**Path:** {project_path}

## Recent Sessions

- [{YYYY-MM-DD} - {first line of summary}](sessions/{date}-{session_id_short}/summary.md)
<!-- Add new sessions above. Keep last 10 only. -->

## Project Overview

<!-- Updated manually or by /recall when patterns emerge -->
```

If file exists: prepend new session entry under "## Recent Sessions". If more than 10 entries, remove the oldest.

## Step 6: Update INDEX.md

File: ~/agent-memory/INDEX.md
If project is not already listed, add a line under "## Projects":
`- [{project_name}](projects/{project_name}/PROJECT.md) — {project_path}`

## Step 7: Git commit and push

```
git -C ~/agent-memory add -A
git -C ~/agent-memory commit -m "session: {project_name} {session_id_short} - {first_sentence_of_summary}"
git -C ~/agent-memory push origin main
```

If push fails (no remote configured), just commit locally without error.

## Step 8: Cleanup

Delete ~/agent-memory/.snapshots/.last-{session_id} if it exists.
```

- [ ] **Step 2: Manually test the SessionEnd prompt**

In CC: ask Claude to follow instructions in `hooks/session-end-prompt.md` for the most recent session. Inspect output:

```bash
PROJ=$(ls ~/agent-memory/projects/ | head -1)
SESSION=$(ls ~/agent-memory/projects/$PROJ/sessions/ | head -1)
cat ~/agent-memory/projects/$PROJ/PROJECT.md
cat ~/agent-memory/INDEX.md
git -C ~/agent-memory log --oneline | head -5
```

- [ ] **Step 3: Commit**

```bash
git add hooks/session-end-prompt.md
git commit -m "feat: add SessionEnd hook agent prompt for final summary and sync"
```

---

## Task 5: Register Hooks in settings.json

Wire up both hooks in `~/.claude/settings.json`.

**Files:**
- Modify: `~/.claude/settings.json`

- [ ] **Step 1: Read current settings.json**

```bash
cat ~/.claude/settings.json
```

Note existing hooks section (currently empty `{}`).

- [ ] **Step 2: Read the prompts and prepare inline versions**

The prompts need to be single-line strings in JSON. Read and escape them:

```bash
python3 -c "
import json
with open('hooks/stop-snapshot-prompt.md') as f:
    print(json.dumps(f.read()))
"
```

- [ ] **Step 3: Add hooks to settings.json**

Edit `~/.claude/settings.json` to add the hooks section. The structure:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "agent",
            "model": "claude-haiku-4-5-20251001",
            "async": true,
            "timeout": 120,
            "statusMessage": "Everywhere: saving snapshot...",
            "prompt": "<contents of hooks/stop-snapshot-prompt.md>"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "agent",
            "model": "claude-haiku-4-5-20251001",
            "async": true,
            "timeout": 180,
            "statusMessage": "Everywhere: finalizing session memory...",
            "prompt": "<contents of hooks/session-end-prompt.md>"
          }
        ]
      }
    ]
  }
}
```

Use the Read tool to load the current settings, merge carefully (preserve all existing keys), then write back with Edit tool.

- [ ] **Step 4: Validate JSON syntax**

```bash
python3 -c "import json; json.load(open(f'{__import__(\"os\").environ[\"HOME\"]}/.claude/settings.json')); print('✓ Valid JSON')"
```

- [ ] **Step 5: Validate hook schema**

```bash
python3 -c "
import json, os
s = json.load(open(os.path.expanduser('~/.claude/settings.json')))
hooks = s.get('hooks', {})
for event in ['Stop', 'SessionEnd']:
    assert event in hooks, f'Missing {event} hook'
    h = hooks[event][0]['hooks'][0]
    assert h['type'] == 'agent'
    assert h['async'] == True
    assert 'haiku' in h['model']
    print(f'✓ {event} hook valid')
"
```

- [ ] **Step 6: Reload hooks**

Tell user: open `/hooks` in CC to reload the configuration, OR restart CC.

- [ ] **Step 7: Commit hook prompts and note settings change**

```bash
# Commit to this repo (settings.json is in ~/ and not tracked here)
git add hooks/
git commit -m "feat: finalize hook prompts, hooks registered in settings.json"
```

---

## Task 6: Write everywhere SKILL.md

The user-facing skill that makes all commands available in any CC session.

**Files:**
- Create: `~/.claude/skills/everywhere/SKILL.md`

- [ ] **Step 1: Create the skills directory**

```bash
mkdir -p ~/.claude/skills/everywhere
```

- [ ] **Step 2: Write SKILL.md**

```markdown
---
name: everywhere
description: Use when the user invokes /everywhere-setup, /session-save, /recall, /memory on, or /memory off. Also use when the user asks to search past sessions, access historical context, or save the current session to memory.
---

# Everywhere

> *Your agent sessions, accessible everywhere.*

Everywhere auto-saves every Claude Code session to a hierarchical memory repo synced to GitHub. Search and recall past sessions from any new session.

## Commands

### /everywhere-setup

First-time setup. Run this once. Steps:

1. **Check prerequisites**
   ```bash
   git --version && gh --version && gh auth status
   ```
   If `gh` is not authenticated, tell user to run `gh auth login` first.

2. **Create GitHub repo**
   ```bash
   gh repo create everywhere-memory --private --description "Everywhere global session memory" --confirm
   REMOTE_URL=$(gh repo view everywhere-memory --json sshUrl -q .sshUrl)
   echo "Remote: $REMOTE_URL"
   ```

3. **Initialize local memory repo** — run `scripts/setup-memory-repo.sh` from the everywhere project, OR manually:
   ```bash
   mkdir -p ~/agent-memory && cd ~/agent-memory
   git init && git branch -m main
   mkdir -p .snapshots global projects
   echo ".snapshots/\n.DS_Store" > .gitignore
   echo "# Everywhere — Global Memory Index\n\n## Projects\n" > INDEX.md
   mkdir -p global && echo "# Cross-Session Insights\n" > global/cross-session-insights.md
   git add -A && git commit -m "init: everywhere memory repo"
   ```

4. **Add remote and push**
   ```bash
   git -C ~/agent-memory remote add origin "$REMOTE_URL"
   git -C ~/agent-memory push -u origin main
   ```

5. **Register hooks** — edit `~/.claude/settings.json` to add the Stop and SessionEnd hooks as documented in the everywhere project at `hooks/stop-snapshot-prompt.md` and `hooks/session-end-prompt.md`.

6. **Verify and summarize** — tell the user what was set up and remind them to reload hooks via `/hooks`.

---

### /session-save

Manually trigger a full save of the current session right now.

Follow the instructions in `~/.claude/skills/everywhere/session-end-prompt.md`, treating the current session_id (ask user or check `~/.claude/history.jsonl` for the most recent sessionId matching the current cwd) as the session to save.

After saving: report what was written and whether git push succeeded.

---

### /recall [query]

Search past sessions in `~/agent-memory/` for content matching the query.

1. **Fast keyword search across summaries and decisions:**
   ```bash
   rg -l "{query}" ~/agent-memory/projects/ --include="*.md" -g "summary.md" -g "decisions.md"
   ```

2. **Search tags in meta.yaml files:**
   ```bash
   rg "{query}" ~/agent-memory/projects/ --include="meta.yaml" -l
   ```

3. **Read top candidates** — for each matching file, read the parent session's `summary.md`. Present results as:
   ```
   📅 2026-04-29 | workspace-dev | session abc123
   → {first 2 sentences of summary}
   ```

4. **Expand on request** — if user wants more detail, read `decisions.md` or `excerpts.md` for that session.

5. **Search excerpts if keyword search finds nothing:**
   ```bash
   rg -i "{query}" ~/agent-memory/projects/ --include="excerpts.md"
   ```

---

### /memory on

Inject the global memory summary into the current session context.

Read and present:
1. `~/agent-memory/INDEX.md` — project list
2. `~/agent-memory/projects/{current-project-name}/PROJECT.md` — recent sessions for this project (current project = `basename $PWD`)

Present as a structured summary at the top of your next response. Tell the user memory is now active and they can ask questions about past sessions.

---

### /memory off

Acknowledge that memory injection is disabled. Do not include memory context in subsequent responses unless the user invokes `/memory on` again.

---

## Memory Repo Location

`~/agent-memory/` — local Git repo, synced to `everywhere-memory` private GitHub repo.

## How Auto-Save Works

- **Stop hook** (every response, debounced): Haiku agent writes incremental snapshot if >10 min since last save
- **SessionEnd hook** (once at exit): Haiku agent writes final complete summary, updates PROJECT.md + INDEX.md, pushes to GitHub
- Both run async — zero impact on main session performance or token count
```

- [ ] **Step 3: Verify the file was written**

```bash
ls -la ~/.claude/skills/everywhere/
head -5 ~/.claude/skills/everywhere/SKILL.md
```

- [ ] **Step 4: Deploy prompt files to skills dir and commit**

The skill references prompt files from `~/.claude/skills/everywhere/` so it's portable:

```bash
# Copy hook prompts into the skill directory so /session-save can reference them
cp hooks/stop-snapshot-prompt.md ~/.claude/skills/everywhere/stop-snapshot-prompt.md
cp hooks/session-end-prompt.md ~/.claude/skills/everywhere/session-end-prompt.md

# Copy SKILL.md back to this repo for documentation
mkdir -p skills
cp ~/.claude/skills/everywhere/SKILL.md skills/SKILL.md
git add skills/SKILL.md hooks/
git commit -m "feat: add everywhere skill SKILL.md and deploy prompt files"
```

---

## Task 7: End-to-End Verification

Prove the full system works: hook fires → transcript read → files written → git push.

- [ ] **Step 1: Verify ~/agent-memory/ has a git remote**

```bash
git -C ~/agent-memory remote -v
```

If no remote: complete the GitHub setup from Task 6 `/everywhere-setup` step 2-4.

- [ ] **Step 2: Trigger a manual end-to-end test**

In this CC session, ask Claude to simulate a SessionEnd by following `hooks/session-end-prompt.md` for the current session. Observe:

```bash
# Before
ls ~/agent-memory/projects/ 2>/dev/null || echo "(empty)"
git -C ~/agent-memory log --oneline | head -3
```

Run simulation, then:

```bash
# After
find ~/agent-memory/projects -name "*.md" | head -10
git -C ~/agent-memory log --oneline | head -3
cat ~/agent-memory/INDEX.md
```

Expected: session directory created with 5 files, new git commit, INDEX.md updated.

- [ ] **Step 3: Verify git push worked**

```bash
git -C ~/agent-memory log --oneline origin/main | head -3
```

Should match local log. If remote not set up yet, just verify local commit exists.

- [ ] **Step 4: Test /recall**

Ask Claude: `/recall everywhere design`

Expected: finds the current session (which covers designing Everywhere), shows summary with session date and project.

- [ ] **Step 5: Start a fresh CC session and verify Stop hook fires**

Open a new CC session on any project. Send a message. Wait ~30 seconds for async agent. Then:

```bash
ls ~/agent-memory/.snapshots/
```

Expected: a `.last-{session_id}` timestamp file appears within ~60 seconds.

- [ ] **Step 6: Final commit**

```bash
git add -A
git commit -m "feat: everywhere v1 complete - hooks, skill, and memory repo verified"
```

---

## Post-Implementation Notes

- **Transcript access risk:** If `~/.claude/projects/` structure changes in a CC update, the transcript-finding logic breaks. Monitor for CC updates.
- **Hook prompt updates:** To update a hook prompt, edit `hooks/*.md` in this repo, then update the inline `prompt` field in `~/.claude/settings.json` and reload via `/hooks`.
- **Adding Codex support:** Codex uses `~/.agents/` for skills and has different hook mechanisms. Out of scope for v1.
- **Debounce tuning:** 10 minutes is the default. Adjust the `600` value in the Stop hook prompt if needed.
