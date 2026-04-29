#!/usr/bin/env bash
# SessionEnd hook: final session summary, git commit + push via claude -p (Haiku)
# Runs async — all failures are silent by design.

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"

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

# Extract started_at; use sentinel if unavailable (avoids midnight-rollover mismatch with Stop hook)
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
if ! [[ "$MSG_COUNT" =~ ^[0-9]+$ ]] || [ "$MSG_COUNT" -lt 3 ]; then
    exit 0
fi
TRANSCRIPT_TEXT=$(echo "$PARSED" | tail -n +3)

# Determine session dir and count prior Stop snapshots
DATE_STR=$(echo "$STARTED_AT" | cut -c1-10)
SESSION_SHORT=$(echo "$SESSION_ID" | cut -c1-6)
SESSION_DIR="$HOME/agent-memory/projects/${PROJECT_NAME}/sessions/${DATE_STR}-${SESSION_SHORT}"
SNAPSHOT_COUNT=0
if [ -d "$SESSION_DIR" ]; then
    # Count how many times the Stop hook wrote a snapshot (is_final: false meta.yamls)
    SNAPSHOT_COUNT=$(grep -l "is_final: false" "$SESSION_DIR/meta.yaml" 2>/dev/null | wc -l | tr -d ' ' || echo "0")
fi

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

# Write all session files via Python (handles quoting safely)
mkdir -p "$SESSION_DIR"

MEMORY_JSON="$MEMORY_JSON" \
SESSION_ID="$SESSION_ID" \
SESSION_SHORT="$SESSION_SHORT" \
PROJECT_PATH="$PROJECT_PATH" \
PROJECT_NAME="$PROJECT_NAME" \
STARTED_AT="$STARTED_AT" \
ENDED_AT="$ENDED_AT" \
SNAPSHOT_COUNT="$SNAPSHOT_COUNT" \
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

summary            = d.get('summary',            'Session complete.')
decisions          = d.get('decisions',           'No significant decisions.')
artifacts          = d.get('artifacts',           '## Files\n\n## Commands\n\n## References')
excerpts           = d.get('excerpts',            'No excerpts captured.')
tags               = d.get('tags', [])
summary_first      = d.get('summary_first_sentence', summary.split('.')[0])
tags_yaml          = '[' + ', '.join(str(t) for t in tags) + ']'

meta = f'''session_id: "{e['SESSION_ID']}"
session_id_short: "{e['SESSION_SHORT']}"
project_path: "{e['PROJECT_PATH']}"
project_name: "{e['PROJECT_NAME']}"
agent: claude-code
started_at: "{e['STARTED_AT']}"
ended_at: "{e['ENDED_AT']}"
is_final: true
snapshot_count: {e['SNAPSHOT_COUNT']}
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

# Write commit_msg to a temp file for the shell to read
with open(os.path.join(session_dir, '.commit_msg'), 'w') as f:
    f.write(summary_first)
PYEOF

# Read commit message written by Python (avoids shell variable escaping issues)
COMMIT_MSG=$(cat "$SESSION_DIR/.commit_msg" 2>/dev/null || echo "session complete")
rm -f "$SESSION_DIR/.commit_msg"

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

# Prepend session entry after the "## Recent Sessions" comment, keep last 10
python3 - "$PROJECT_MD" "$SESSION_LINK" << 'PYEOF'
import sys
path, entry = sys.argv[1], sys.argv[2]
with open(path) as f:
    content = f.read()
marker = "## Recent Sessions\n"
if marker in content:
    parts = content.split(marker, 1)
    rest = parts[1]
    lines = rest.split('\n')
    # Insert after the opening comment line
    insert_at = 0
    for i, line in enumerate(lines):
        if line.startswith('<!--'):
            insert_at = i + 1
            break
    lines.insert(insert_at, entry)
    # Keep only 10 session entries (newest first)
    session_lines = [l for l in lines if l.startswith('- [')]
    if len(session_lines) > 10:
        non_session = [l for l in lines if not l.startswith('- [')]
        session_lines = session_lines[:10]
        new_lines = []
        for l in non_session:
            new_lines.append(l)
            if l.startswith('<!--'):
                new_lines.extend(session_lines)
        lines = new_lines
    new_content = parts[0] + marker + '\n'.join(lines)
    with open(path, 'w') as f:
        f.write(new_content)
PYEOF

# Update INDEX.md (create if missing)
INDEX_MD="$HOME/agent-memory/INDEX.md"
if [ ! -f "$INDEX_MD" ]; then
    cat > "$INDEX_MD" << 'EOF'
# Everywhere — Global Memory Index

> Auto-maintained by Everywhere. Last updated by SessionEnd hook.

## Projects

EOF
fi
if ! grep -q "\[$PROJECT_NAME\]" "$INDEX_MD"; then
    echo "- [$PROJECT_NAME](projects/$PROJECT_NAME/PROJECT.md) — $PROJECT_PATH" >> "$INDEX_MD"
fi

# Git commit + push (fail silently if no remote configured)
git -C "$HOME/agent-memory" add -A 2>/dev/null || true
git -C "$HOME/agent-memory" commit -m "session: $PROJECT_NAME $SESSION_SHORT - $COMMIT_MSG" 2>/dev/null || true
git -C "$HOME/agent-memory" push origin main 2>/dev/null || true

# Cleanup debounce file
TIMESTAMP_FILE="$HOME/agent-memory/.snapshots/.last-$SESSION_ID"
[ -f "$TIMESTAMP_FILE" ] && rm -f "$TIMESTAMP_FILE"
