# agent

The in-app modeling agent (`server/agent/`, `/ws/agent`, `web/js/agent.js`): a Claude Agent SDK session that edits the user's model through workbench tools. Read this before changing its tools, prompt, isolation, authentication, or the chat panel.

## Flow

```mermaid
sequenceDiagram
  participant UI as Chat panel
  participant WS as /ws/agent (AgentChat)
  participant S as AgentSession
  participant CLI as Claude Code CLI (bundled with the SDK)
  participant T as workbench tools
  UI->>WS: prompt {text, model}
  WS->>S: ask(text, model)  (model -> Workspace)
  S->>CLI: query
  CLI->>T: tool calls (in-process MCP server "workbench")
  T-->>WS: {"type": "model"} after each change
  WS-->>UI: model (canvas redraws), batch (results drawer opens)
  CLI-->>S: assistant text, tool results, result
  S-->>UI: text, tool, tool_result, done
```

- The browser owns the model and sends it with every prompt. `Workspace` keeps a copy that is validated on every change (an invalid result is reverted and reported to the agent as a tool error) and can be undone. After each change, the server pushes the model back, and `applyAgentModel` adopts it, lays out new entities, and marks any live run as stale.
- Each prompt runs as a task, so `interrupt` gets through. `reset` starts a new SDK session.

## Tools (`tools.py`)

get_model, list_types, add_entity, update_entity, remove_entity, connect, update_link, disconnect, replace_model, undo, insert_template, set_group, calibrate_group, define_type, set_scenario, run, run_batch, list_sources, propose_changes. Reading other projects (Read/Grep/Glob, proposals) is covered in the sources card.

- Tool results are short text: `brief()` gives the key numbers per entity, and `compare()` gives mean ± CI per scenario. Simulations run in a worker thread.
- `calibrate_group` takes `kind` (auto, server, delay). Its result text reports the capacity and the bottleneck when it fits a server stand-in.
- `define_type` writes **YAML only**, to `libraries/<lib>/types/<name>.yaml`. Names must match `[a-z][a-z0-9-]*`. The library is reloaded after writing, and the file is restored if the new type breaks it. The agent has no way to write Python behaviors, and the system prompt tells it to ask the user instead.
- Adding a tool: write a `make_<name>(env)` factory (use `guarded(...)`, and set `changes=True` if it edits the model), then add it to both `TOOL_FACTORIES` and `TOOL_NAMES`. `test_tool_names_match_factories` checks the two lists agree.

## Isolation and authentication (`session.py`)

- `tools=[]` turns off every built-in tool (Bash, file edits, web). With registered sources it is `["Read", "Grep", "Glob"]`, guarded by a hook (see the sources card). `allowed_tools` lists only the workbench tools, and `can_use_tool` denies anything else.
- `setting_sources=[]` and `skills=[]` keep the agent from loading this repo's CLAUDE.md, hooks, permissions or skills, which are for developing the workbench, not for using it.
- At import time, `strip_api_keys()` removes `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from the server's environment. The SDK passes the server's environment on to the CLI, and its `env` option can add variables but not remove them, so this is the only way to make sure the CLI uses its own login (the user's subscription). `/api/agent/status` reports which keys were removed.
- The model is the login's default. Set `SIMBENCH_AGENT_MODEL` to override it. A prompt has at most 40 turns.
- The SDK includes its own CLI (about 370 MB in `.venv`). If the CLI is not logged in, the error appears in the chat. Run `claude` once and log in.

## Testing

- `tests/test_agent.py` calls the tool handlers directly and checks the session options, without calling a model.
- **Recorded conversations** (`server/agent/replay.py`, `record.py`, `tests/test_agent_replay.py`). The SDK talks to the CLI through a `Transport` (JSON messages both ways), and `AgentSession(transport=...)` can swap it out.
  - `python -m server.agent.record tests/cassettes/NAME.jsonl --prompt ... [--model ...] [--source name=dir]` runs a real conversation (your login, real model) in a sandbox (temp libraries, run store, sources). It writes every message to a cassette, plus the browser events and the final model.
  - Replay plays back only the CLI's side. The SDK, the tool handlers, the read guard hook and `can_use_tool` all run for real, and every answer must match the recording. The browser events and final model must match too. No CLI, login or network is needed, so it runs in CI.
  - `tests/fixtures/shop` is a tiny project with a fake `.env`. The `shop-from-source` cassette records the guard denying `.env`, and a test replays it with the guard disabled to prove the replay catches it.
- Cassettes are scrubbed. Absolute paths become tokens (`{WORKDIR}`, `{SOURCE:name}`, `{HOME}`, plus `_REAL` variants for macOS `/private`), and the account and personal setup keys (`PRIVATE_KEYS`: plugins, commands, memory paths, and so on) and `rate_limit_event` are dropped. Check a new cassette before committing it.
- **When a cassette goes stale.** A change to tool behavior, tool schemas, the system prompt's effect on our answers, or the SDK's wire protocol makes a replay report mismatches. Re-record with the prompts in the cassette's `meta`. The model's choices will differ, so check the new conversation still exercises what the old one did. Recording uses the SDK's internal `SubprocessCLITransport`, so check `RecordingTransport.subprocess` after SDK upgrades.
