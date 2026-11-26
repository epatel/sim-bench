"""The discrete-event simulation kernel.

One ``Simulation`` holds the clock, the event queue, the runtime entities and
links, and all metrics. It is deterministic for a given (model, seed): ties in
time are broken by scheduling order, and every random draw comes from a named
stream (see ``rng.RngPool``).
"""

from __future__ import annotations

import copy
import heapq
import pickle
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .behaviors import Behavior
from .composite import CompositeError, GroupProbe, flatten
from .library import Library, LibraryError
from .metrics import Counter, Gauge, Samples
from .model import ConnectionSpec, EntitySpec, ModelSpec, PatchSpec
from .rng import RngPool, sample

Listener = Callable[[dict[str, Any]], None]


class ModelError(ValueError):
    """The model can't be simulated as written (unknown type, bad port, ...)."""


@dataclass
class Message:
    id: int
    payload: dict[str, Any]
    created_at: float
    origin: str
    size: float = 1.0
    hops: int = 0


@dataclass(order=True)
class Event:
    time: float
    seq: int
    kind: str = field(compare=False)  # "deliver" | "timer"
    entity: str = field(compare=False)
    key: str = field(compare=False)  # port name for deliver, timer key for timer
    data: Any = field(compare=False, default=None)
    link: str | None = field(compare=False, default=None)


class MetricSet:
    def __init__(self, sim: Simulation, owner: str) -> None:
        self._sim = sim
        self.owner = owner
        self.items: dict[str, Counter | Samples | Gauge] = {}

    def counter(self, name: str) -> Counter:
        return self._get(name, Counter)

    def samples(self, name: str) -> Samples:
        return self._get(
            name, lambda: Samples(self._sim.rngs.stream(f"metric:{self.owner}:{name}"))
        )

    def gauge(self, name: str) -> Gauge:
        return self._get(name, lambda: Gauge(start=self._sim.now, _last_t=self._sim.now))

    def _get(self, name, factory):
        m = self.items.get(name)
        if m is None:
            m = self.items[name] = factory()
        return m

    def summary(self, now: float) -> dict[str, Any]:
        return {name: m.summary(now) for name, m in sorted(self.items.items())}


class Context:
    """What a behavior may touch. Bound to one entity."""

    def __init__(self, sim: Simulation, spec: EntitySpec, params: dict[str, Any]) -> None:
        self._sim = sim
        self.entity_id = spec.id
        self.item_type = spec.type
        self.params = params  # type defaults merged with the entity's own params
        self.rng = sim.rngs.stream(f"entity:{spec.id}")
        self.metrics = MetricSet(sim, spec.id)

    @property
    def now(self) -> float:
        return self._sim.now

    def message(self, payload: dict[str, Any] | None = None, size: float = 1.0) -> Message:
        return self._sim.new_message(self.entity_id, payload or {}, size)

    def send(self, port: str, msg: Message) -> bool:
        return self._sim.send(self.entity_id, port, msg)

    def drop(self, msg: Message, reason: str) -> None:
        self._sim.message_gone(msg)
        self._sim.emit({"type": "drop", "entity": self.entity_id, "msg": msg.id, "reason": reason})

    def schedule(self, delay: float, key: str, data: Any = None) -> None:
        if delay < 0:
            raise ValueError(f"{self.entity_id}: cannot schedule {delay} in the past")
        self._sim.schedule(self._sim.now + delay, "timer", self.entity_id, key, data)

    def counter(self, name: str) -> Counter:
        return self.metrics.counter(name)

    def samples(self, name: str) -> Samples:
        return self.metrics.samples(name)

    def gauge(self, name: str) -> Gauge:
        return self.metrics.gauge(name)

    def connected(self, port: str) -> bool:
        """Whether an output port has a link (sending on an unlinked port is 'unrouted')."""
        return (self.entity_id, port) in self._sim._out


class Link:
    def __init__(self, sim: Simulation, spec: ConnectionSpec) -> None:
        self.spec = spec
        self.key = spec.key()
        self.rng = sim.rngs.stream(f"link:{self.key}")
        self.metrics = MetricSet(sim, f"link:{self.key}")
        self.busy_until = 0.0
        self.last_arrival = 0.0
        for name in ("sent", "delivered", "lost"):
            self.metrics.counter(name)
        self.metrics.gauge("in_flight")

    def transmit(self, sim: Simulation, msg: Message) -> None:
        spec, now = self.spec, sim.now
        self.metrics.counter("sent").add()
        if not spec.up or (spec.loss and self.rng.random() < spec.loss):
            self.metrics.counter("lost").add()
            sim.message_gone(msg)
            sim.emit({"type": "lost", "link": self.key, "msg": msg.id})
            return
        depart = now
        if spec.bandwidth:
            depart = max(now, self.busy_until) + msg.size / spec.bandwidth
            self.busy_until = depart
        arrive = depart + sample(spec.latency, self.rng)
        if spec.ordered:
            arrive = max(arrive, self.last_arrival)
            self.last_arrival = arrive
        self.metrics.gauge("in_flight").add(1, now)
        dst = spec.dst
        sim.schedule(arrive, "deliver", dst.entity, dst.port, msg, link=self.key)
        sim.emit({"type": "send", "link": self.key, "msg": msg.id, "t": now, "arrive": arrive})


