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

Native hooks on both agents feed the same `~/agent-memory/`:

| Trigger | Agent | When | Action |
|---|---|---|---|
| `Stop` hook | Claude Code | End of each turn | Debounced (10 min) snapshot. |
| `SessionEnd` hook | Claude Code | `/exit` or session close | Final snapshot, commit, `pull --rebase` + push. |
| `Stop` hook | Codex CLI | End of each turn | Debounced (10 min) snapshot of the current rollout. |
| `SessionStart` hook | Codex CLI | Codex launch (`startup` or `resume`) | Walk recent rollouts, finalize any idle >10 min, commit + push. |

- **Claude** sessions are summarized by `claude -p` running Haiku 4.5.
- **Codex** sessions are summarized by `codex exec --ephemeral` running a cheap
  tier model (default `gpt-5.4-mini`). The `--ephemeral` flag prevents the
  summarizer call from itself producing a rollout.

Sessions with fewer than 3 user messages are skipped on both paths. The Codex
setup touches **only** `~/.codex/hooks.json` (preserving any pre-existing
user-owned hook entries) and the `[hooks.state]` block in `~/.codex/config.toml`
that records the user's trust of our hooks (so Codex doesn't prompt on first
launch). Every other section of `config.toml` — `model`, `notify`, `projects`,
`plugins`, other `[hooks.state]` entries for hooks the user trusted manually —
is untouched. Existing `notify` integrations (e.g., Computer Use) keep working.

Note: Codex finalize runs at the **next** Codex launch, not at session exit
(Codex CLI has no `SessionEnd` event as of 0.130.0). If you go a week without
opening Codex, the in-progress rollout from your last session won't push
until you launch Codex again.

Every error path logs to stderr and exits 0; a misbehaving snapshot never
blocks your session.

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
/everywhere-codex-setup      # Optional: Codex side (Stop + SessionStart hooks; cross-platform)
```

`/everywhere-setup` initializes `~/agent-memory/`, optionally creates a private
GitHub repo for sync, and verifies hooks are registered.
`/everywhere-codex-setup` stages the hook scripts to `~/.everywhere/hooks/`
(for stable absolute paths) and merges Codex `Stop` + `SessionStart` entries
into `~/.codex/hooks.json`. Requires `codex-cli >= 0.130.0` (the `hooks`
feature must be stable).

## Commands

Once installed, the plugin exposes:

| Command | Purpose |
|---|---|
| `/everywhere-setup` | One-time setup. Initializes memory repo + Claude Code hooks. |
| `/everywhere-codex-setup` | One-time setup for Codex CLI capture (registers `Stop` + `SessionStart` hooks). |
| `/everywhere-codex-uninstall` | Remove the Codex hook entries and staged scripts. |
| `/session-save` | Manually trigger a save of the current session (Claude or Codex auto-detected). |
| `/recall <query>` | Search past sessions across all projects and agents, ranked. |
| `/memory on` | Inject `INDEX.md` + the current project's `PROJECT.md` into context. |
| `/memory off` | (Reminder) injected memory persists in the current session — start a new one to drop it. |

### Dev-mode invocation (no command files)

If you run Everywhere from a checked-out repo instead of the marketplace install
(skill symlinked into `~/.claude/skills/everywhere`), the `commands/` directory
isn't loaded, so the bare `/recall`, `/session-save`, etc. won't be recognized
by Claude Code's slash parser. Invoke the skill directly instead:

```
/everywhere recall <query>
/everywhere session-save
/everywhere memory on
/everywhere memory off
/everywhere setup
/everywhere codex-setup
/everywhere codex-uninstall
```

The harness passes the trailing args to the `everywhere` skill, which routes
to the matching section in `SKILL.md`. Equivalent to the bare commands.

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
│           └── 2026-05-06-abc123def456/
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
- **Codex trigger:** Codex `Stop` and `SessionStart` hooks (stable since
  codex-cli 0.130.0). `Stop` fires per turn for incremental snapshots;
  `SessionStart` fires on `startup`/`resume` and runs the finalize sweep.
  Both hook commands fork a detached process (`nohup ... &`) so they return
  in milliseconds — Codex requires synchronous hook commands (the `async`
  flag is silently skipped), and a sync wrapper around `nohup` is the
  standard way to avoid blocking the TUI.
- **Staging directory:** Scripts are copied to `~/.everywhere/hooks/` so the
  registered hook commands in `~/.codex/hooks.json` have a stable absolute
  path — handy for dev-mode installs from a cloned repo. The TCC workaround
  that motivated this in the launchd era is no longer needed (Codex hooks
  run under the user, not launchd). Re-running setup refreshes the staged
  copies after a plugin update.
- **Multi-machine:** `SessionEnd` (Claude) and the Codex `SessionStart`
  finalize sweep both do `git pull --rebase --autostash origin main` before
  pushing. Concurrent edits from different machines reconcile automatically.
  True conflicts (rare, since each session writes its own dated folder) leave
  the local commit in place to be retried next session.
- **Failure mode:** every error path logs to stderr and exits 0. A hook will
  *never* prevent you from `/exit`-ing.

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `EVERYWHERE_DEBUG` | `0` | When `1`, hook logs progress to stderr (visible in the agent's status line). |
| `EVERYWHERE_CODEX_MODEL` | `gpt-5.4-mini` | Codex summarizer model. Export from your shell rc; the Codex hook commands inherit the parent Codex process's environment. |

Tunables live near the top of `hooks/snapshot.py` (Claude path) and
`hooks/codex_sweep.py` (shared rollout-handling library):

- `DEBOUNCE_SECONDS = 600` — minimum gap between non-final snapshots.
- `FINALITY_IDLE_SECONDS = 600` — Codex rollout idle threshold to trigger
  finalization.
- `MIN_USER_MESSAGES = 3` — sessions shorter than this are skipped.
- `MAX_CONVERSATION_CHARS = 60000` — transcript is tail-truncated to this size
  before being passed to the summarizer.
- `DEFAULT_MODEL = "claude-haiku-4-5"` — Claude summarizer model.
- `SCAN_DAYS = 7` — Codex `SessionStart` sweep only walks the last N days of rollouts.

## Repo

<https://github.com/iamNCJ/Everywhere>

## License

MIT
