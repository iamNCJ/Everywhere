#!/usr/bin/env bash
SESSION_ID="${1:?Usage: find-session.sh <session_id>}"
TRANSCRIPT=$(find ~/.claude/projects -name "${SESSION_ID}.jsonl" 2>/dev/null | head -1)
if [ -z "$TRANSCRIPT" ]; then
    echo "Session not found: $SESSION_ID" >&2
    exit 1
fi
echo "$TRANSCRIPT"

# Also extract project info
CWD=$(python3 -c "
import json
with open('$TRANSCRIPT') as f:
    for line in f:
        d = json.loads(line.strip())
        if d.get('cwd'):
            print(d['cwd'])
            break
" 2>/dev/null)
echo "CWD: $CWD"
echo "Project name: $(basename "$CWD")"
