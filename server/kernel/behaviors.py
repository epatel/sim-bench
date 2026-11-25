"""Tier-1 (stochastic / black-box) behaviors.

A behavior reacts to events through its ``Context``. The same Context API is
what tier-2 state machines and tier-3 scripts will use later, so keep it small:
``now``, ``params``, ``rng``, ``send``, ``schedule``, ``message``, and the metric
helpers ``counter`` / ``samples`` / ``gauge``.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any, ClassVar

from .rng import sample

if TYPE_CHECKING:
    from .sim import Context, Message


class Behavior:
    kind: ClassVar[str] = ""
    description: ClassVar[str] = ""
    icon: ClassVar[str] = "fa-cube"  # Font Awesome icon for the canvas
    # Parameter metadata for editors: name -> {"type", "default", "help", ...}.
    # Types: "dist" (distribution spec), "int", "float", "enum" (+ "options"),
    # "object". "nullable": True means null is allowed (e.g. unbounded).
    params_schema: ClassVar[dict[str, dict[str, Any]]] = {}

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    @classmethod
    def ports(cls, params: dict[str, Any]) -> tuple[list[str], list[str]]:
        """(input ports, output ports) for the given parameters."""
        return [], []

    def on_start(self) -> None:
        pass

    def on_message(self, port: str, msg: Message) -> None:
        pass

    def on_timer(self, key: str, data: Any) -> None:
        pass

    def summary_extra(self) -> dict[str, Any]:
        return {}

    def on_params_changed(self, names: set[str]) -> None:
        """Params were patched mid-run. Most behaviors read params on each event and
        need nothing; ones that cache a param at start must refresh it here."""


class Source(Behavior):
    kind = "source"
    description = "Emits messages with a given inter-arrival time distribution."
    icon = "fa-faucet"
    params_schema = {
        "interarrival": {"type": "dist", "default": 1.0, "help": "time between messages"},
        "count": {
            "type": "int",
            "default": None,
            "nullable": True,
            "help": "stop after this many (empty = forever)",
        },
        "start": {"type": "float", "default": 0.0, "help": "time of the first gap"},
        "size": {"type": "dist", "default": 1.0, "help": "message size (for link bandwidth)"},
        "payload": {"type": "object", "default": {}, "help": "fields copied into each message"},
    }

    @classmethod
    def ports(cls, params):
        return [], ["out"]

    def on_start(self) -> None:
        p = self.ctx.params
        self.remaining = p.get("count")
        self.ctx.schedule(float(p.get("start", 0.0)) + self._gap(), "emit")

    def _gap(self) -> float:
        return sample(self.ctx.params.get("interarrival", 1.0), self.ctx.rng)

    def on_timer(self, key, data):
        if self.remaining is not None:
            if self.remaining <= 0:
                return
            self.remaining -= 1
        p = self.ctx.params
        msg = self.ctx.message(
            dict(p.get("payload", {})), size=sample(p.get("size", 1.0), self.ctx.rng)
        )
        self.ctx.counter("emitted").add()
        self.ctx.send("out", msg)
        if self.remaining is None or self.remaining > 0:
            self.ctx.schedule(self._gap(), "emit")


class Server(Behavior):
    kind = "server"
    description = "A queue in front of N parallel servers (G/G/c/K)."
    icon = "fa-server"
    params_schema = {
        "servers": {"type": "int", "default": 1, "help": "parallel workers"},
        "service": {"type": "dist", "default": 1.0, "help": "service time per message"},
        "queue_capacity": {
            "type": "int",
            "default": None,
            "nullable": True,
            "help": "max waiting messages (empty = unbounded)",
        },
        "error_rate": {"type": "float", "default": 0.0, "help": "probability a job fails"},
        "error_port": {"type": "bool", "default": False, "help": "send failed jobs to 'error'"},
    }

    @classmethod
    def ports(cls, params):
        return ["in"], ["out", "error"] if params.get("error_port") else ["out"]

    def on_start(self) -> None:
        p = self.ctx.params
        self.servers = int(p.get("servers", 1))
        self.capacity = p.get("queue_capacity")  # None = unbounded
        self.busy = 0
        self.queue: deque = deque()
        for name in ("arrivals", "completed", "dropped", "errors"):
            self.ctx.counter(name)
        self.ctx.gauge("queue")
        self.ctx.gauge("busy")

    def on_message(self, port, msg):
        self.ctx.counter("arrivals").add()
        if self.busy < self.servers:
            self._begin(msg, self.ctx.now)
        elif self.capacity is None or len(self.queue) < self.capacity:
            self.queue.append((msg, self.ctx.now))
            self.ctx.gauge("queue").set(len(self.queue), self.ctx.now)
        else:
            self.ctx.counter("dropped").add()
            self.ctx.drop(msg, "queue full")

    def on_params_changed(self, names: set[str]) -> None:
        p = self.ctx.params
        self.servers = int(p.get("servers", 1))
        self.capacity = p.get("queue_capacity")
        while self.queue and self.busy < self.servers:  # more servers: start waiting jobs
            nxt, t = self.queue.popleft()
            self.ctx.gauge("queue").set(len(self.queue), self.ctx.now)
            self._begin(nxt, t)

    def _begin(self, msg, enqueued_at: float) -> None:
        now = self.ctx.now
        self.busy += 1
        self.ctx.gauge("busy").set(self.busy, now)
        self.ctx.samples("wait").add(now - enqueued_at)
        service = sample(self.ctx.params.get("service", 1.0), self.ctx.rng)
        self.ctx.schedule(service, "done", (msg, enqueued_at))

    def on_timer(self, key, data):
        msg, enqueued_at = data
        now = self.ctx.now
        self.busy -= 1
        self.ctx.gauge("busy").set(self.busy, now)
        self.ctx.samples("residence").add(now - enqueued_at)
        error_rate = float(self.ctx.params.get("error_rate", 0.0))
        if error_rate and self.ctx.rng.random() < error_rate:
            self.ctx.counter("errors").add()
            if self.ctx.params.get("error_port"):
                self.ctx.send("error", msg)
            else:
                self.ctx.drop(msg, "error")
        else:
            self.ctx.counter("completed").add()
            self.ctx.send("out", msg)
        if self.queue:
            nxt, t = self.queue.popleft()
            self.ctx.gauge("queue").set(len(self.queue), now)
            self._begin(nxt, t)

    def summary_extra(self):
        busy = self.ctx.gauge("busy")
        return {"utilization": busy.time_average(self.ctx.now) / self.servers}


class Delay(Behavior):
    kind = "delay"
    description = "Holds each message for a sampled time, with unlimited concurrency."
    icon = "fa-hourglass-half"
    params_schema = {
        "delay": {"type": "dist", "default": 0.0, "help": "hold time per message"},
        "error_rate": {"type": "float", "default": 0.0, "help": "probability a message is lost"},
    }

    @classmethod
    def ports(cls, params):
        return ["in"], ["out"]

    def on_start(self) -> None:
        self.ctx.counter("passed")
        self.ctx.counter("errors")

    def on_message(self, port, msg):
        self.ctx.schedule(sample(self.ctx.params.get("delay", 0.0), self.ctx.rng), "out", msg)

    def on_timer(self, key, data):
        error_rate = float(self.ctx.params.get("error_rate") or 0.0)
        if error_rate and self.ctx.rng.random() < error_rate:
            self.ctx.counter("errors").add()
            self.ctx.drop(data, "error")
            return
        self.ctx.counter("passed").add()
        self.ctx.send("out", data)


class Router(Behavior):
    kind = "router"
    description = "Forwards each message to one output, by weight or round-robin."
    icon = "fa-shuffle"
    params_schema = {
        "mode": {
            "type": "enum",
            "options": ["random", "round_robin"],
            "default": "random",
            "help": "how to pick an output",
        },
        "routes": {
            "type": "object",
            "default": {"out": 1},
            "help": "output port name -> weight (defines the outputs)",
        },
    }

    @classmethod
    def ports(cls, params):
        return ["in"], list(params.get("routes", {"out": 1}))

    def on_start(self) -> None:
        self.next = 0
        self._read_routes()

    def _read_routes(self) -> None:
        routes = self.ctx.params.get("routes", {"out": 1})
        if getattr(self, "names", None) not in (None, list(routes)):
            raise ValueError("a router's route names (its ports) cannot change mid-run")
        self.names = list(routes)
        self.weights = [float(w) for w in routes.values()]
        self.mode = self.ctx.params.get("mode", "random")

    def on_params_changed(self, names: set[str]) -> None:
        self._read_routes()

    def on_message(self, port, msg):
        if self.mode == "round_robin":
            out = self.names[self.next % len(self.names)]
            self.next += 1
        else:
            out = self.ctx.rng.choices(self.names, weights=self.weights)[0]
        self.ctx.counter(f"to_{out}").add()
        self.ctx.send(out, msg)


class Sink(Behavior):
    kind = "sink"
    description = "Consumes messages and records end-to-end latency."
    icon = "fa-flag-checkered"

    @classmethod
    def ports(cls, params):
        return ["in"], []

    def on_start(self) -> None:
        self.ctx.counter("received")

    def on_message(self, port, msg):
        self.ctx.counter("received").add()
        self.ctx.samples("latency").add(self.ctx.now - msg.created_at)
        self.ctx.samples("hops").add(msg.hops)


BEHAVIORS: dict[str, type[Behavior]] = {
    cls.kind: cls for cls in (Source, Server, Delay, Router, Sink)
}
