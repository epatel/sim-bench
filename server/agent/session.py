"""One agent conversation: a Claude Agent SDK client wired to the workbench tools.

The SDK runs its bundled Claude Code CLI as a subprocess. It authenticates with
whatever login that CLI has; ``strip_api_keys`` removes API-key variables from
this process first so a stray key can never switch the agent to API billing.

The agent is isolated from the developer setup of this repo: no filesystem
settings (CLAUDE.md, hooks, permissions), no skills, and no built-in tools
(Bash, file edits, web). It can only use the workbench tools.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from .tools import SERVER_NAME, Env, build_server, tool_names

log = logging.getLogger("simbench.agent")

KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
MAX_TURNS = 40

SYSTEM_PROMPT = """\
You are the modeling assistant inside Simulation Workbench, a discrete-event simulator.
The user sees the model on a canvas next to this chat; every change you make with the
workbench tools appears there immediately.

Concepts:
- Entities have an id, an item type and params. Item types come from built-ins (source,
  server, delay, router, sink, machine) and libraries (e.g. web/app-server). Call
  list_types before building, and prefer library types and templates.
- Connections go from an output port to an input port: from "a.out" to "b.in". An output
  has one link; inputs take many. Links can have latency, loss, bandwidth.
- Distributions are a number or e.g. {dist: exp, mean: 0.1}, {dist: lognormal, mean, std},
  {dist: uniform, low, high}, {dist: normal, mean, std}.
- Groups are composites with boundary ports, expanded or abstract (one calibrated stand-in).
- Scenarios are what-if variants (patches on params, links or group modes, optionally at a
  time). run_batch compares them over seeds with confidence intervals.
- Custom logic: define a type that extends "machine" (a state machine, as data). You cannot
  write Python; if something truly needs Python, say what and why, and let the user do it.

How to work:
- Read the model (get_model) before changing it. Keep ids short and snake_case.
- After structural changes, run a short simulation to check messages flow.
- Base claims on tool results, with numbers. Say how uncertain a comparison is (CIs).
- Be brief in chat: what you changed, what you found, what you suggest next.
"""


def strip_api_keys() -> list[str]:
    """Remove API-key variables from this process; return the names removed."""
    removed = [k for k in KEY_VARS if os.environ.pop(k, None) is not None]
    if removed:
        log.warning("agent: removed %s from the environment to use the subscription login", removed)
    return removed


Send = Callable[[dict[str, Any]], Awaitable[None]]


class AgentSession:
    def __init__(self, env: Env, send: Send) -> None:
        self.env = env
        self.send = send
        self.client: ClaudeSDKClient | None = None
        self.allowed = set(tool_names())

    def options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            system_prompt=SYSTEM_PROMPT,
            tools=[],  # no built-in tools at all
            mcp_servers={SERVER_NAME: build_server(self.env)},
            allowed_tools=sorted(self.allowed),
            can_use_tool=self.can_use_tool,
            setting_sources=[],  # ignore CLAUDE.md, hooks and permissions on disk
            skills=[],
            max_turns=MAX_TURNS,
            model=os.environ.get("SIMBENCH_AGENT_MODEL") or None,
            stderr=lambda line: log.debug("cli: %s", line),
        )

    async def can_use_tool(self, name: str, tool_input: dict[str, Any], context: Any):
        if name in self.allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(message=f"{name} is not available in the workbench")

    async def ask(self, text: str, model: dict[str, Any]) -> None:
        """Send one user prompt (with the browser's current model) and stream the reply."""
        self.env.workspace.replace(model)
        if self.client is None:
            self.client = ClaudeSDKClient(self.options())
            await self.client.connect()
        await self.client.query(text)
        async for message in self.client.receive_response():
            for event in events_for(message):
                await self.send(event)

    async def interrupt(self) -> None:
        if self.client is not None:
            await self.client.interrupt()

    async def close(self) -> None:
        if self.client is not None:
            client, self.client = self.client, None
            await client.disconnect()


def events_for(message: Any) -> list[dict[str, Any]]:
    """Translate an SDK message into chat events for the browser."""
    if isinstance(message, AssistantMessage):
        return [e for b in message.content if (e := _assistant_block(b))]
    if isinstance(message, UserMessage) and isinstance(message.content, list):
        return [_tool_result(b) for b in message.content if isinstance(b, ToolResultBlock)]
    if isinstance(message, ResultMessage):
        return [
            {
                "type": "done",
                "error": message.is_error,
                "subtype": message.subtype,
                "turns": message.num_turns,
                "duration_ms": message.duration_ms,
                "cost_usd": message.total_cost_usd,
            }
        ]
    return []


def _assistant_block(block: Any) -> dict[str, Any] | None:
    if isinstance(block, TextBlock) and block.text.strip():
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        name = block.name.removeprefix(f"mcp__{SERVER_NAME}__")
        return {"type": "tool", "id": block.id, "name": name, "input": block.input}
    return None


def _tool_result(block: ToolResultBlock) -> dict[str, Any]:
    content = block.content
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return {"type": "tool_result", "id": block.tool_use_id, "error": bool(block.is_error),
            "text": (content or "")[:600]}  # fmt: skip
