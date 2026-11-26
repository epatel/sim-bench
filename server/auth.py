"""An optional access token, for running the workbench on a public host.

Set ``SIMBENCH_TOKEN`` in the environment or in ``.env`` at the repo root. Without it,
nothing changes: the workbench is open, as it is on localhost. With it, every ``/api``
and ``/ws`` request must carry the token, and ``/`` serves a landing page that asks for
it (or takes it from ``?token=``) and keeps it in the browser's localStorage.

The browser sends the token as ``Authorization: Bearer <token>`` on fetches. A browser
can't set headers on a WebSocket, so there it offers the subprotocols
``["sim-bench", "token.<token>"]`` and the server picks ``sim-bench``. That keeps the
token out of URLs and so out of proxy access logs.

The static files (HTML, JS, CSS) stay public: the repo is public anyway.
"""

from __future__ import annotations

import hmac
import json
import os
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any

Scope = MutableMapping[str, Any]
ASGI = Callable[[Scope, Callable, Callable], Awaitable[None]]

ENV_KEY = "SIMBENCH_TOKEN"
SUBPROTOCOL = "sim-bench"
TOKEN_PREFIX = "token."
PROTECTED = ("/api/", "/ws/")


def read_env_file(path: Path) -> dict[str, str]:
    """``KEY=value`` lines; ``#`` comments, blank lines and optional quotes allowed."""
    if not path.is_file():
        return {}
    out = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not _is_setting(line):
            continue
        key, value = line.split("=", 1)
        out[key.removeprefix("export ").strip()] = value.strip().strip("\"'")
    return out


def _is_setting(line: str) -> bool:
    return "=" in line and not line.startswith("#")


def load_token(env_file: Path) -> str | None:
    return os.environ.get(ENV_KEY) or read_env_file(env_file).get(ENV_KEY) or None


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return ""


def offered_token(scope: Scope) -> str:
    """The token a request carries: a bearer header, or a WebSocket subprotocol."""
    auth = _header(scope, b"authorization")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    for proto in _header(scope, b"sec-websocket-protocol").split(","):
        proto = proto.strip()
        if proto.startswith(TOKEN_PREFIX):
            return proto[len(TOKEN_PREFIX) :]
    return ""


def wants_subprotocol(scope: Scope) -> bool:
    """Whether a WebSocket offered ours, so ``accept`` must select it."""
    offered = _header(scope, b"sec-websocket-protocol").split(",")
    return SUBPROTOCOL in (p.strip() for p in offered)


class TokenGate:
    """ASGI middleware: refuse ``/api`` and ``/ws`` requests without the token."""

    def __init__(self, app: ASGI, token: str | None) -> None:
        self.app = app
        self.token = token

    def allows(self, scope: Scope) -> bool:
        if not self.token or not scope.get("path", "").startswith(PROTECTED):
            return True
        return hmac.compare_digest(offered_token(scope).encode(), self.token.encode())

    async def __call__(self, scope: Scope, receive: Callable, send: Callable) -> None:
        if scope["type"] not in ("http", "websocket") or self.allows(scope):
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
        else:
            body = json.dumps({"detail": "missing or wrong access token"}).encode()
            headers = [(b"content-type", b"application/json")]
            await send({"type": "http.response.start", "status": 401, "headers": headers})
            await send({"type": "http.response.body", "body": body})
