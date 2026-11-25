#!/bin/sh
# Stop: run the fast test suite; give Claude one more turn if it fails.
input=$(cat)
printf '%s' "$input" | grep -Eq '"stop_hook_active"[[:space:]]*:[[:space:]]*true' && exit 0
cd "$CLAUDE_PROJECT_DIR" || exit 0
[ -x .venv/bin/pytest ] || exit 0          # no test runner: do nothing
out=$(.venv/bin/pytest -q -x 2>&1) && exit 0
printf 'Tests fail. Fix them, or say why they are expected to fail:\n%s\n' "$(printf '%s\n' "$out" | tail -20)" >&2
exit 2
