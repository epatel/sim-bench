"""Calibration: measure an expanded group at its boundary and fit an abstract stand-in.

Two stand-ins:

- ``delay``: lognormal residence plus loss, with unlimited concurrency. It reproduces
  latency at the measured load but never saturates.
- ``server``: c parallel servers with an exponential service time, sized so that
  (1) its capacity c/S equals the group's measured capacity and (2) its M/M/c mean
  residence at the measured arrival rate equals the measured mean residence. It
  saturates where the group does.

The group's capacity comes from one run: for each member that reports utilization
(a ``server``), capacity = served / (utilization * span), the jobs per time it handles
when fully busy. Divided by the member's share of the group's arrivals, that is the
group arrival rate at which the member saturates. The smallest one is the bottleneck.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .composite import CompositeError
from .model import ModelSpec
from .sim import Simulation

MAX_SERVERS = 512
KINDS = ("auto", "server", "delay")


@dataclass(frozen=True)
class Request:
    group: str
    until: float = 1000.0
    seed: int = 0
    kind: str = "auto"  # auto | server | delay


@dataclass
class Measurement:
    probe: dict  # GroupProbe summary
    now: float
    seed: int
    members: dict = field(default_factory=dict)  # member id -> entity summary


def run_calibration(
    model: ModelSpec, req: Request, library: Any, max_events: int
) -> tuple[Simulation, Measurement, dict]:
    """Run ``model`` with ``req.group`` expanded; return (sim, measurement, stand-in)."""
    sim = Simulation(expanded(model, req.group), seed=req.seed, library=library)
    sim.run(until=req.until, max_events=max_events)
    summary = sim.summary()
    members = {e.id: summary["entities"][e.id] for e in model.entities if e.group == req.group}
    m = Measurement(summary["groups"][req.group], sim.now, req.seed, members)
    return sim, m, suggest_abstract(m, req.kind)


def expanded(model: ModelSpec, gid: str) -> ModelSpec:
    """``model`` with group ``gid`` (which must have a boundary) forced to expanded."""
    group = next((g for g in model.groups if g.id == gid), None)
    if group is None:
        raise CompositeError(f"no group {gid!r}")
    if not group.inputs or not group.outputs:
        raise CompositeError(f"group {gid!r} needs boundary inputs and outputs to calibrate")
    groups = [g.model_copy(update={"mode": "expanded"}) if g.id == gid else g for g in model.groups]
    return model.model_copy(update={"groups": groups})


def suggest_abstract(m: Measurement, kind: str = "auto") -> dict:
    """A stand-in for the measured group. "auto" picks ``server`` when a capacity was found."""
    if kind not in KINDS:
        raise CompositeError(f"stand-in kind must be one of {KINDS}")
    r = m.probe["residence"]
    if not r.get("count"):
        raise CompositeError("no message crossed the group boundary; run longer or check wiring")
    bottleneck = find_bottleneck(m)
    if kind == "server" and bottleneck is None:
        raise CompositeError("no member reports utilization, so the capacity is unknown")
    calibration = {
        "measured_until": m.now,
        "seed": m.seed,
        "samples": r["count"],
        "arrival_rate": m.probe["arrival_rate"],
        "throughput": m.probe["throughput"],
        "residence_mean": r["mean"],
        "residence_p95": r.get("p95"),
        "loss": m.probe["loss"],
    }
    if kind == "delay" or bottleneck is None:
        params = _delay_params(r)
        calibration["note"] = "valid near the measured arrival rate; no capacity limit"
        stand_in = {"type": "delay", "params": params}
    else:
        params, fit = _server_params(m.probe, bottleneck)
        calibration.update(fit)
        stand_in = {"type": "server", "params": params}
    if m.probe["loss"] > 0:
        params["error_rate"] = m.probe["loss"]
    return {**stand_in, "calibration": calibration}


def find_bottleneck(m: Measurement) -> dict | None:
    """The member that saturates first as the group's arrival rate grows."""
    found = [b for mid, s in m.members.items() if (b := _saturation(mid, s, m))]
    return min(found, key=lambda b: b["capacity"], default=None)


def _saturation(mid: str, s: dict, m: Measurement) -> dict | None:
    """The group arrival rate at which member ``mid`` is fully busy, or None if unknown."""
    util = s.get("utilization") or 0.0
    arrivals = _count(s, "arrivals")
    served = _count(s, "completed") + _count(s, "errors")
    measurable = min(util, arrivals, served, m.probe["entered"], m.now) > 0
    if not measurable:
        return None
    capacity = served / (util * m.now)  # jobs per time when every server is busy
    share = arrivals / m.probe["entered"]
    servers = max(1, round(s.get("busy", {}).get("avg", util) / util))
    return {"entity": mid, "capacity": capacity / share, "utilization": util, "servers": servers}


def _count(summary: dict, name: str) -> float:
    m = summary.get(name)
    return float(m.get("value", 0)) if isinstance(m, dict) else 0.0


def _delay_params(r: dict) -> dict:
    mean, std = r["mean"], r["std"]
    if std > 1e-12 and mean > 0:
        return {"delay": {"dist": "lognormal", "mean": mean, "std": std}}
    return {"delay": mean}


def _server_params(probe: dict, bottleneck: dict) -> tuple[dict, dict]:
    mu = bottleneck["capacity"]  # group capacity, jobs per time
    lam = probe["arrival_rate"]
    target = probe["residence"]["mean"]
    if lam < mu:
        c = fit_servers(lam, mu, target)
        note = "M/M/c fit: capacity and mean residence match the measurement"
    else:  # measured in overload: the residence is not a steady-state value
        c = bottleneck["servers"]
        note = "measured at or above capacity; servers taken from the bottleneck"
    service = float(f"{c / mu:.6g}")
    params = {"servers": c, "service": {"dist": "exp", "mean": service}}
    fit = {
        "capacity": mu,
        "bottleneck": bottleneck["entity"],
        "bottleneck_utilization": bottleneck["utilization"],
        "predicted_residence": mmc_residence(lam, c, service) if lam < mu else None,
        "note": note,
    }
    return params, fit


def fit_servers(lam: float, mu: float, target: float) -> int:
    """The c whose M/M/c residence (capacity mu, arrival rate lam) is closest to target.

    With the total capacity fixed, residence grows with c (one fast server beats c
    slow ones), so the first c that reaches the target, or the one before it, is best.
    """
    prev = None
    for c in range(1, MAX_SERVERS + 1):
        w = mmc_residence(lam, c, c / mu)
        if w >= target:
            if prev is not None and target - prev < w - target:
                return c - 1
            return c
        prev = w
    return MAX_SERVERS


def mmc_residence(lam: float, c: int, service: float) -> float:
    """Mean time in an M/M/c system (queue + service); inf if unstable."""
    a = lam * service  # offered load in servers
    rho = a / c
    if rho >= 1:
        return math.inf
    # Erlang C via the stable recurrence on Erlang B
    b = 1.0
    for k in range(1, c + 1):
        b = a * b / (k + a * b)
    wait_prob = b / (1 - rho + rho * b)
    return service + wait_prob * service / (c * (1 - rho))


__all__ = [
    "Measurement",
    "Request",
    "expanded",
    "find_bottleneck",
    "fit_servers",
    "mmc_residence",
    "run_calibration",
    "suggest_abstract",
]
