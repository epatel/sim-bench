"""Visuals: small scripts that turn a live run into things to show on the Visual canvas.

A model's ``visuals`` each have a widget (``label``, ``bar`` or ``sparkline``), an
optional ``entity`` they look at, and a script of ``name = <expr>`` lines that must set
``output``:

    busy = queue + busy               # metric names give their current number
    output = busy
    title = "db load"                 # optional: title, unit, min, max
    max = 20

The script is parsed into expressions and interpreted by ``expr`` (no ``eval``), like
machine scripts. It runs on the server with each run summary. The browser never runs it,
so a model from the agent or a shared file can't run code in your page.

Names a script can use (later ones win):
- the model's global constants, and its functions next to the pure built-ins and ``str``
- ``now``, ``entities`` (every entity's summary, e.g. ``entities.db.queue.avg``)
- for a visual with an entity: ``id``, ``m`` (its full summary, e.g. ``m.residence.p95``),
  its machine variables, and each metric by name as its current number (a counter's
  value, a gauge's value, a sample set's mean)
- the names assigned on earlier lines
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from .expr import ExprError, evaluate
from .machine_script import NAME, ScriptError, blocks, check_expr
from .model_globals import PURE, Globals

WIDGETS = ("label", "bar", "sparkline")
SENT = ("output", "title", "unit", "min", "max")  # what the browser gets of each visual
FUNCS = {**PURE, "str": str}
_ASSIGN = re.compile(rf"^({NAME})\s*=(?!=)\s*(.+)$")


@dataclass(frozen=True)
class VisualScript:
    steps: tuple[tuple[str, str], ...]  # (name, expression), in order


@lru_cache(maxsize=256)
def parse_visual(code: str) -> VisualScript:
    """Parse a visual's script (``ScriptError`` names the line)."""
    steps = []
    for line in blocks(code):
        m = _ASSIGN.match(line.text)
        if not m or line.body:
            raise line.fail("expected 'name = <expr>'")
        steps.append((m.group(1), check_expr(line, m.group(2))))
    if "output" not in {name for name, _ in steps}:
        raise ScriptError("set output, e.g. 'output = queue'")
    return VisualScript(tuple(steps))


def current(metric: Any) -> Any:
    """A metric's current number; anything that isn't a metric is returned as is."""
    if not isinstance(metric, dict) or "kind" not in metric:
        return metric
    if metric["kind"] == "samples":
        return metric.get("mean")
    return metric.get("value")


def environment(entity: str | None, summary: dict[str, Any], g: Globals) -> dict[str, Any]:
    entities = summary.get("entities", {})
    env: dict[str, Any] = {**g.consts, "now": summary.get("time", 0.0), "entities": entities}
    if entity is None:
        return env
    if entity not in entities:
        raise ExprError(f"{entity} has no data (removed, or inside an abstract group)")
    data = entities[entity]
    env.update(data.get("vars") or {})
    env.update({k: current(v) for k, v in data.items() if isinstance(v, dict) and "kind" in v})
    env.update(id=entity, m=data)
    return env


def run_visual(code: str, entity: str | None, summary: dict[str, Any], g: Globals) -> dict:
    """``{output, title, ...}`` from one visual, or ``{"error": message}``."""
    try:
        env = environment(entity, summary, g)
        funcs = g.bind(FUNCS)
        for name, expr in parse_visual(code).steps:
            env[name] = evaluate(expr, env, funcs)
    except (ExprError, ScriptError, TypeError, ValueError, ZeroDivisionError) as e:
        return {"error": str(e)}
    return {k: _plain(env[k]) for k in SENT if k in env}


def _plain(v: Any) -> Any:
    """Keep what JSON can carry; show anything else as text."""
    return v if isinstance(v, (int, float, str, bool, type(None))) else str(v)


def run_visuals(visuals: list, summary: dict[str, Any], g: Globals) -> dict[str, dict]:
    return {v.id: run_visual(v.code, v.entity, summary, g) for v in visuals}
