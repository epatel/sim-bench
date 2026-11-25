# architecture

How the workbench is split into parts, and where each part lives. Read this before adding a new top-level module.

## Parts

```mermaid
flowchart LR
  Web["web/<br/>canvas, panels"] <-- REST + WebSocket --> API["server/app.py<br/>FastAPI"]
  API --> Kernel["server/kernel/<br/>discrete-event engine"]
  API --> Store["model store (planned M3)<br/>projects/, libraries/"]
  API --> Agent["server/agent/ (planned M7)<br/>claude-agent-sdk"]
  Kernel --> DB[("SQLite run results (planned M6)")]
```

| Path | Status | Owns |
|---|---|---|
| `server/kernel/` | built (M1) | Clock, event queue, RNG streams, tier-1 behaviors, links, metrics, snapshot/fork, headless CLI. |
| `projects/examples/` | built (M1) | Example models in YAML (`mm1.yaml`, `web-backend.yaml`). |
| `server/app.py`, `web/` | built (M2) | FastAPI REST for models and types, a WebSocket that streams live runs, and the vanilla-JS canvas editor. Font Awesome is vendored. |
| `libraries/` | planned (M3) | Item types and templates as YAML. |
| `server/agent/` | planned (M7) | Claude Agent SDK session and in-process MCP tools that call the kernel and the model store. |

## Boundaries

- The kernel knows nothing about HTTP, files on disk (except `load_model`), or the agent. Everything above it talks to it through `ModelSpec`, `Simulation` and listener records.
- Models are data (pydantic `ModelSpec`, saved as YAML). Runtime state lives only in `Simulation`. The canvas and the agent edit models and never touch runtime objects directly.
- Listener records (`{"type": "send" | "deliver" | "drop" | "lost" | "unrouted", ...}`) are the contract for live visualization. Adding a field is fine. Renaming one breaks the UI.

## Tech choices

- Python 3.12 in `.venv`, pydantic v2, PyYAML, FastAPI and uvicorn. pytest, ruff and httpx for development.
- No build step for the frontend (decided in `project-plan.md`).
