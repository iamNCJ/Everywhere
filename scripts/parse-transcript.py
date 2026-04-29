#!/usr/bin/env python3
"""Extract user/assistant conversation from a CC session JSONL."""
import json, sys

def parse_transcript(jsonl_path):
    messages = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_type = entry.get("type")
            message = entry.get("message", {})
            if msg_type == "user":
                content = message.get("content", "")
                if isinstance(content, str) and content.strip():
                    messages.append({"role": "user", "text": content.strip()})
                elif isinstance(content, list):
                    text = " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ).strip()
                    if text:
                        messages.append({"role": "user", "text": text})
            elif msg_type == "assistant":
                content = message.get("content", [])
                if isinstance(content, list):
                    text = " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ).strip()
                    if text:
                        messages.append({"role": "assistant", "text": text})
    return messages

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: parse-transcript.py <path-to-session.jsonl>")
        sys.exit(1)
    try:
        msgs = parse_transcript(sys.argv[1])
    except FileNotFoundError:
        print(f"Error: file not found: {sys.argv[1]}", file=sys.stderr)
        sys.exit(1)
    print(f"Found {len(msgs)} messages")
    for m in msgs[:3]:
        print(f"\n[{m['role'].upper()}] {m['text'][:200]}")