class Simulation:
    def __init__(
        self,
        model: ModelSpec,
        seed: int = 0,
        behaviors: dict[str, type[Behavior]] | None = None,
        library: Library | None = None,
    ) -> None:
        self.model = model  # as written, with groups
        try:
            self.flat, boundary = flatten(model)
        except CompositeError as e:
            raise ModelError(str(e)) from e
        self.seed = seed
        self.now = 0.0
        self.events_processed = 0
        self.rngs = RngPool(seed)
        self._queue: list[Event] = []
        self._seq = 0
        self._msg_seq = 0
        self._listeners: list[Listener] = []
        self._started = False
        self.library = library or Library(behaviors)

        self.entities: dict[str, Behavior] = {}
        self.ports: dict[str, tuple[list[str], list[str]]] = {}
        for spec in self.flat.entities:
            self._add_entity(spec)
        self.links: dict[str, Link] = {}
        self._out: dict[tuple[str, str], Link] = {}
        for conn in self.flat.connections:
            self._add_link(conn)
        # Boundary probes for expanded groups (kept on the sim so snapshots carry them).
        self.probes: dict[str, GroupProbe] = {}
        self._probe_in = self._attach_probes(boundary.inputs)
        self.patches_applied: list[dict[str, Any]] = []
        self._probe_out = self._attach_probes(boundary.outputs)

    def _add_entity(self, spec: EntitySpec) -> None:
        if spec.id in self.entities:
            raise ModelError(f"duplicate entity id {spec.id!r}")
        try:
            resolved = self.library.resolve(spec.type)
        except LibraryError as e:
            raise ModelError(f"entity {spec.id!r}: {e}") from e
        params = {**resolved.defaults, **copy.deepcopy(spec.params)}
        try:
            self.ports[spec.id] = resolved.behavior.ports(params)
        except ValueError as e:  # e.g. an invalid state machine
            raise ModelError(f"entity {spec.id!r}: {e}") from e
        self.entities[spec.id] = resolved.behavior(Context(self, spec, params))

    def _add_link(self, conn: ConnectionSpec) -> None:
        src = self._check_port(conn.src, out=True)
        self._check_port(conn.dst, out=False)
        if src in self._out:
            raise ModelError(f"output {conn.source} is connected more than once")
        if conn.key() in self.links:
            raise ModelError(f"duplicate connection {conn.key()!r}")
        link = Link(self, conn)
        self.links[link.key] = link
        self._out[src] = link

    def _attach_probes(self, mapping) -> dict[tuple[str, str], list[GroupProbe]]:
        """Map each boundary inner port to its group's probe (one probe per group)."""
        table: dict[tuple[str, str], list[GroupProbe]] = {}
        for inner, (gid, _) in mapping.items():
            if gid not in self.probes:
                self.probes[gid] = GroupProbe(gid, self.rngs.stream(f"probe:{gid}"), 0.0)
            table.setdefault(inner, []).append(self.probes[gid])
        return table

    # --- scenario patches during a run ----------------------------------------

    def check_patch(self, patch: PatchSpec) -> None:
        """Raise ModelError unless ``patch`` can be applied to this running sim."""
        if patch.group:
            raise ModelError("a group's mode cannot change during a run")
        if patch.entity and patch.entity not in self.entities:
            raise ModelError(
                f"patch: no running entity {patch.entity!r} (inside an abstract group?)"
            )
        if patch.link and patch.link not in self.links:
            raise ModelError(f"patch: no link {patch.link!r}")

    def schedule_patch(self, time: float, patch: PatchSpec) -> None:
        self.check_patch(patch)
        self.schedule(time, "patch", "", "", patch)

    def apply_patch(self, patch: PatchSpec) -> None:
        """Change an entity param or a link field now (a timed scenario patch)."""
        self.check_patch(patch)
        if patch.entity:
            behavior = self.entities[patch.entity]
            behavior.ctx.params[patch.param] = copy.deepcopy(patch.value)
            behavior.on_params_changed({patch.param})
        else:
            link = self.links[patch.link]
            link.spec = link.spec.model_copy(update={patch.field: patch.value})
        self.patches_applied.append({"t": self.now, "patch": patch.describe()})
        self.emit({"type": "patch", "t": self.now, "patch": patch.describe()})

    def message_gone(self, msg: Message) -> None:
        """A message was dropped, lost or unrouted: close it in any probe that holds it."""
        for probe in self.probes.values():
            probe.gone(msg.id)

    def _check_port(self, ref, out: bool) -> tuple[str, str]:
        if ref.entity not in self.ports:
            raise ModelError(f"connection refers to unknown entity {ref.entity!r}")
        ins, outs = self.ports[ref.entity]
        allowed = outs if out else ins
        if ref.port not in allowed:
            kind = "output" if out else "input"
            raise ModelError(f"{ref} is not an {kind} port (has {allowed})")
        return ref.entity, ref.port

    # --- scheduling -------------------------------------------------------

    def schedule(self, time: float, kind: str, entity: str, key: str, data=None, link=None) -> None:
        if time < self.now:
            raise ValueError(f"event at {time} is before now ({self.now})")
        self._seq += 1
        heapq.heappush(self._queue, Event(time, self._seq, kind, entity, key, data, link))

    def new_message(self, origin: str, payload: dict[str, Any], size: float) -> Message:
        self._msg_seq += 1
        return Message(self._msg_seq, payload, self.now, origin, size)

    def send(self, entity: str, port: str, msg: Message) -> bool:
        for probe in self._probe_out.get((entity, port), ()):
            probe.exit(msg.id, self.now)
        link = self._out.get((entity, port))
        if link is None:
            self.entities[entity].ctx.counter("unrouted").add()
            self.message_gone(msg)
            self.emit({"type": "unrouted", "entity": entity, "port": port, "msg": msg.id})
            return False
        msg.hops += 1
        link.transmit(self, msg)
        return True

    # --- running ----------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for behavior in self.entities.values():
            behavior.on_start()

    @property
    def pending(self) -> int:
        return len(self._queue)

    @property
    def next_time(self) -> float | None:
        return self._queue[0].time if self._queue else None

    def step(self) -> Event | None:
        """Process one event. Returns it, or None when the queue is empty."""
        self.start()
        if not self._queue:
            return None
        ev = heapq.heappop(self._queue)
        self.now = ev.time
        self.events_processed += 1
        if ev.kind == "patch":
            self.apply_patch(ev.data)
            return ev
        target = self.entities[ev.entity]
        if ev.kind == "deliver":
            if ev.link:
                link = self.links[ev.link]
                link.metrics.counter("delivered").add()
                link.metrics.gauge("in_flight").add(-1, self.now)
            self.emit(
                {
                    "type": "deliver",
                    "t": self.now,
                    "link": ev.link,
                    "entity": ev.entity,
                    "port": ev.key,
                    "msg": ev.data.id,
                }
            )
            for probe in self._probe_in.get((ev.entity, ev.key), ()):
                probe.enter(ev.data.id, self.now)
            target.on_message(ev.key, ev.data)
        else:
            target.on_timer(ev.key, ev.data)
        return ev

    def run(self, until: float | None = None, max_events: int | None = None) -> int:
        """Process events up to time ``until`` (inclusive). Returns events processed."""
        self.start()
        n = 0
        while self._queue and (until is None or self._queue[0].time <= until):
            if max_events is not None and n >= max_events:
                return n
            self.step()
            n += 1
        if until is not None and until > self.now:
            self.now = until
        return n

    # --- observation ------------------------------------------------------

    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Listener) -> None:
        self._listeners.remove(fn)

    def emit(self, record: dict[str, Any]) -> None:
        for fn in self._listeners:
            fn(record)

    def summary(self) -> dict[str, Any]:
        self.start()  # behaviors set up their state (and metrics) in on_start
        entities = {}
        for eid, b in self.entities.items():
            entities[eid] = {
                "type": b.kind,
                "item_type": b.ctx.item_type,
                **b.ctx.metrics.summary(self.now),
                **b.summary_extra(),
            }
        links = {k: link.metrics.summary(self.now) for k, link in self.links.items()}
        return {
            "model": self.model.name,
            "seed": self.seed,
            "time": self.now,
            "events": self.events_processed,
            "pending": len(self._queue),
            "entities": entities,
            "links": links,
            "groups": {gid: p.summary(self.now) for gid, p in self.probes.items()},
            "patches": list(self.patches_applied),
        }

    # --- snapshot / fork --------------------------------------------------

    def snapshot(self) -> bytes:
        """Serialize the full run state (listeners are not included)."""
        listeners, self._listeners = self._listeners, []
        try:
            return pickle.dumps(self)
        finally:
            self._listeners = listeners

    @staticmethod
    def restore(data: bytes) -> Simulation:
        # pickle: only ever restore snapshots this process produced, never user uploads.
        return pickle.loads(data)

    def fork(self) -> Simulation:
        """An independent copy that continues exactly as this run would."""
        return Simulation.restore(self.snapshot())
