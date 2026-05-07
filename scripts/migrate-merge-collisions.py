#!/usr/bin/env python3
"""Resolve collisions left by migrate-project-slugs.py.

When the new slug dir already existed (because the slug-aware hook had
already run before migration), the renamer skipped that pair. This script
merges the legacy dir into the new slug dir:

* sessions present only in the legacy dir are moved over (with meta.yaml
  rewritten to the new slug)
* sessions present in both are kept as-is in the new dir (newer copy wins)
* PROJECT.md entries are merged, deduped, sorted newest first
* the legacy dir is removed
* duplicate INDEX.md entries are deduped (keep the new-slug entry)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

MEMORY_REPO = Path.home() / "agent-memory"
PROJECTS_DIR = MEMORY_REPO / "projects"
INDEX_MD = MEMORY_REPO / "INDEX.md"


def project_slug(abs_path: str) -> str:
    basename = os.path.basename(abs_path) or "unknown"
    h = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:8]
    return f"{basename}-{h}"


def read_project_path(project_dir: Path) -> str | None:
    pmd = project_dir / "PROJECT.md"
    if not pmd.exists():
        return None
    for line in pmd.read_text().splitlines():
        m = re.match(r"\*\*Path:\*\*\s+(.+?)\s*$", line)
        if m:
            return m.group(1).strip()
    return None


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(MEMORY_REPO), *args],
        check=check, capture_output=True, text=True,
    )


def update_meta_slug(session_dir: Path, new_name: str) -> None:
    meta = session_dir / "meta.yaml"
    if not meta.exists():
        return
    text = meta.read_text()
    new_text, n = re.subn(
        r'^(project_name:\s*)"[^"]*"',
        lambda m: f'{m.group(1)}"{new_name}"',
        text, flags=re.MULTILINE,
    )
    if n and new_text != text:
        meta.write_text(new_text)


SESSION_LINE = re.compile(
    r"^- \[(?P<date>\d{4}-\d{2}-\d{2}) — (?P<rest>.+?)\]\(sessions/(?P<sid>[\d-]+-[\w]+)/summary\.md\)\s*$",
)


def parse_sessions_block(text: str) -> tuple[list[str], list[str], list[str]]:
    """Split PROJECT.md into (head, session_lines, tail) around the recent-sessions block."""
    lines = text.split("\n")
    head, sessions, tail = [], [], []
    state = "head"
    for ln in lines:
        if state == "head":
            head.append(ln)
            if ln.strip().startswith("<!-- Sessions listed below"):
                state = "sessions"
            continue
        if state == "sessions":
            if SESSION_LINE.match(ln):
                sessions.append(ln)
                continue
            # blank line allowed inside the block; first non-session, non-blank ends it
            if ln.strip() == "":
                # peek ahead — if next non-blank is another section, end; else allow
                # simpler: a blank line ends the block
                tail.append(ln)
                state = "tail"
                continue
            tail.append(ln)
            state = "tail"
            continue
        tail.append(ln)
    return head, sessions, tail


def merge_project_md(legacy: Path, new: Path, new_slug: str, project_path: str) -> None:
    """Merge sessions block from legacy PROJECT.md into new PROJECT.md, dedupe by session id."""
    new_md = new / "PROJECT.md"
    legacy_md = legacy / "PROJECT.md"
    new_text = new_md.read_text() if new_md.exists() else (
        f"# {new_slug}\n\n**Path:** {project_path}\n\n## Recent Sessions\n"
        "<!-- Sessions listed below, newest first. Maintained by Everywhere. -->\n\n"
        "## Project Overview\n\n<!-- Updated manually or by /recall when patterns emerge -->\n"
    )
    legacy_text = legacy_md.read_text() if legacy_md.exists() else ""
    n_head, n_sessions, n_tail = parse_sessions_block(new_text)
    _, l_sessions, _ = parse_sessions_block(legacy_text) if legacy_text else ([], [], [])

    seen_sids: set[str] = set()
    merged: list[tuple[str, str, str]] = []  # (date, sid, line)
    for ln in n_sessions + l_sessions:
        m = SESSION_LINE.match(ln)
        if not m:
            continue
        sid = m.group("sid")
        if sid in seen_sids:
            continue
        seen_sids.add(sid)
        merged.append((m.group("date"), sid, ln))
    merged.sort(key=lambda t: t[0], reverse=True)

    out = "\n".join(n_head + [t[2] for t in merged] + n_tail)
    if not out.endswith("\n"):
        out += "\n"
    new_md.write_text(out)


def dedupe_index() -> bool:
    if not INDEX_MD.exists():
        return False
    text = INDEX_MD.read_text()
    seen: set[str] = set()
    out = []
    changed = False
    for ln in text.split("\n"):
        m = re.match(r"^- \[([^\]]+)\]\(projects/([^/]+)/PROJECT\.md\)", ln)
        if m:
            slug = m.group(2)
            if slug in seen:
                changed = True
                continue
            seen.add(slug)
        out.append(ln)
    if changed:
        INDEX_MD.write_text("\n".join(out))
    return changed


def find_collisions() -> list[tuple[Path, Path, str]]:
    """Return [(legacy_dir, new_dir, project_path), ...] for unmigrated dirs whose slug dir exists."""
    out = []
    for child in sorted(PROJECTS_DIR.iterdir()):
        if not child.is_dir():
            continue
        proj_path = read_project_path(child)
        if proj_path is None:
            continue
        slug = project_slug(os.path.abspath(proj_path))
        if child.name == slug:
            continue  # already migrated
        new_dir = PROJECTS_DIR / slug
        if not new_dir.exists():
            continue  # not a collision; the previous migration would have handled it
        out.append((child, new_dir, proj_path))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    collisions = find_collisions()
    print(f"=== Collision merges (memory repo: {MEMORY_REPO}) ===")
    if not collisions:
        print("None — nothing to do.")
    for legacy, new, p in collisions:
        legacy_sids = {q.name for q in (legacy / "sessions").iterdir()} if (legacy / "sessions").exists() else set()
        new_sids = {q.name for q in (new / "sessions").iterdir()} if (new / "sessions").exists() else set()
        only_legacy = legacy_sids - new_sids
        both = legacy_sids & new_sids
        print(f"  {legacy.name}  →  {new.name}    [{p}]")
        print(f"    move from legacy: {sorted(only_legacy) or '(none)'}")
        print(f"    drop from legacy (also in new, keep new copy): {sorted(both) or '(none)'}")

    dup_in_index = False
    if INDEX_MD.exists():
        seen, dups = set(), 0
        for ln in INDEX_MD.read_text().split("\n"):
            m = re.match(r"^- \[([^\]]+)\]\(projects/([^/]+)/PROJECT\.md\)", ln)
            if not m:
                continue
            slug = m.group(2)
            if slug in seen:
                dups += 1
            seen.add(slug)
        if dups:
            print(f"\nINDEX.md has {dups} duplicate project entr(ies) — will dedupe.")
            dup_in_index = True

    if not args.apply:
        print("\n(dry-run — pass --apply to execute)")
        return 0

    for legacy, new, _ in collisions:
        new_sessions_dir = new / "sessions"
        new_sessions_dir.mkdir(parents=True, exist_ok=True)
        legacy_sessions_dir = legacy / "sessions"
        if legacy_sessions_dir.exists():
            for sd in sorted(legacy_sessions_dir.iterdir()):
                if not sd.is_dir():
                    continue
                target = new_sessions_dir / sd.name
                rel_src = sd.relative_to(MEMORY_REPO)
                if target.exists():
                    # duplicate — drop legacy copy, keep new
                    git("rm", "-rf", str(rel_src))
                else:
                    rel_dst = target.relative_to(MEMORY_REPO)
                    git("mv", str(rel_src), str(rel_dst))
                    update_meta_slug(target, new.name)
        # merge PROJECT.md (after sessions moved so legacy still has its file)
        merge_project_md(legacy, new, new.name, read_project_path(legacy) or "")
        # remove legacy dir contents
        # legacy/PROJECT.md and any leftovers
        for f in legacy.rglob("*"):
            if f.is_file():
                git("rm", "-f", str(f.relative_to(MEMORY_REPO)))
        # remove now-empty dirs
        for d in sorted(legacy.rglob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        if legacy.exists() and not any(legacy.iterdir()):
            legacy.rmdir()
        # stage the merged PROJECT.md
        git("add", str((new / "PROJECT.md").relative_to(MEMORY_REPO)))

    if dup_in_index:
        if dedupe_index():
            git("add", str(INDEX_MD.relative_to(MEMORY_REPO)))

    if args.commit:
        diff = git("diff", "--cached", "--quiet", check=False)
        if diff.returncode == 0:
            print("Nothing staged — skipping commit.")
        else:
            git("commit", "-m", "chore: merge slug-collision project dirs")
            print("Committed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
