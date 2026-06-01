#!/usr/bin/env python3
"""Backfill missing Codex rollouts into Everywhere memory."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hooks.codex_repair import (  # noqa: E402
    backfill_candidate,
    collect_missing_codex_rollouts,
    commit_memory_repo,
)
from hooks.codex_sweep import load_cursor  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory-repo", default=str(Path.home() / "agent-memory"))
    parser.add_argument("--sessions-root", default=str(Path.home() / ".codex" / "sessions"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="perform writes")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--model", default=None)
    parser.add_argument(
        "--summarizer-agent",
        choices=("claude-code", "codex"),
        default="claude-code",
        help="agent backend used only to summarize historical transcripts",
    )
    parser.add_argument("--include-stubs", action="store_true")
    parser.add_argument("--no-push", action="store_true")
    parser.add_argument("--no-stub-on-failure", action="store_true")
    args = parser.parse_args()

    memory_repo = Path(args.memory_repo).expanduser()
    sessions_root = Path(args.sessions_root).expanduser()
    candidates = collect_missing_codex_rollouts(
        memory_repo,
        sessions_root,
        include_stub_summaries=args.include_stubs,
    )
    if args.limit:
        candidates = candidates[: args.limit]

    print(f"missing eligible Codex rollouts: {len(candidates)}")
    for candidate in candidates:
        print(
            f"{candidate.started_at[:10]} {candidate.session_short} "
            f"users={candidate.user_count} cwd={candidate.cwd}"
        )

    if args.dry_run:
        return 0
    if not args.yes:
        print("Refusing to write without --yes. Use --dry-run to inspect.", file=sys.stderr)
        return 2

    cursor_path = memory_repo / ".snapshots" / ".codex-cursor.json"
    cursor = load_cursor(cursor_path)
    written = []
    failed = []
    for index, candidate in enumerate(candidates, start=1):
        print(f"[{index}/{len(candidates)}] backfilling {candidate.session_id} ...", flush=True)
        try:
            session_dir = backfill_candidate(
                candidate,
                memory_repo,
                cursor,
                summarizer_agent=args.summarizer_agent,
                model=args.model,
                stub_on_failure=not args.no_stub_on_failure,
            )
            written.append(session_dir)
            print(f"  wrote {session_dir}", flush=True)
        except Exception as e:
            failed.append((candidate.session_id, e))
            print(f"  FAILED {candidate.session_id}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    if written:
        status = commit_memory_repo(
            memory_repo,
            f"repair: backfill {len(written)} missing Codex sessions",
            push=not args.no_push,
        )
        print(f"git: {status}")

    if failed:
        print(f"failures: {len(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
