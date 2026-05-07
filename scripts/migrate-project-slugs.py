#!/usr/bin/env python3
"""One-shot migration: rename ``~/agent-memory/projects/<basename>/`` to
``~/agent-memory/projects/<basename>-<hash8>/``.

Run once after upgrading to the slug-based naming scheme. Idempotent —
re-running does nothing once everything is migrated.

Usage:
    python3 scripts/migrate-project-slugs.py            # dry run (default)
    python3 scripts/migrate-project-slugs.py --apply    # perform renames
    python3 scripts/migrate-project-slugs.py --apply --commit  # also git-commit
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
    """Extract the ``**Path:** /abs/path`` line from PROJECT.md."""
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


def is_git_repo() -> bool:
    return (MEMORY_REPO / ".git").exists()


def rename_dir(old: Path, new: Path, *, use_git: bool) -> None:
    if use_git:
        git("mv", str(old.relative_to(MEMORY_REPO)), str(new.relative_to(MEMORY_REPO)))
    else:
        old.rename(new)


def update_meta_yaml(session_dir: Path, new_name: str) -> bool:
    """Rewrite ``project_name: "..."`` in meta.yaml. Returns True if changed."""
    meta = session_dir / "meta.yaml"
    if not meta.exists():
        return False
    text = meta.read_text()
    new_text, n = re.subn(
        r'^(project_name:\s*)"[^"]*"',
        lambda m: f'{m.group(1)}"{new_name}"',
        text,
        flags=re.MULTILINE,
    )
    if n == 0 or new_text == text:
        return False
    meta.write_text(new_text)
    return True


def update_index_md(rename_map: dict[str, str], apply: bool) -> bool:
    """Rewrite INDEX.md links. Returns True if file changed."""
    if not INDEX_MD.exists():
        return False
    text = INDEX_MD.read_text()
    new_text = text
    line_pat = re.compile(
        r"^- \[([^\]]+)\]\(projects/([^/]+)/PROJECT\.md\)\s+—\s+(.+?)\s*$",
        flags=re.MULTILINE,
    )

    def repl(m: re.Match) -> str:
        old_label, old_slug, path = m.group(1), m.group(2), m.group(3)
        new_slug = project_slug(os.path.abspath(path))
        if old_slug == new_slug:
            return m.group(0)
        rename_map.setdefault(old_slug, new_slug)
        return f"- [{new_slug}](projects/{new_slug}/PROJECT.md) — {path}"

    new_text = line_pat.sub(repl, text)
    changed = new_text != text
    if changed and apply:
        INDEX_MD.write_text(new_text)
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually perform renames (default: dry-run)")
    ap.add_argument("--commit", action="store_true", help="git commit after applying (requires --apply)")
    args = ap.parse_args()

    if not PROJECTS_DIR.exists():
        print(f"No projects dir at {PROJECTS_DIR} — nothing to migrate.")
        return 0

    use_git = is_git_repo()
    if not use_git:
        print(f"Note: {MEMORY_REPO} is not a git repo; using plain rename.", file=sys.stderr)

    plan: list[tuple[Path, Path, str]] = []  # (old_dir, new_dir, project_path)
    skipped: list[tuple[Path, str]] = []
    collisions: list[tuple[Path, Path]] = []

    for child in sorted(PROJECTS_DIR.iterdir()):
        if not child.is_dir():
            continue
        proj_path = read_project_path(child)
        if proj_path is None:
            skipped.append((child, "no **Path:** line in PROJECT.md"))
            continue
        new_slug = project_slug(os.path.abspath(proj_path))
        if child.name == new_slug:
            continue  # already migrated
        new_dir = PROJECTS_DIR / new_slug
        if new_dir.exists():
            collisions.append((child, new_dir))
            continue
        plan.append((child, new_dir, proj_path))

    print(f"=== Migration plan (memory repo: {MEMORY_REPO}) ===")
    print(f"Renames: {len(plan)}")
    for old, new, p in plan:
        print(f"  {old.name}  →  {new.name}    [{p}]")
    if skipped:
        print(f"\nSkipped ({len(skipped)}):")
        for d, reason in skipped:
            print(f"  {d.name}: {reason}")
    if collisions:
        print(f"\nCollisions ({len(collisions)}) — target slug already exists, manual review needed:")
        for old, new in collisions:
            print(f"  {old.name}  ✗  {new.name}")

    if not args.apply:
        print("\n(dry-run — pass --apply to execute)")
        return 0

    rename_map: dict[str, str] = {}
    for old, new, _ in plan:
        rename_dir(old, new, use_git=use_git)
        rename_map[old.name] = new.name
        # update meta.yaml in each session dir
        sessions_dir = new / "sessions"
        if sessions_dir.exists():
            for sd in sessions_dir.iterdir():
                if sd.is_dir():
                    update_meta_yaml(sd, new.name)

    index_changed = update_index_md(rename_map, apply=True)

    print(f"\nApplied {len(plan)} rename(s). INDEX.md {'updated' if index_changed else 'unchanged'}.")

    if args.commit and use_git:
        git("add", "-A")
        diff = git("diff", "--cached", "--quiet", check=False)
        if diff.returncode == 0:
            print("Nothing staged — skipping commit.")
        else:
            git("commit", "-m", "chore: migrate project slugs to basename-hash form")
            print("Committed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
