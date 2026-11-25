"""Composites (milestone M4): compile groups into the flat model the kernel runs.

The kernel only knows flat entities and connections. ``flatten`` turns a model
with ``groups`` into that flat form:

- an **expanded** group stays as its member entities; outside connections that
  name a boundary port (``pool.in``) are rewritten to the inner port it maps to;
- an **abstract** group's members and internal connections are dropped and one
  stand-in entity, with the group's id, takes their place; outside connections
  to inner ports are rewritten to the matching boundary port.

Rewritten connections keep their original key (``id`` or ``from->to``) so link
metrics and the live view match what the user drew.

``GroupProbe`` measures messages crossing an expanded group's boundary. This
is what calibration uses to derive an abstract stand-in.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .metrics import Samples
from .model import ConnectionSpec, EntitySpec, GroupSpec, ModelSpec, PortRef


class CompositeError(ValueError):
    """A group is inconsistent (bad boundary, unknown members, ...)."""


@dataclass
class Boundary:
    """Where each group's boundary sits in the flat model."""

    inputs: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    outputs: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    # (flat entity, port) -> (group id, boundary port), for expanded groups only


def _ref(r: str) -> tuple[str, str]:
    p = PortRef.parse(r)
    return p.entity, p.port


def _members_by_group(model: ModelSpec) -> dict[str, set[str]]:
    members: dict[str, set[str]] = {}
    for e in model.entities:
        if e.group:
            members.setdefault(e.group, set()).add(e.id)
    return members


def _check_boundary(g: GroupSpec, inner: set[str]) -> None:
    sides = [("input", n, t) for n, t in g.inputs.items()]
    sides += [("output", n, t) for n, t in g.outputs.items()]
    for kind, name, target in sides:
        if _ref(target)[0] not in inner:
            raise CompositeError(
                f"group {g.id!r}: boundary {kind} {name!r} maps to {target!r}, "
                "which is not a member"
            )


def _check_group(g: GroupSpec, entity_ids: set[str], inner: set[str]) -> None:
    if g.id in entity_ids:
        raise CompositeError(f"group {g.id!r} has the same id as an entity")
    if not inner:
        raise CompositeError(f"group {g.id!r} has no member entities")
    if g.mode == "abstract" and g.abstract is None:
        raise CompositeError(f"group {g.id!r} is abstract but has no abstract behavior")
    _check_boundary(g, inner)


def check_groups(model: ModelSpec) -> dict[str, set[str]]:
    """Validate group specs; return group id -> member entity ids."""
    entity_ids = {e.id for e in model.entities}
    members = _members_by_group(model)
    ids = [g.id for g in model.groups]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise CompositeError(f"duplicate group {sorted(dupes)[0]!r}")
    for g in model.groups:
        _check_group(g, entity_ids, members.get(g.id, set()))
    return members


class _Flattener:
    """Rewrites one model's groups into flat entities and connections (see ``flatten``)."""

    def __init__(self, model: ModelSpec) -> None:
        members = check_groups(model)
        self.model = model
        self.groups = {g.id: g for g in model.groups}
        self.owner = {m: gid for gid, ms in members.items() if gid in self.groups for m in ms}
        self.abstract = {gid for gid, g in self.groups.items() if g.mode == "abstract"}

    def entities(self) -> list[EntitySpec]:
        kept = [e for e in self.model.entities if self.owner.get(e.id) not in self.abstract]
        stand_ins = [
            EntitySpec(
                id=gid, type=self.groups[gid].abstract.type, params=self.groups[gid].abstract.params
            )
            for gid in sorted(self.abstract)
        ]
        return kept + stand_ins

    def connections(self) -> list[ConnectionSpec]:
        return [c2 for c in self.model.connections if (c2 := self._connection(c)) is not None]

    def _connection(self, c: ConnectionSpec) -> ConnectionSpec | None:
        src_owner, dst_owner = self.owner.get(c.src.entity), self.owner.get(c.dst.entity)
        if src_owner in self.abstract and src_owner == dst_owner:
            return None  # internal to an abstract group
        key = c.key()
        src, dst = self._endpoint(c.source, True, key), self._endpoint(c.target, False, key)
        if (src, dst) == (c.source, c.target):
            return c
        return c.model_copy(update={"id": key, "source": src, "target": dst})

    def _endpoint(self, ref: str, outgoing: bool, key: str) -> str:
        ent, port = _ref(ref)
        if ent in self.groups:
            return self._boundary_ref(self.groups[ent], port, outgoing, key)
        gid = self.owner.get(ent)
        if gid in self.abstract:
            return self._through_boundary(self.groups[gid], ref, outgoing, key)
        return ref

    def _boundary_ref(self, g: GroupSpec, port: str, outgoing: bool, key: str) -> str:
        """``group.port`` -> the stand-in's port (abstract) or the inner port (expanded)."""
        mapping = g.outputs if outgoing else g.inputs
        if port not in mapping:
            side = "output" if outgoing else "input"
            raise CompositeError(
                f"connection {key}: group {g.id!r} has no boundary {side} {port!r}"
            )
        return f"{g.id}.{port}" if g.id in self.abstract else mapping[port]

    def _through_boundary(self, g: GroupSpec, ref: str, outgoing: bool, key: str) -> str:
        """An inner port of an abstract group -> the stand-in's matching boundary port."""
        mapping = g.outputs if outgoing else g.inputs
        name = next((n for n, target in mapping.items() if target == ref), None)
        if name is None:
            raise CompositeError(
                f"connection {key} reaches inside abstract group {g.id!r} through {ref!r}, "
                "which is not a boundary port"
            )
        return f"{g.id}.{name}"

    def boundary(self) -> Boundary:
        b = Boundary()
        for gid, g in self.groups.items():
            if gid in self.abstract:
                continue
            b.inputs.update({_ref(t): (gid, n) for n, t in g.inputs.items()})
            b.outputs.update({_ref(t): (gid, n) for n, t in g.outputs.items()})
        return b


