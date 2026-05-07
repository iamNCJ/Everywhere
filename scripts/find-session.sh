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
SLUG=$(CWD="$CWD" python3 -c "
import hashlib, os
cwd = os.environ.get('CWD', '')
abs_path = os.path.abspath(cwd) if cwd else ''
basename = os.path.basename(abs_path) or 'unknown'
h = hashlib.sha256(abs_path.encode('utf-8')).hexdigest()[:8]
print(f'{basename}-{h}')
")
echo "Project name: $SLUG"
