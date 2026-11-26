#!/bin/sh
# Runs on the server from the deployed directory (called by `make deploy`).
# Creates the venv and the token on first run, installs the unit, restarts.
set -eu
cd "$(dirname "$0")/.."

test -x .venv/bin/python || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
# diagrams in agent replies; without it they show as code, so a failure isn't fatal
python3 scripts/vendor_mermaid.py || echo "mermaid not fetched; diagrams will show as code"

if [ ! -f .env ]; then
  umask 077
  echo "SIMBENCH_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" > .env
  echo "created .env with a new SIMBENCH_TOKEN (read it with: cat $(pwd)/.env)"
fi

if ! cmp -s deploy/sim-bench.service /etc/systemd/system/sim-bench.service; then
  sudo install -m 644 deploy/sim-bench.service /etc/systemd/system/sim-bench.service
  sudo systemctl daemon-reload
  sudo systemctl enable sim-bench
fi
sudo systemctl restart sim-bench
sleep 2
systemctl is-active sim-bench
