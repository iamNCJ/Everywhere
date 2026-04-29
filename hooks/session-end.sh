#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(dirname "$(dirname "$(realpath "$0")")")}"

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || true)
[ -z "$SESSION_ID" ] && exit 0

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

ENDED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Parse conversation
PARSED=$(TRANSCRIPT_PATH="$TRANSCRIPT" python3 -c "
import json, os
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
print('\n\n'.join(msgs[:60]))
" 2>/dev/null || echo "0")

MSG_COUNT=$(echo "$PARSED" | head -1)
[ "${MSG_COUNT:-0}" -lt 3 ] && exit 0
TRANSCRIPT_TEXT=$(echo "$PARSED" | tail -n +3)

# Determine session dir and snapshot_count
DATE_STR=$(echo "$STARTED_AT" | cut -c1-10)
SESSION_SHORT=$(echo "$SESSION_ID" | cut -c1-6)
SESSION_DIR="$HOME/agent-memory/projects/${PROJECT_NAME}/sessions/${DATE_STR}-${SESSION_SHORT}"
SNAPSHOT_COUNT=0
[ -f "$SESSION_DIR/meta.yaml" ] && SNAPSHOT_COUNT=1

# Generate final memory via claude -p
PROMPT_FILE="$PLUGIN_ROOT/hooks/session-end-prompt.md"
MEMORY_JSON=$(echo "$TRANSCRIPT_TEXT" | claude -p "$(cat "$PROMPT_FILE")

The complete session transcript is provided above. Return ONLY valid JSON with these keys:
- summary: string (thorough 3-5 sentences covering problem, approach, outcome, anything incomplete)
- decisions: string (complete markdown with sub-headings if >5 decisions)
- artifacts: string (markdown with ## Files, ## Commands, ## References sections)
- excerpts: string (3-5 most valuable Q&A pairs, verbatim quotes only)
- tags: array of 5-8 strings
- summary_first_sentence: string (just the first sentence of summary, for commit message)" \
    --model claude-haiku-4-5-20251001 2>/dev/null || echo '{}')

SUMMARY=$(echo "$MEMORY_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('summary','Session complete.'))" 2>/dev/null || echo "Session complete.")
DECISIONS=$(echo "$MEMORY_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('decisions','No significant decisions.'))" 2>/dev/null || echo "No significant decisions.")
ARTIFACTS=$(echo "$MEMORY_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('artifacts','## Files\n\n## Commands\n\n## References'))" 2>/dev/null || echo "## Files\n\n## Commands\n\n## References")
EXCERPTS=$(echo "$MEMORY_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('excerpts','No excerpts captured.'))" 2>/dev/null || echo "No excerpts captured.")
TAGS=$(echo "$MEMORY_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('tags',[]))" 2>/dev/null || echo "[]")
COMMIT_MSG=$(echo "$MEMORY_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('summary_first_sentence','session complete'))" 2>/dev/null || echo "session complete")

# Write session files
mkdir -p "$SESSION_DIR"

cat > "$SESSION_DIR/meta.yaml" << EOF
session_id: $SESSION_ID
session_id_short: $SESSION_SHORT
project_path: $PROJECT_PATH
project_name: $PROJECT_NAME
agent: claude-code
started_at: $STARTED_AT
ended_at: $ENDED_AT
is_final: true
snapshot_count: $SNAPSHOT_COUNT
tags: $TAGS
EOF

printf '%s\n' "$SUMMARY" > "$SESSION_DIR/summary.md"
printf '%s\n' "$DECISIONS" > "$SESSION_DIR/decisions.md"
printf '%s\n' "$ARTIFACTS" > "$SESSION_DIR/artifacts.md"
printf '%s\n' "$EXCERPTS" > "$SESSION_DIR/excerpts.md"

# Update PROJECT.md
PROJECT_MD="$HOME/agent-memory/projects/${PROJECT_NAME}/PROJECT.md"
SESSION_LINK="- [${DATE_STR} — ${COMMIT_MSG}](sessions/${DATE_STR}-${SESSION_SHORT}/summary.md)"

if [ ! -f "$PROJECT_MD" ]; then
    cat > "$PROJECT_MD" << EOF
# ${PROJECT_NAME}

**Path:** ${PROJECT_PATH}

## Recent Sessions

<!-- Sessions listed below, newest first. Maintained by Everywhere. -->

## Project Overview

<!-- Updated manually or by /recall when patterns emerge -->
EOF
fi

# Prepend session entry after "## Recent Sessions" line
python3 - "$PROJECT_MD" "$SESSION_LINK" << 'PYEOF'
import sys
path, entry = sys.argv[1], sys.argv[2]
with open(path) as f:
    content = f.read()
marker = "## Recent Sessions\n"
if marker in content:
    parts = content.split(marker, 1)
    # Find insertion point (after comment line if present)
    rest = parts[1]
    lines = rest.split('\n')
    insert_at = 0
    for i, line in enumerate(lines):
        if line.startswith('<!--'):
            insert_at = i + 1
            break
    lines.insert(insert_at, entry)
    # Keep only 10 session entries
    session_lines = [l for l in lines if l.startswith('- [')]
    if len(session_lines) > 10:
        # Remove oldest (last) entries
        non_session = [l for l in lines if not l.startswith('- [')]
        session_lines = session_lines[:10]
        lines = []
        for l in non_session:
            lines.append(l)
            if l.startswith('<!--'):
                lines.extend(session_lines)
    new_content = parts[0] + marker + '\n'.join(lines)
    with open(path, 'w') as f:
        f.write(new_content)
PYEOF

# Update INDEX.md
INDEX_MD="$HOME/agent-memory/INDEX.md"
if [ -f "$INDEX_MD" ] && ! grep -q "\[$PROJECT_NAME\]" "$INDEX_MD"; then
    echo "- [$PROJECT_NAME](projects/$PROJECT_NAME/PROJECT.md) — $PROJECT_PATH" >> "$INDEX_MD"
fi

# Git commit + push
git -C "$HOME/agent-memory" add -A 2>/dev/null || true
git -C "$HOME/agent-memory" commit -m "session: $PROJECT_NAME $SESSION_SHORT - $COMMIT_MSG" 2>/dev/null || true
git -C "$HOME/agent-memory" push origin main 2>/dev/null || true  # fail silently if no remote

# Cleanup debounce file
TIMESTAMP_FILE="$HOME/agent-memory/.snapshots/.last-$SESSION_ID"
[ -f "$TIMESTAMP_FILE" ] && rm -f "$TIMESTAMP_FILE"
