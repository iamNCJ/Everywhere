#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(dirname "$(dirname "$(realpath "$0")")")}"

# Read hook input
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || true)
[ -z "$SESSION_ID" ] && exit 0

# Debounce check
SNAPSHOTS_DIR="$HOME/agent-memory/.snapshots"
mkdir -p "$SNAPSHOTS_DIR"
TIMESTAMP_FILE="$SNAPSHOTS_DIR/.last-$SESSION_ID"
if [ -f "$TIMESTAMP_FILE" ]; then
    LAST=$(cat "$TIMESTAMP_FILE")
    NOW=$(date +%s)
    [ $((NOW - LAST)) -lt 600 ] && exit 0
fi

# Find transcript
TRANSCRIPT=$(find ~/.claude/projects -name "${SESSION_ID}.jsonl" 2>/dev/null | head -1 || true)
[ -z "$TRANSCRIPT" ] && exit 0

# Extract project info
PROJECT_PATH=$(TRANSCRIPT_PATH="$TRANSCRIPT" python3 -c "
import json, os
with open(os.environ['TRANSCRIPT_PATH']) as f:
    for line in f:
        try:
            d = json.loads(line.strip())
            if d.get('cwd'):
                print(d['cwd'])
                break
        except: continue
" 2>/dev/null || true)
[ -z "$PROJECT_PATH" ] && exit 0
PROJECT_NAME=$(basename "$PROJECT_PATH")

# Extract started_at
STARTED_AT=$(TRANSCRIPT_PATH="$TRANSCRIPT" python3 -c "
import json, os
from datetime import datetime, timezone
with open(os.environ['TRANSCRIPT_PATH']) as f:
    for line in f:
        try:
            d = json.loads(line.strip())
            ts = d.get('timestamp')
            if ts:
                if ts > 1e10: ts = ts / 1000
                print(datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
                break
        except: continue
" 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)

# Parse conversation text and count messages
PARSED=$(TRANSCRIPT_PATH="$TRANSCRIPT" python3 -c "
import json, os, sys
msgs = []
count = 0
with open(os.environ['TRANSCRIPT_PATH']) as f:
    for line in f:
        try:
            d = json.loads(line.strip())
            if d.get('type') == 'user':
                content = d.get('message', {}).get('content', '')
                if isinstance(content, str) and content.strip():
                    count += 1
                    msgs.append('USER: ' + content.strip()[:800])
                elif isinstance(content, list):
                    text = ' '.join(b.get('text','') for b in content if isinstance(b,dict) and b.get('type')=='text').strip()
                    if text:
                        count += 1
                        msgs.append('USER: ' + text[:800])
            elif d.get('type') == 'assistant':
                content = d.get('message', {}).get('content', [])
                if isinstance(content, list):
                    text = ' '.join(b.get('text','') for b in content if isinstance(b,dict) and b.get('type')=='text').strip()
                    if text:
                        msgs.append('ASSISTANT: ' + text[:800])
        except: continue
print(count)
print('---TRANSCRIPT---')
print('\n\n'.join(msgs[:40]))
" 2>/dev/null || echo "0")

MSG_COUNT=$(echo "$PARSED" | head -1)
[ "${MSG_COUNT:-0}" -lt 3 ] && exit 0

TRANSCRIPT_TEXT=$(echo "$PARSED" | tail -n +3)

# Generate session memory via claude -p (single call, returns JSON)
PROMPT_FILE="$PLUGIN_ROOT/hooks/stop-snapshot-prompt.md"
MEMORY_JSON=$(echo "$TRANSCRIPT_TEXT" | claude -p "$(cat "$PROMPT_FILE")

The session transcript is provided above. Return ONLY valid JSON with these keys:
- summary: string (3-5 sentences)
- decisions: string (markdown bullet list, or 'No significant decisions yet.')
- artifacts: string (markdown with ## Files, ## Commands, ## References sections)
- excerpts: string (2-3 most valuable Q&A verbatim quotes)
- tags: array of 3-5 strings" \
    --model claude-haiku-4-5-20251001 2>/dev/null || echo '{}')

# Parse JSON fields
SUMMARY=$(echo "$MEMORY_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('summary','Session snapshot.'))" 2>/dev/null || echo "Session snapshot.")
DECISIONS=$(echo "$MEMORY_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('decisions','No significant decisions yet.'))" 2>/dev/null || echo "No significant decisions yet.")
ARTIFACTS=$(echo "$MEMORY_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('artifacts','## Files\n\n## Commands\n\n## References'))" 2>/dev/null || echo "## Files\n\n## Commands\n\n## References")
EXCERPTS=$(echo "$MEMORY_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('excerpts','No excerpts captured.'))" 2>/dev/null || echo "No excerpts captured.")
TAGS=$(echo "$MEMORY_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('tags',[]))" 2>/dev/null || echo "[]")

# Create session directory
DATE_STR=$(echo "$STARTED_AT" | cut -c1-10)
SESSION_SHORT=$(echo "$SESSION_ID" | cut -c1-6)
SESSION_DIR="$HOME/agent-memory/projects/${PROJECT_NAME}/sessions/${DATE_STR}-${SESSION_SHORT}"
mkdir -p "$SESSION_DIR"

# Write files
cat > "$SESSION_DIR/meta.yaml" << EOF
session_id: $SESSION_ID
session_id_short: $SESSION_SHORT
project_path: $PROJECT_PATH
project_name: $PROJECT_NAME
agent: claude-code
started_at: $STARTED_AT
is_final: false
tags: $TAGS
EOF

printf '%s\n' "$SUMMARY" > "$SESSION_DIR/summary.md"
printf '%s\n' "$DECISIONS" > "$SESSION_DIR/decisions.md"
printf '%s\n' "$ARTIFACTS" > "$SESSION_DIR/artifacts.md"
printf '%s\n' "$EXCERPTS" > "$SESSION_DIR/excerpts.md"

# Update debounce timestamp
date +%s > "$TIMESTAMP_FILE"