def flatten(model: ModelSpec) -> tuple[ModelSpec, Boundary]:
    if not model.groups:
        return model, Boundary()
    f = _Flattener(model)
    flat = model.model_copy(
        update={"entities": f.entities(), "connections": f.connections(), "groups": []}
    )
    return flat, f.boundary()


class GroupProbe:
    """Counts and times messages crossing one expanded group's boundary."""

    def __init__(self, group: str, rng: random.Random, start: float) -> None:
        self.group = group
        self.start = start
        self.entered = 0
        self.exited = 0
        self.dropped = 0
        self.open: dict[int, float] = {}  # msg id -> entry time
        self.residence = Samples(rng)

    def enter(self, msg_id: int, now: float) -> None:
        if msg_id not in self.open:  # a loop back into the group keeps the first entry
            self.entered += 1
            self.open[msg_id] = now

    def exit(self, msg_id: int, now: float) -> None:
        t0 = self.open.pop(msg_id, None)
        if t0 is not None:
            self.exited += 1
            self.residence.add(now - t0)

    def gone(self, msg_id: int) -> None:
        if self.open.pop(msg_id, None) is not None:
            self.dropped += 1

    def summary(self, now: float) -> dict:
        done = self.exited + self.dropped
        span = now - self.start
        return {
            "entered": self.entered,
            "exited": self.exited,
            "dropped": self.dropped,
            "inside": len(self.open),
            "loss": self.dropped / done if done else 0.0,
            "throughput": self.exited / span if span > 0 else 0.0,
            "arrival_rate": self.entered / span if span > 0 else 0.0,
            "residence": self.residence.summary(now),
        }


def suggest_abstract(probe_summary: dict, now: float, seed: int) -> dict:
    """An abstract stand-in that reproduces the measured boundary behavior.

    A ``delay`` with lognormal residence (unlimited concurrency) and the measured
    loss as ``error_rate``. It is only valid near the measured arrival rate:
    it does not know about the group's capacity, so it won't saturate.
    """
    r = probe_summary["residence"]
    if not r.get("count"):
        raise CompositeError("no message crossed the group boundary; run longer or check wiring")
    mean, std = r["mean"], r["std"]
    if std > 1e-12 and mean > 0:
        delay = {"dist": "lognormal", "mean": mean, "std": std}
    else:
        delay = mean
    params: dict = {"delay": delay}
    if probe_summary["loss"] > 0:
        params["error_rate"] = probe_summary["loss"]
    return {
        "type": "delay",
        "params": params,
        "calibration": {
            "measured_until": now,
            "seed": seed,
            "samples": r["count"],
            "arrival_rate": probe_summary["arrival_rate"],
            "throughput": probe_summary["throughput"],
            "residence_p95": r.get("p95"),
            "loss": probe_summary["loss"],
            "note": "valid near the measured arrival rate; the stand-in has no capacity limit",
        },
    }


__all__ = [
    "Boundary",
    "CompositeError",
    "GroupProbe",
    "GroupSpec",
    "check_groups",
    "flatten",
    "suggest_abstract",
]
