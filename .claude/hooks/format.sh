#!/bin/sh
# PostToolUse(Edit|Write): format edited Python files with the project's ruff.
command -v python3 >/dev/null || exit 0      # tool missing: do nothing, never block
[ -x "$CLAUDE_PROJECT_DIR/.venv/bin/ruff" ] || exit 0
f=$(python3 -I -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))')
case "$f" in
  *.py) "$CLAUDE_PROJECT_DIR/.venv/bin/ruff" format -q "$f" ;;
esac
exit 0
