# Everywhere

> *Your Claude Code and Codex sessions, accessible everywhere.*

Everywhere is a Claude Code plugin (with optional Codex CLI support) that
auto-saves every session into a local git repo at `~/agent-memory/`, optionally
synced to GitHub. Past sessions become searchable across projects, agents, and
machines — context never gets stranded on whichever laptop happened to host
the original conversation.

## Why

Agent CLI sessions are ephemeral — when you `/exit`, the conversation is gone.
You can scroll back through `~/.claude/projects/` or `~/.codex/sessions/`
transcripts, but they're raw and untagged, not browsable. Everywhere fixes that:

- **Auto-summarize** every session into structured Markdown (summary, decisions,
  artifacts, verbatim excerpts, tags).
- **One repo per user, one folder per project**, so context is local-first and
  trivially greppable.
- **Cross-agent** — Claude Code and Codex sessions land side-by-side under the
  same project; `meta.yaml.agent` distinguishes the source. `/recall` searches
  both.
- **Cross-machine** — push to GitHub, pull on another laptop, and `/recall` your
  past work from anywhere.

## How it works

Two capture paths feed the same `~/agent-memory/`:

| Trigger | Agent | When | Action |
|---|---|---|---|
| `Stop` hook | Claude Code | End of each turn | Debounced (10 min) snapshot. |
| `SessionEnd` hook | Claude Code | `/exit` or session close | Final snapshot, commit, `pull --rebase` + push. |
| `launchd` sweeper | Codex CLI | Every 5 min | Walks `~/.codex/sessions/`, snapshots active rollouts, finalizes idle ones (>10 min no activity). |

- **Claude** sessions are summarized by `claude -p` running Haiku 4.5.
- **Codex** sessions are summarized by `codex exec --ephemeral` running a cheap
  tier model (default `gpt-5.4-mini`). The `--ephemeral` flag prevents the
  summarizer call from itself producing a rollout.

Sessions with fewer than 3 user messages are skipped on both paths. The Codex
sweeper does **not** modify `~/.codex/config.toml` — it doesn't touch `notify`,
so existing notify integrations (e.g., Computer Use) keep working.

Every error path logs to stderr and exits 0; a misbehaving snapshot never
blocks your session or the launchd timer.

## Install

See **[INSTALL.md](./INSTALL.md)** for the full step-by-step guide (it doubles as
an agent-readable runbook — you can ask another Claude to install Everywhere
for you and point it at that file).

Quick path for the impatient:

```
/plugin marketplace add iamNCJ/Everywhere
/plugin install everywhere@Everywhere
```

Then in any Claude Code session:

```
/everywhere-setup            # Claude side (memory repo + Stop/SessionEnd hooks)
/everywhere-codex-setup      # Optional: Codex side (launchd sweeper, macOS only)
```

`/everywhere-setup` initializes `~/agent-memory/`, optionally creates a private
GitHub repo for sync, and verifies hooks are registered.
`/everywhere-codex-setup` stages the hook scripts to `~/.everywhere/hooks/`
(avoiding macOS TCC restrictions on `~/Documents/`) and installs a launchd
agent that polls Codex sessions every 5 min.

## Commands

Once installed, the plugin exposes:

| Command | Purpose |
|---|---|
| `/everywhere-setup` | One-time setup. Initializes memory repo + Claude Code hooks. |
| `/everywhere-codex-setup` | One-time setup for Codex CLI capture (macOS launchd sweeper). |
| `/everywhere-codex-uninstall` | Remove the Codex sweeper and staged hooks. |
| `/session-save` | Manually trigger a save of the current session (Claude or Codex auto-detected). |
| `/recall <query>` | Search past sessions across all projects and agents, ranked. |
| `/memory on` | Inject `INDEX.md` + the current project's `PROJECT.md` into context. |
| `/memory off` | (Reminder) injected memory persists in the current session — start a new one to drop it. |

## Repo layout

After running `/everywhere-setup`, your `~/agent-memory/` looks like:

```
~/agent-memory/
├── INDEX.md                                  # all projects, auto-maintained
├── global/
│   └── cross-session-insights.md             # cross-project patterns (manual)
├── projects/
│   └── <project-name>/
│       ├── PROJECT.md                        # recent sessions for this project
│       └── sessions/
│           └── 2026-05-06-abc123/
│               ├── meta.yaml                 # session_id, tags, started_at, ...
│               ├── summary.md                # 3–5 sentence headline
│               ├── decisions.md              # technical decisions + rationale
│               ├── artifacts.md              # files / commands / references
│               └── excerpts.md               # 3–5 verbatim Q&A exchanges
└── .snapshots/                               # gitignored debounce state
```

## Local-only mode

GitHub sync is **optional**. If you don't run step 4 of `INSTALL.md` (or don't
configure a remote on `~/agent-memory/`), the hook detects no remote and skips
push silently — everything still works locally.

## Design notes

- **Hook type (Claude):** `command` + `async: true`. The hook spawns
  `python3 snapshot.py` which calls `claude -p` only for the summary step.
  `type: agent` was tried but is documented as experimental; the command path
  is deterministic and survives parent-process exit reliably.
- **Codex trigger:** a launchd user agent (`StartInterval = 300`) instead of
  Codex's `notify` config option. Reason: `notify` is a single-command field
  and is commonly already in use (e.g., Codex Computer Use). The launchd path
  is fully passive and never touches `~/.codex/config.toml`. Trade-off: up to
  ~5 min capture latency, which is fine for "searchable memory" use cases.
- **TCC staging:** macOS Transparency, Consent, and Control blocks
  launchd-spawned processes from reading `~/Documents/` without Full Disk
  Access. `/everywhere-codex-setup` works around this by copying the hook
  scripts into `~/.everywhere/hooks/` and pointing the plist there.
  Re-running setup refreshes the staged copies after a plugin update.
- **Multi-machine:** `SessionEnd` (Claude) and the finalize-branch of the
  Codex sweeper both do `git pull --rebase --autostash origin main` before
  pushing. Concurrent edits from different machines reconcile automatically.
  True conflicts (rare, since each session writes its own dated folder) leave
  the local commit in place to be retried next session.
- **Failure mode:** every error path logs to stderr and exits 0. The hook /
  sweeper will *never* prevent you from `/exit`-ing or block another launchd
  invocation.

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `EVERYWHERE_DEBUG` | `0` | When `1`, hook logs progress to stderr (visible in Claude Code's status line / Codex sweep log). |
| `EVERYWHERE_CODEX_MODEL` | `gpt-5.4-mini` | Codex summarizer model. Set inline in the launchd plist's `EnvironmentVariables` block for persistence. |

Tunables live near the top of `hooks/snapshot.py` (Claude path) and
`hooks/codex_sweep.py` (Codex sweeper):

- `DEBOUNCE_SECONDS = 600` — minimum gap between non-final snapshots.
- `FINALITY_IDLE_SECONDS = 600` — Codex rollout idle threshold to trigger
  finalization.
- `MIN_USER_MESSAGES = 3` — sessions shorter than this are skipped.
- `MAX_CONVERSATION_CHARS = 60000` — transcript is tail-truncated to this size
  before being passed to the summarizer.
- `DEFAULT_MODEL = "claude-haiku-4-5"` — Claude summarizer model.
- `SCAN_DAYS = 7` — Codex sweeper only walks the last N days of rollouts.

## Repo

<https://github.com/iamNCJ/Everywhere>

## License

MIT
