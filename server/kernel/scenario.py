"""What-if scenarios and batch runs (milestone M6).

A scenario is the base model plus patches (see ``model.PatchSpec``). Patches
without ``at`` change the model before the run; patches with ``at`` happen during
it. ``BASELINE`` is the model as written.

``run_one`` runs one (scenario, seed) and samples metrics over time;
``aggregate`` turns several seeds into a mean and a 95% confidence interval per
metric; ``whatif`` forks a running simulation into a baseline and a scenario
branch that continue from the same state with the same random numbers.
"""

from __future__ import annotations

import math
import time
from typing import Any

from .library import Library
from .model import ModelSpec, PatchSpec, ScenarioSpec
from .sim import ModelError, Simulation

BASELINE = "baseline"


def scenario_of(model: ModelSpec, scenario_id: str | None) -> ScenarioSpec | None:
    if scenario_id in (None, "", BASELINE):
        return None
    try:
        return model.scenario(scenario_id)
    except KeyError:
        raise ModelError(f"no scenario {scenario_id!r}") from None


def apply_static(model: ModelSpec, scenario: ScenarioSpec | None) -> ModelSpec:
    """A copy of ``model`` with the scenario's before-the-run patches applied."""
    out = model.model_copy(deep=True)
    for p in scenario.patches if scenario else []:
        if p.at is None:
            _apply_to_model(out, p)
    return out


def _apply_to_model(model: ModelSpec, p: PatchSpec) -> None:
    if p.entity:
        _find(model.entities, p.entity, "entity").params[p.param] = p.value
    elif p.link:
        c = _find(model.connections, p.link, "link")
        index = model.connections.index(c)
        model.connections[index] = c.model_copy(update={"id": c.key(), p.field: p.value})
    else:
        _find(model.groups, p.group, "group").mode = p.mode


def _find(items: list, key: str, what: str):
    """The entity/group with this id, or the connection with this key."""
    for item in items:
        if (item.key() if what == "link" else item.id) == key:
            return item
    raise ModelError(f"patch: no {what} {key!r}")


def build(
    model: ModelSpec, scenario_id: str | None, seed: int, library: Library | None = None
) -> Simulation:
    """A ready-to-run Simulation for one scenario, timed patches scheduled."""
    scenario = scenario_of(model, scenario_id)
    sim = Simulation(apply_static(model, scenario), seed=seed, library=library)
    for p in scenario.patches if scenario else []:
        if p.at is not None:
            sim.schedule_patch(p.at, p)
    return sim


# --- metrics as flat numbers --------------------------------------------------------

_FIELDS = ("value", "mean", "avg", "p95", "p99", "max", "count", "peak")


def flat_metrics(summary: dict[str, Any]) -> dict[str, float]:
    """Every number in a run summary, keyed by a dotted path.

    e.g. ``entities.done.latency.mean``, ``links.a.out->b.in.lost.value``,
    ``groups.pool.residence.p95``.
    """
    out: dict[str, float] = {}
    for section in ("entities", "links", "groups"):
        for owner, metrics in summary.get(section, {}).items():
            for name, m in metrics.items():
                _flatten_one(out, f"{section}.{owner}.{name}", m)
    return out


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _flatten_one(out: dict[str, float], path: str, m: Any) -> None:
    if _number(m):
        out[path] = float(m)
    elif isinstance(m, dict):
        out.update({f"{path}.{k}": float(m[k]) for k in _FIELDS if _number(m.get(k))})
        if "residence" in m:  # a group probe nests a samples metric
            _flatten_one(out, f"{path}.residence", m["residence"])


# --- running ------------------------------------------------------------------------------


def run_one(
    sim: Simulation, until: float, samples: int = 40, max_events: int | None = None
) -> dict[str, Any]:
    """Run ``sim`` to ``until``, sampling every metric ``samples`` times."""
    t0 = time.perf_counter()
    start = sim.now
    series: list[tuple[float, dict[str, float]]] = []
    budget = max_events
    for i in range(1, samples + 1):
        n = sim.run(until=start + (until - start) * i / samples, max_events=budget)
        series.append((sim.now, flat_metrics(sim.summary())))
        if budget is not None:
            budget -= n
            if budget <= 0:
                break
    summary = sim.summary()
    return {
        "summary": summary,
        "flat": flat_metrics(summary),
        "series": series,
        "events": sim.events_processed,
        "truncated": sim.now < until,
        "wall_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


# 0.975 quantiles of Student's t for 1..30 degrees of freedom (then ~normal)
_T975 = [
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
]  # fmt: skip


def stats(values: list[float]) -> dict[str, float]:
    """Mean, std and the half-width of a 95% confidence interval for the mean."""
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return {"n": n, "mean": mean, "std": 0.0, "ci95": 0.0}
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
    t = _T975[n - 2] if n - 1 <= len(_T975) else 1.96
    return {"n": n, "mean": mean, "std": std, "ci95": t * std / math.sqrt(n)}


def aggregate(flats: list[dict[str, float]]) -> dict[str, dict[str, float]]:
    """Per metric path, stats over runs (paths missing in some runs use the rest)."""
    paths = sorted({p for f in flats for p in f})
    return {p: stats([f[p] for f in flats if p in f]) for p in paths}


def mean_series(runs: list[list[tuple[float, dict[str, float]]]], path: str) -> list[list[float]]:
    """[[t, mean over runs], ...] for one metric, aligned by sample index."""
    out = []
    for i in range(min(len(r) for r in runs) if runs else 0):
        values = [r[i][1][path] for r in runs if path in r[i][1]]
        if values:
            out.append([runs[0][i][0], sum(values) / len(values)])
    return out


# --- what-if from a running simulation ---------------------------------------------------


def whatif(
    sim: Simulation, scenario: ScenarioSpec, horizon: float, samples: int = 40
) -> dict[str, Any]:
    """Fork ``sim`` twice and run both ``horizon`` further: as is, and with the scenario.

    Both forks continue from the same state with the same random streams, so the
    difference comes from the scenario alone. Patches without ``at`` apply at the
    fork; patches with ``at`` apply that long after it. Group patches can't apply
    to a running simulation.
    """
    start = sim.now
    base, alt = sim.fork(), sim.fork()
    for p in scenario.patches:
        if p.at is None:
            alt.apply_patch(p)
        else:
            alt.schedule_patch(start + p.at, p)
    until = start + horizon
    return {
        "from": start,
        "horizon": horizon,
        BASELINE: run_one(base, until, samples),
        scenario.id: run_one(alt, until, samples),
    }
