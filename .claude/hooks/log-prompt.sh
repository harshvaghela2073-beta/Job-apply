#!/bin/bash
# UserPromptSubmit hook: appends every prompt to prompts/PROMPTS.md for future reference.
# Prints nothing to stdout (stdout from this hook would be injected into Claude's context).
set -euo pipefail

root="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
log="$root/prompts/PROMPTS.md"
mkdir -p "$(dirname "$log")"

input="$(cat)"
# Drop editor context the IDE prepends (e.g. <ide_opened_file>…</ide_opened_file>); keep only what the user typed.
prompt="$(printf '%s' "$input" | jq -r '.prompt // empty' | perl -0pe 's/<ide_[a-z_]+>.*?<\/ide_[a-z_]+>\n?//gs')"
[ -z "$prompt" ] && exit 0
session="$(printf '%s' "$input" | jq -r '.session_id // "unknown"')"

{
  printf '\n---\n\n## %s · session `%s`\n\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "${session:0:8}"
  printf '%s\n' "$prompt"
} >> "$log"
