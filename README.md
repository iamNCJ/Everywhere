# Everywhere

> *Your Claude Code sessions, accessible everywhere.*

Everywhere is a Claude Code plugin that auto-saves every session you have with
Claude into a local git repo at `~/agent-memory/`, optionally synced to GitHub.
Past sessions become searchable across projects and machines, so context never
gets stranded on whichever laptop happened to host the original conversation.

## Why

Claude Code sessions are ephemeral — when you `/exit`, the conversation is gone.
You can scroll back through `~/.claude/projects/` JSONL transcripts, but they're
raw and untaggued, not browsable. Everywhere fixes that:

- **Auto-summarize** every session into structured Markdown (summary, decisions,
  artifacts, verbatim excerpts, tags).
- **One repo per user, one folder per project**, so context is local-first and
  trivially greppable.
- **Cross-machine** — push to GitHub, pull on another laptop, and `/recall` your
  past work from anywhere.

## How it works

Two hooks fire automatically:

| Hook | When | Action |
|---|---|---|
| `Stop` | End of each Claude turn | Debounced (10 min) snapshot to local repo. |
| `SessionEnd` | `/exit` or session close | Final snapshot, commit, `pull --rebase` + push. |

Summaries are produced by `claude -p` running Haiku 4.5 — a few cents per session,
no extra API key needed. Sessions with fewer than 3 user messages are skipped.

The hook is fully async and exits 0 on any error, so a misbehaving snapshot never
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
/everywhere-setup
```

…which initializes `~/agent-memory/`, optionally creates a private GitHub repo
for sync, and verifies hooks are registered.

## Commands

Once installed, the plugin exposes:

| Command | Purpose |
|---|---|
| `/everywhere-setup` | One-time setup. Initializes memory repo + hooks. |
| `/session-save` | Manually trigger a save of the current session. |
| `/recall <query>` | Search past sessions across all projects, ranked. |
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

- **Hook type:** `command` + `async: true`. The hook spawns `python3 snapshot.py`
  which calls `claude -p` only for the summary step. `type: agent` was tried but
  is documented as experimental; the command path is deterministic and survives
  parent-process exit reliably.
- **Multi-machine:** `SessionEnd` does `git pull --rebase --autostash origin main`
  before pushing. Concurrent edits from different machines reconcile automatically.
  True conflicts (rare, since each session writes its own dated folder) leave the
  local commit in place to be retried next session.
- **Failure mode:** every error path logs to stderr and exits 0. The hook will
  *never* prevent you from `/exit`-ing.

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `EVERYWHERE_DEBUG` | `0` | When `1`, hook logs progress to stderr (visible in Claude Code's status line). |

Tunables live near the top of `hooks/snapshot.py`:

- `DEBOUNCE_SECONDS = 600` — minimum gap between non-final snapshots.
- `MIN_USER_MESSAGES = 3` — sessions shorter than this are skipped.
- `MAX_CONVERSATION_CHARS = 60000` — transcript is tail-truncated to this size
  before being passed to `claude -p`.
- `DEFAULT_MODEL = "claude-haiku-4-5"` — model used for summarization.

## Repo

<https://github.com/iamNCJ/Everywhere>

## License

MIT
