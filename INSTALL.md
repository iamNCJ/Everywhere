# Installing Everywhere

> **Audience:** Another Claude / agent helping a user install this plugin from scratch.
> If you're a human, you can also just follow the steps yourself.

## What this plugin does

Everywhere is a Claude Code plugin that auto-saves every session into `~/agent-memory/`
(a local git repo, optionally pushed to GitHub). After install, two hooks fire automatically:

- `Stop` — writes a snapshot of the session locally (debounced, 10 min).
- `SessionEnd` — writes the final snapshot, commits, optionally `pull --rebase` + pushes.

Sessions become searchable via `/recall`, and per-project context is available via `/memory on`.
The plugin works **fully offline / local-only** if the user doesn't want GitHub sync.

## Agent task

Walk the user through the steps below. Ask before doing anything that touches their
account (creating GitHub repos, modifying `~/.claude/settings.json`). For each step,
run the listed check first — skip the step if it's already satisfied.

The minimum viable install is **steps 1, 2, 3, 5, 6**. GitHub sync (step 4) is optional.

---

## Step 1 — Prerequisites

```bash
git --version           # any recent version
claude --version        # the Claude Code CLI must be on PATH
python3 --version       # 3.8+
```

If `claude` is missing: tell the user to install Claude Code first
(<https://claude.com/claude-code>) and stop here.

GitHub-related tools are only needed if the user wants remote sync (step 4):

```bash
gh --version
gh auth status
```

If `gh` is missing or not authed, you can still proceed with local-only install.

---

## Step 2 — Install the plugin

There are two install paths. **Ask the user which they prefer** (default: A).

### A. Marketplace install (recommended)

```
/plugin marketplace add iamNCJ/Everywhere
/plugin install everywhere@Everywhere
```

This drops the plugin under `~/.claude/plugins/cache/` and auto-loads
`hooks/hooks.json`. **No manual hook registration is needed.**

Verify:

```bash
ls ~/.claude/plugins/cache/ | grep -i everywhere
```

### B. Local clone (for development or if marketplace is unavailable)

```bash
git clone https://github.com/iamNCJ/Everywhere.git ~/projects/everywhere
```

When using a local clone, hooks are **not** auto-registered. Do step 6's
"Manual hook registration" instead of relying on the plugin loader. Note the
absolute path to `~/projects/everywhere` — you'll need it.

---

## Step 3 — Initialize the memory repo

```bash
test -d ~/agent-memory/.git && echo "exists" || echo "missing"
```

If `missing`, run the bundled setup script:

```bash
bash ~/.claude/plugins/cache/*everywhere*/scripts/setup-memory-repo.sh
# or, for a local clone:
bash ~/projects/everywhere/scripts/setup-memory-repo.sh
```

This creates `~/agent-memory/` with `INDEX.md`, `global/`, `projects/`,
`.snapshots/`, a `.gitignore`, and an initial commit on branch `main`.

---

## Step 4 — (Optional) GitHub sync

Skip this if the user wants local-only memory. Sync is useful for:

- Working from multiple machines and wanting consistent history.
- Backup / disaster recovery.
- Browsing past sessions via the GitHub web UI.

**Ask the user first** — this creates a new GitHub repo under their account.

```bash
# Pick a name; default agent-memory. Private is strongly recommended — sessions
# may contain code/secrets/PII.
REPO_NAME=agent-memory
gh repo create "$REPO_NAME" --private --description "Everywhere session memory"
git -C ~/agent-memory remote add origin "$(gh repo view "$REPO_NAME" --json sshUrl -q .sshUrl)"
git -C ~/agent-memory push -u origin main
```

If the repo already exists, skip `gh repo create` and just set the remote.

**Multi-machine note:** the `SessionEnd` hook does `git pull --rebase --autostash`
before pushing, so concurrent edits from multiple machines reconcile automatically.
On true conflict (same file edited on both ends — extremely rare for snapshot files),
the rebase aborts, the local commit is kept, and the next session retries.

---

## Step 5 — Verify the memory repo

```bash
ls ~/agent-memory/
git -C ~/agent-memory log --oneline
git -C ~/agent-memory remote -v   # empty is fine if user skipped step 4
```

You should see `INDEX.md`, `global/`, `projects/`, and an `init: everywhere memory repo`
commit.

---

## Step 6 — Hooks

### A. Marketplace install: nothing to do

The plugin's `hooks/hooks.json` is loaded automatically. Confirm by running
`/hooks` inside Claude Code — you should see `Stop` and `SessionEnd` entries
mentioning `snapshot.py`.

### B. Manual hook registration (local clone only)

Find the absolute path to `snapshot.py`:

```bash
ABS=$(cd ~/projects/everywhere && pwd)/hooks/snapshot.py
echo "$ABS"
```

Merge this into `~/.claude/settings.json` (preserve all existing keys):

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "python3 \"<ABS_PATH>\"",
        "async": true,
        "timeout": 120,
        "statusMessage": "Everywhere: snapshotting session..."
      }]
    }],
    "SessionEnd": [{
      "hooks": [{
        "type": "command",
        "command": "python3 \"<ABS_PATH>\"",
        "async": true,
        "timeout": 180,
        "statusMessage": "Everywhere: finalizing session memory..."
      }]
    }]
  }
}
```

Replace `<ABS_PATH>` with the value from `$ABS`. Validate:

```bash
python3 -c "import json; json.load(open('$HOME/.claude/settings.json')); print('valid')"
```

Hooks take effect on the next session, or run `/hooks` to reload.

---

## Step 7 — Smoke test

Have the user start a fresh session, exchange at least 3 user messages with Claude,
then `/exit`. Within ~30 seconds:

```bash
ls -lt ~/agent-memory/projects/*/sessions/ | head
git -C ~/agent-memory log --oneline -3
```

You should see a new session directory and a new `session: <project> <id> - ...`
commit. If GitHub sync is configured, also confirm:

```bash
git -C ~/agent-memory log origin/main..main   # empty = fully pushed
```

---

## Step 8 — (Optional) Codex CLI capture

If the user also uses Codex CLI and wants its sessions captured into the same
`~/agent-memory/`, register Codex hooks. Cross-platform (anywhere Codex runs).

```bash
codex --version                                          # verify Codex CLI is installed
codex features list 2>/dev/null | grep -q "^hooks .*true"  # must print nothing if OK
```

Codex hooks are stable since `codex-cli >= 0.130.0`. If `hooks` shows as
`under development`, run `codex update` first.

Then invoke `/everywhere-codex-setup` inside Claude Code. The skill walks
through:

1. (If upgrading from the launchd version) `launchctl bootout` the old job
   and delete the plist.
2. Stage hooks to `~/.everywhere/hooks/` so the registered `command` strings
   in `~/.codex/hooks.json` use stable absolute paths.
3. Merge two entries into `~/.codex/hooks.json`: a `Stop` hook (per-turn,
   debounced snapshot) and a `SessionStart` hook (finalize idle rollouts on
   the next Codex launch). The merge preserves any existing user-owned hook
   entries.
4. Optionally copy `SKILL.md` to `~/.codex/skills/everywhere/` so `/recall`
   and `/memory on` work from inside Codex too.

After install, Codex sessions are auto-captured at end of every turn
(debounced 10 min). Sessions idle for >10 min get a final commit + push on
the **next** Codex launch (Codex CLI has no `SessionEnd` event). The setup
does **not** modify `~/.codex/config.toml`, so any existing `notify`
integrations keep working.

Verify:

```bash
python3 -c "import json; print(json.dumps(json.load(open('$HOME/.codex/hooks.json')), indent=2))" | head -40
EVERYWHERE_DEBUG=1 codex                                 # open a session, take a few turns, /exit
grep -rl 'agent: codex' ~/agent-memory/projects/ | head  # after a turn fires
```

To uninstall just the Codex side: invoke `/everywhere-codex-uninstall`.

---

## Troubleshooting

Re-run with debug logs to see what the hook is doing:

```bash
EVERYWHERE_DEBUG=1 claude   # then have a short session and /exit
```

Hook stderr appears in Claude Code's status line transiently; the most reliable way
to inspect it is to invoke `snapshot.py` directly with a real payload (see below).

| Symptom | Likely cause / fix |
|---|---|
| No session dir written after `/exit` | Fewer than 3 user messages (intentional skip), or `claude -p` on PATH is broken. Test with `EVERYWHERE_DEBUG=1`. |
| `claude -p` fails / non-JSON output | The summary model rejected the prompt or CLI auth expired. Run `claude` once interactively to refresh auth. |
| `git push` fails with `rejected (non-fast-forward)` | A concurrent commit pushed from another machine. The next `SessionEnd` will `pull --rebase` and retry — usually self-heals. |
| `git pull --rebase` aborts with conflict | Same snapshot file edited on two machines. Resolve manually in `~/agent-memory/` (`git rebase --continue` or `--abort`). |
| Memory repo not initialized error in stderr | Step 3 was skipped. Run the setup script. |
| Hook never fires | Hooks not loaded — run `/hooks` to reload, or check `~/.claude/settings.json` JSON validity. |

Manually trigger the hook for testing:

```bash
TRANSCRIPT=$(ls -t ~/.claude/projects/$(echo "$PWD" | sed 's|^/||; s|/|-|g')/*.jsonl | head -1)
echo "{\"session_id\":\"test123\",\"transcript_path\":\"$TRANSCRIPT\",\"cwd\":\"$PWD\",\"hook_event_name\":\"SessionEnd\"}" \
  | EVERYWHERE_DEBUG=1 python3 ~/.claude/plugins/cache/*everywhere*/hooks/snapshot.py
```

For a no-`claude -p` smoke test, add `--dry-run` to the python command — it writes a stub
summary so you can verify file/git plumbing without burning tokens.

---

## Uninstall

```bash
/plugin uninstall everywhere@Everywhere     # marketplace install
# or for local clone: remove the Stop/SessionEnd hook blocks from
# ~/.claude/settings.json by hand.
```

If Codex capture was installed, also run `/everywhere-codex-uninstall`. It
removes our entries from `~/.codex/hooks.json` (preserving any user-owned
hooks), best-effort cleans up the legacy launchd plist (if upgrading from
an old install), and removes `~/.everywhere/hooks/`.

The `~/agent-memory/` repo is left intact — delete it manually if desired.
