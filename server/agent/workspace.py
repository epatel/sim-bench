"""The model the agent edits: a validated, undoable copy of the browser's model.

The browser owns the model. It sends it along with each prompt; tools change
this copy; every change is validated (and reverted if invalid) and then pushed
back to the browser, which redraws.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from ..kernel import ModelError

Validate = Callable[[dict[str, Any]], Any]  # raises ModelError for a bad model

MAX_UNDO = 50


class ToolError(Exception):
    """A tool call that failed; the message goes back to the agent."""


class Workspace:
    def __init__(self, model: dict[str, Any], validate: Validate) -> None:
        self.model = _normalized(model)
        self.validate = validate
        self.history: list[dict[str, Any]] = []
        self.listeners: list[Callable[[dict[str, Any]], Any]] = []

    def replace(self, model: dict[str, Any]) -> None:
        """The browser's current model (sent with each prompt). Not undoable."""
        self.model = _normalized(model)

    def change(self, fn: Callable[[dict[str, Any]], Any]) -> Any:
        """Apply ``fn`` to a copy; keep it only if the result validates."""
        draft = copy.deepcopy(self.model)
        result = fn(draft)
        try:
            self.validate(draft)
        except ModelError as e:
            raise ToolError(f"change rejected, the model would be invalid: {e}") from e
        self.history.append(self.model)
        del self.history[:-MAX_UNDO]
        self.model = draft
        return result

    def undo(self) -> None:
        if not self.history:
            raise ToolError("nothing to undo")
        self.model = self.history.pop()

    # --- lookups ------------------------------------------------------------------

    def entity(self, model: dict[str, Any], entity_id: str) -> dict[str, Any]:
        for e in model["entities"]:
            if e["id"] == entity_id:
                return e
        raise ToolError(f"no entity {entity_id!r}")

    def connection(self, model: dict[str, Any], key: str) -> dict[str, Any]:
        for c in model["connections"]:
            if link_key(c) == key:
                return c
        raise ToolError(f"no connection {key!r} (keys look like 'a.out->b.in')")

    def group(self, model: dict[str, Any], group_id: str) -> dict[str, Any]:
        for g in model["groups"]:
            if g["id"] == group_id:
                return g
        raise ToolError(f"no group {group_id!r}")


def link_key(c: dict[str, Any]) -> str:
    return c.get("id") or f"{c['from']}->{c['to']}"


def _normalized(model: dict[str, Any]) -> dict[str, Any]:
    m = copy.deepcopy(model or {})
    m.setdefault("name", "untitled")
    for key in ("entities", "connections", "groups", "scenarios"):
        m[key] = list(m.get(key) or [])
    for e in m["entities"]:
        e.setdefault("params", {})
    return m
