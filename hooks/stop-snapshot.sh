#!/usr/bin/env bash
# Stop hook: debounced session snapshot via claude -p (Haiku)
# Runs async — all failures are silent by design.

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"

# Read hook input
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || true)
[ -z "$SESSION_ID" ] && exit 0

# Debounce: skip if snapshot taken within last 10 minutes
SNAPSHOTS_DIR="$HOME/agent-memory/.snapshots"
mkdir -p "$SNAPSHOTS_DIR"
TIMESTAMP_FILE="$SNAPSHOTS_DIR/.last-$SESSION_ID"
if [ -f "$TIMESTAMP_FILE" ]; then
    LAST=$(cat "$TIMESTAMP_FILE" 2>/dev/null || echo "0")
    NOW=$(date +%s)
    [ $((NOW - LAST)) -lt 600 ] && exit 0
fi

# Find transcript
TRANSCRIPT=$(find ~/.claude/projects -name "${SESSION_ID}.jsonl" 2>/dev/null | head -1 || true)
[ -z "$TRANSCRIPT" ] && exit 0

# Extract project info using env var to avoid shell injection
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

# Extract started_at from first timestamped entry; use sentinel if unavailable
# timestamp field is an ISO 8601 string (e.g. "2026-04-29T06:46:00.903Z")
STARTED_AT=$(TRANSCRIPT_PATH="$TRANSCRIPT" python3 -c "
import json, os
with open(os.environ['TRANSCRIPT_PATH']) as f:
    for line in f:
        try:
            d = json.loads(line.strip())
            ts = d.get('timestamp')
            if ts and isinstance(ts, str):
                print(ts[:19] + 'Z')
                break
        except: continue
" 2>/dev/null || true)
[ -z "$STARTED_AT" ] && STARTED_AT="unknown"

# Parse conversation: count user messages, collect text
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
print('\n\n'.join(msgs[:40]))
" 2>/dev/null || echo "0")

MSG_COUNT=$(echo "$PARSED" | head -1)
if ! [[ "$MSG_COUNT" =~ ^[0-9]+$ ]] || [ "$MSG_COUNT" -lt 3 ]; then
    exit 0
fi
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

# Parse JSON and write session files via Python (handles quoting safely)
DATE_STR=$(echo "$STARTED_AT" | cut -c1-10)
SESSION_SHORT=$(echo "$SESSION_ID" | cut -c1-6)
SESSION_DIR="$HOME/agent-memory/projects/${PROJECT_NAME}/sessions/${DATE_STR}-${SESSION_SHORT}"
mkdir -p "$SESSION_DIR"

MEMORY_JSON="$MEMORY_JSON" \
SESSION_ID="$SESSION_ID" \
SESSION_SHORT="$SESSION_SHORT" \
PROJECT_PATH="$PROJECT_PATH" \
PROJECT_NAME="$PROJECT_NAME" \
STARTED_AT="$STARTED_AT" \
SESSION_DIR="$SESSION_DIR" \
python3 << 'PYEOF'
import json, os

e = os.environ
session_dir = e['SESSION_DIR']
raw = e.get('MEMORY_JSON', '{}')

try:
    d = json.loads(raw)
except Exception:
    d = {}

summary   = d.get('summary',   'Session snapshot.')
decisions = d.get('decisions', 'No significant decisions yet.')
artifacts = d.get('artifacts', '## Files\n\n## Commands\n\n## References')
excerpts  = d.get('excerpts',  'No excerpts captured.')
tags      = d.get('tags', [])
tags_yaml = '[' + ', '.join(str(t) for t in tags) + ']'

# meta.yaml: quote all string values to handle paths with spaces/colons
meta = f'''session_id: "{e['SESSION_ID']}"
session_id_short: "{e['SESSION_SHORT']}"
project_path: "{e['PROJECT_PATH']}"
project_name: "{e['PROJECT_NAME']}"
agent: claude-code
started_at: "{e['STARTED_AT']}"
is_final: false
tags: {tags_yaml}
'''

with open(os.path.join(session_dir, 'meta.yaml'), 'w') as f:
    f.write(meta)
with open(os.path.join(session_dir, 'summary.md'), 'w') as f:
    f.write(summary + '\n')
with open(os.path.join(session_dir, 'decisions.md'), 'w') as f:
    f.write(decisions + '\n')
with open(os.path.join(session_dir, 'artifacts.md'), 'w') as f:
    f.write(artifacts + '\n')
with open(os.path.join(session_dir, 'excerpts.md'), 'w') as f:
    f.write(excerpts + '\n')
PYEOF

# Update debounce timestamp
date +%s > "$TIMESTAMP_FILE"
