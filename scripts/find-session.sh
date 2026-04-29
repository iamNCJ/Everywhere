#!/usr/bin/env bash
SESSION_ID="${1:?Usage: find-session.sh <session_id>}"
TRANSCRIPT=$(find ~/.claude/projects -name "${SESSION_ID}.jsonl" 2>/dev/null | head -1)
if [ -z "$TRANSCRIPT" ]; then
    echo "Session not found: $SESSION_ID" >&2
    exit 1
fi
echo "$TRANSCRIPT"

# Also extract project info
CWD=$(TRANSCRIPT_PATH="$TRANSCRIPT" python3 -c "
import json, os
with open(os.environ['TRANSCRIPT_PATH']) as f:
    for line in f:
        try:
            d = json.loads(line.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if d.get('cwd'):
            print(d['cwd'])
            break
" 2>/dev/null)
if [ -z "$CWD" ]; then
    echo "Warning: could not extract CWD from transcript" >&2
fi
echo "CWD: $CWD"
echo "Project name: $(basename "$CWD")"
