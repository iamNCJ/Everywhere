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
