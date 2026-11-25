#!/usr/bin/env bash
# The checks CI runs: tests, lint, format, JS syntax, and (when a base revision is
# given) aihealth delta. Run locally from the repo root: scripts/check.sh [BASE_REV]
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-.venv/bin/python}
base=${1:-}

echo "== ruff"
"$PY" -m ruff check .
"$PY" -m ruff format --check .

echo "== js syntax"
if command -v node >/dev/null 2>&1; then
  for f in web/js/*.js; do node --check "$f"; done
else
  echo "node not found; skipped"
fi

echo "== pytest"
"$PY" -m pytest -q

if [[ -n "$base" ]]; then
  echo "== aihealth delta $base..HEAD"
  if command -v aihealth >/dev/null 2>&1; then
    aihealth delta --base "$base" --head HEAD
  else
    echo "aihealth not installed; skipped"
  fi
fi
echo "all checks passed"
