# Simulation Workbench (sim-bench)

## About this project

A local workbench that runs a discrete-event simulation of entities, messages and events in a Python server, edited and watched through a browser canvas. Models can be refined from high-level to low-level where needed, and branched for "what if" scenarios. A Claude agent (Claude Agent SDK on the subscription login) will drive the model and derive behavior from other projects' source code. Milestones M1 (kernel) and M2 (server and canvas) are done. Libraries, resolution, scenarios and the agent are planned.

@project-plan.md

## Commands

- Setup: `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`
- Run: `.venv/bin/python -m server --reload` (then open http://127.0.0.1:8765, or pass `--port`)
- Headless run: `.venv/bin/python -m server.kernel projects/examples/web-backend.yaml --until 300 --seed 1`
- Test: `.venv/bin/pytest -q`
- Lint: `.venv/bin/ruff check . && .venv/bin/ruff format --check .`

## Cards

Architecture
- [architecture](cards/architecture.md) — before adding a top-level module or deciding where new code goes

Domains
- [sim-kernel](cards/sim-kernel.md) — when changing scheduling, behaviors, links, metrics, snapshots or the kernel CLI
- [server-and-canvas](cards/server-and-canvas.md) — when changing the REST/WebSocket protocol, the canvas editor, the inspector or live-run streaming

Decisions
- [standing-defaults](cards/standing-defaults.md) — when adding a Python dependency or the first SQLite table (venv, migrations)

When a commit adds non-obvious logic or settles an architecture choice, update or add the card in the same commit.

## Enforced by hooks (.claude/settings.json)

- Python files are auto-formatted with ruff after each edit. Don't hand-format them.
- The test suite runs when you finish. A failing test sends you back once. Fix it, or explain why it is expected to fail.
- Force pushes are blocked. Push normally, or ask the user.
- Edits under `.venv/` are blocked. Change `requirements*.txt` instead.
