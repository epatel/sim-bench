"""Record a real agent session's wire traffic, and replay it without the CLI or a model.

The SDK talks to the Claude Code CLI through a ``Transport``: JSON messages both ways.
``RecordingTransport`` wraps the real (subprocess) transport and writes every message
to a cassette (JSON lines: ``{"dir": "in" | "out", "msg": ...}``; "in" = CLI -> SDK).

``ReplayTransport`` plays the CLI's side back. Everything on our side still runs for
real: the SDK, our workbench tool handlers (the CLI's recorded ``mcp_message`` requests
call them), the read guard hook and ``can_use_tool``. Each answer we write is compared
with the recorded one; differences are collected in ``mismatches``.

Absolute paths are replaced by tokens (``{WORKDIR}``, ``{SOURCE:name}``, ``{HOME}``)
when recording and expanded when replaying, so a cassette works on another machine
and does not leak local paths.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, Transport

# Keys that describe the user's account, machine or personal Claude Code setup (plugins,
# commands, memory), not the conversation. Dropped when recording; replay doesn't need them.
PRIVATE_KEYS = {
    "account", "email", "organization", "organizationName", "subscriptionType",
    "commands", "agents", "models", "plugins", "skills", "slash_commands",
    "terminal_slash_commands", "memory_paths", "messaging_socket_path", "pid",
    "user_output_styles_dir", "available_output_styles",
}  # fmt: skip
# Messages about the user's usage, not the conversation; not recorded.
SKIP_TYPES = {"rate_limit_event"}


@dataclass
class Paths:
    """Token <-> absolute path pairs, applied as plain string replacement."""

    tokens: dict[str, str] = field(default_factory=dict)  # token -> path

    def scrub(self, text: str) -> str:
        for token, path in sorted(self.tokens.items(), key=lambda kv: -len(kv[1])):
            text = text.replace(path, token)
        return text

    def expand(self, text: str) -> str:
        for token, path in self.tokens.items():
            text = text.replace(token, path)
        return text

    def scrub_value(self, value: Any) -> Any:
        """``scrub`` applied to every string inside a JSON-like value."""
        return json.loads(self.scrub(json.dumps(value)))


def drop_private(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: drop_private(v) for k, v in value.items() if k not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [drop_private(v) for v in value]
    return value


class RecordingTransport(Transport):
    """Delegates to ``inner`` and appends every message to ``entries``."""

    def __init__(self, inner: Transport, paths: Paths) -> None:
        self.inner = inner
        self.paths = paths
        self.entries: list[dict[str, Any]] = []

    @classmethod
    def subprocess(cls, options: ClaudeAgentOptions, paths: Paths) -> RecordingTransport:
        # Internal SDK class: the same transport ClaudeSDKClient builds by default.
        from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

        async def no_prompt() -> AsyncIterator[dict[str, Any]]:
            return
            yield {}  # an async generator that never yields

        # ClaudeSDKClient routes permission prompts over stdio when can_use_tool is set,
        # but only applies that to the transport it builds itself.
        if options.can_use_tool:
            options = replace(options, permission_prompt_tool_name="stdio")
        return cls(SubprocessCLITransport(prompt=no_prompt(), options=options), paths)

    def _log(self, direction: str, msg: dict[str, Any]) -> None:
        scrubbed = json.loads(self.paths.scrub(json.dumps(drop_private(msg))))
        self.entries.append({"dir": direction, "msg": scrubbed})

    async def connect(self) -> None:
        await self.inner.connect()

    async def write(self, data: str) -> None:
        for line in data.splitlines():
            if line.strip():
                self._log("out", json.loads(line))
        await self.inner.write(data)

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        async for msg in self.inner.read_messages():
            if msg.get("type") not in SKIP_TYPES:
                self._log("in", msg)
            yield msg

    async def close(self) -> None:
        await self.inner.close()

    def is_ready(self) -> bool:
        return self.inner.is_ready()

    async def end_input(self) -> None:
        await self.inner.end_input()


def out_key(msg: dict[str, Any]) -> tuple:
    """What identifies a message we write, to find its recorded counterpart."""
    kind = msg.get("type")
    if kind == "control_request":
        return kind, msg["request"].get("subtype")
    if kind == "control_response":
        return kind, msg["response"].get("request_id")
    return (kind,)


# Fields that legitimately differ between runs (ids the SDK generates, timings).
VOLATILE = re.compile(r'"(request_id|uuid|session_id|timestamp|duration_ms)": "?[^,"}]*"?')


def comparable(msg: dict[str, Any]) -> str:
    return VOLATILE.sub(r'"\1": "*"', json.dumps(drop_private(msg), sort_keys=True))


class ReplayTransport(Transport):
    """Plays back the CLI side of a recorded session."""

    def __init__(self, entries: list[dict[str, Any]], paths: Paths) -> None:
        self.entries = [json.loads(paths.expand(json.dumps(e))) for e in entries]
        self.consumed = [False] * len(self.entries)
        self.pos = 0
        self.ids: dict[str, str] = {}  # recorded request id -> this run's id
        self.mismatches: list[str] = []
        self._changed = asyncio.Event()
        self._closed = asyncio.Event()

    async def connect(self) -> None:
        pass

    def is_ready(self) -> bool:
        return not self._closed.is_set()

    async def end_input(self) -> None:
        pass

    async def close(self) -> None:
        self._closed.set()
        self._changed.set()

    async def write(self, data: str) -> None:
        for line in data.splitlines():
            if line.strip():
                self._accept(json.loads(line))
        self._changed.set()

    def _accept(self, msg: dict[str, Any]) -> None:
        key = out_key(msg)
        i = self._find_out(key)
        if i is None:
            self.mismatches.append(f"unexpected write {key}: {json.dumps(msg)[:300]}")
            return
        self.consumed[i] = True
        recorded = self.entries[i]["msg"]
        if msg.get("type") == "control_request":
            self.ids[recorded["request_id"]] = msg["request_id"]
        elif comparable(msg) != comparable(recorded):
            self.mismatches.append(
                f"{key} differs:\n  recorded {comparable(recorded)[:600]}\n"
                f"  replayed {comparable(msg)[:600]}"
            )

    def _find_out(self, key: tuple) -> int | None:
        for i in range(self.pos, len(self.entries)):
            if self._pending_out(i) and out_key(self.entries[i]["msg"]) == key:
                return i
        return None

    def _pending_out(self, i: int) -> bool:
        return self.entries[i]["dir"] == "out" and not self.consumed[i]

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while not self._closed.is_set():
            if self.pos >= len(self.entries):
                await self._closed.wait()  # like a CLI waiting for the next prompt
                return
            entry = self.entries[self.pos]
            if self._pending_out(self.pos):
                self._changed.clear()
                await self._changed.wait()  # wait for the SDK to write it
                continue
            self.pos += 1
            if entry["dir"] == "in":
                yield self._rewrite_ids(entry["msg"])

    def _rewrite_ids(self, msg: dict[str, Any]) -> dict[str, Any]:
        if msg.get("type") == "control_response":
            rid = msg["response"].get("request_id")
            if rid in self.ids:
                msg = {**msg, "response": {**msg["response"], "request_id": self.ids[rid]}}
        return msg

    def leftover(self) -> list[tuple]:
        """Recorded writes we never made (a shorter conversation than recorded)."""
        return [out_key(e["msg"]) for e, used in zip(self.entries, self.consumed, strict=True)
                if e["dir"] == "out" and not used]  # fmt: skip


def save_cassette(path: Path, meta: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    lines = [json.dumps({"meta": meta})] + [json.dumps(e) for e in entries]
    path.write_text("\n".join(lines) + "\n")


def load_cassette(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return lines[0]["meta"], lines[1:]
