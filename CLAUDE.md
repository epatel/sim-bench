# Simulation Workbench (sim-bench)

## About this project

A local workbench that runs a discrete-event simulation of entities, messages and events in a Python server, edited and watched through a browser canvas. Models can be refined from high-level to low-level where needed, and branched for "what if" scenarios. A Claude agent (Claude Agent SDK on the subscription login) will drive the model and derive behavior from other projects' source code. Milestones M1 (kernel), M2 (server and canvas), M3 (libraries and templates), M4 (composites and calibration), M5 (state machines and Python behaviors) and M6 (scenarios, batches and what-if) are done. The agent (M7, M8) is planned.

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
- [libraries](cards/libraries.md) — when adding or editing item types, templates or a library, or changing type resolution
- [composites](cards/composites.md) — when changing groups, abstract/expanded modes, `flatten`, boundary probes or calibration
- [behaviors](cards/behaviors.md) — when writing an entity with custom logic (state machine or Python behavior), or changing the expression language
- [scenarios](cards/scenarios.md) — when changing scenario patches, batch runs and statistics, the what-if fork, the results store or the results drawer

Decisions
- [standing-defaults](cards/standing-defaults.md) — when adding a Python dependency or the first SQLite table (venv, migrations)

When a commit adds non-obvious logic or settles an architecture choice, update or add the card in the same commit.

## Enforced by hooks (.claude/settings.json)

- Python files are auto-formatted with ruff after each edit. Don't hand-format them.
- The test suite runs when you finish. A failing test sends you back once. Fix it, or explain why it is expected to fail.
- Force pushes are blocked. Push normally, or ask the user.
- Edits under `.venv/` are blocked. Change `requirements*.txt` instead.
- Edits to Python behaviors (`libraries/**/behaviors/*.py`) need the user's approval, because they run as code in the server. Prefer a state machine (`extends: machine`) when one can express the behavior.
- After each edit, aihealth reports any new or worsened code smells in that file. Before you stop, it checks all uncommitted changes and gives you one more turn if health got worse. Fix the smells or say why they are intended.

## Code health

- Before committing, run `aihealth delta` and fix new or worsened smells it reports in code you touched (exit code 1 = health degraded).
- `aihealth scan .` gives an overview of the codebase.
- aihealth is a global install (on PATH), pinned to v0.4.0: `uv tool install "git+https://github.com/epatel/ai-health@033d1867fe514867d944d9387ee72db0188eed47"  # v0.4.0`. The hooks and the `.mcp.json` server do nothing until it is installed.
