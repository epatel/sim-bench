"""The discrete-event simulation kernel.

One ``Simulation`` holds the clock, the event queue, the runtime entities and
links, and all metrics. It is deterministic for a given (model, seed): ties in
time are broken by scheduling order, and every random draw comes from a named
stream (see ``rng.RngPool``).
"""

from __future__ import annotations

import heapq
import pickle
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .behaviors import BEHAVIORS, Behavior
from .metrics import Counter, Gauge, Samples
from .model import ConnectionSpec, EntitySpec, ModelSpec
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

    def __init__(self, sim: Simulation, spec: EntitySpec) -> None:
        self._sim = sim
        self.entity_id = spec.id
        self.params = spec.params
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
    ) -> None:
        self.model = model
        self.seed = seed
        self.now = 0.0
        self.events_processed = 0
        self.rngs = RngPool(seed)
        self._queue: list[Event] = []
        self._seq = 0
        self._msg_seq = 0
        self._listeners: list[Listener] = []
        self._started = False
        registry = behaviors or BEHAVIORS

        self.entities: dict[str, Behavior] = {}
        self.ports: dict[str, tuple[list[str], list[str]]] = {}
        for spec in model.entities:
            if spec.id in self.entities:
                raise ModelError(f"duplicate entity id {spec.id!r}")
            cls = registry.get(spec.type)
            if cls is None:
                raise ModelError(f"entity {spec.id!r}: unknown type {spec.type!r}")
            self.ports[spec.id] = cls.ports(spec.params)
            self.entities[spec.id] = cls(Context(self, spec))

        self.links: dict[str, Link] = {}
        self._out: dict[tuple[str, str], Link] = {}
        for conn in model.connections:
            src = self._check_port(conn.src, out=True)
            self._check_port(conn.dst, out=False)
            if src in self._out:
                raise ModelError(f"output {conn.source} is connected more than once")
            if conn.key() in self.links:
                raise ModelError(f"duplicate connection {conn.key()!r}")
            link = Link(self, conn)
            self.links[link.key] = link
            self._out[src] = link

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
        link = self._out.get((entity, port))
        if link is None:
            self.entities[entity].ctx.counter("unrouted").add()
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
                    "entity": ev.entity,
                    "port": ev.key,
                    "msg": ev.data.id,
                }
            )
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
        entities = {}
        for eid, b in self.entities.items():
            entities[eid] = {"type": b.kind, **b.ctx.metrics.summary(self.now), **b.summary_extra()}
        links = {k: link.metrics.summary(self.now) for k, link in self.links.items()}
        return {
            "model": self.model.name,
            "seed": self.seed,
            "time": self.now,
            "events": self.events_processed,
            "pending": len(self._queue),
            "entities": entities,
            "links": links,
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
