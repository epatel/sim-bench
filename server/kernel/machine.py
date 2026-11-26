"""Tier-2 behavior: a declarative state machine (milestone M5).

The machine definition is plain data (YAML/JSON), so it is safe to keep in a
model file, edit in the UI, or have the agent write. Expressions use
``expr.evaluate`` (no arbitrary code). It can also be written as a script that
reads like code (``machine_script.py``), which is parsed into this same data.

    machine:
      inputs: [in]                 # input ports  (default [in])
      outputs: [out, rejected]     # output ports (default [out])
      vars: {failures: 0}          # initial variable values
      initial: closed
      on: {...}                    # fallback handlers for every state
      states:
        closed:
          enter: [actions]         # run on entering the state
          exit:  [actions]         # run on leaving it
          on:
            in: [actions]          # a message arriving on port "in"
            retry: [actions]       # a timer event named "retry"

Actions (each a one-key mapping, run in order):

    send: port                     forward the current message (or a new one)
    emit: {port, payload: {k: expr}, size: expr}     always a new message
    drop: reason                   drop the current message
    set: {var: expr, ...}          assign variables
    goto: state                    leave this state (exit, then enter); ends the list
    after: expr, event: name       fire `event` later, carrying the current message;
                                   cancelled if the state that was current when it was
                                   set is left (keep: true to keep it). To time a
                                   state, start the timer in its `enter`.
    if: expr, then: [..], else: [..]
    count: name                    add 1 to a counter metric
    observe: {name: expr}          add a sample to a metric
    push: var                      append the current message to a list variable
    pop: var                       take the oldest message from a list variable
    release: {from: var, to: port} send every message in a list variable, oldest first

Expression names: variables, then params, then the model's globals
(``model_globals.py``: constants and functions), plus ``now``, ``state`` and ``msg``
(the payload, with ``_age``, ``_hops`` and ``_size`` added). Functions:
random(), uniform(a, b), exponential(mean), sample(dist), len, min, max, abs, round,
int, float.

A message that a handler neither sends, drops, pushes nor holds is absorbed
(counted as ``absorbed``). A runtime error drops the message, counts ``errors``
and is reported as ``last_error``; the run continues.
"""

from __future__ import annotations

from typing import Any

from .behaviors import Behavior
from .expr import ExprError, evaluate, parse
from .machine_script import ScriptError, parse_script
from .rng import sample

VERBS = (
    "send", "emit", "drop", "set", "goto", "after", "if",
    "count", "observe", "push", "pop", "release",
)  # fmt: skip
MAX_TRANSITIONS = 50  # per event, to stop goto loops


class MachineError(ValueError):
    pass


DEFAULT_MACHINE = """\
inputs in
outputs out

state idle:
  on in: send out
"""


# --- validation ------------------------------------------------------------------


def check_machine(m: Any) -> dict[str, Any]:
    """Validate a machine definition (a script or a mapping); return it as a mapping
    with defaults filled in."""
    m = _as_mapping(m)
    if not isinstance(m, dict):
        raise MachineError("machine must be a script or a mapping")
    m = {"inputs": ["in"], "outputs": ["out"], "vars": {}, "on": {}, **_yaml_on(m)}
    states = m.get("states")
    if not isinstance(states, dict) or not states:
        raise MachineError("machine needs at least one state")
    m["states"] = states = {k: _yaml_on(v or {}) for k, v in states.items()}
    if m.get("initial") not in states:
        raise MachineError(f"initial state {m.get('initial')!r} is not one of {list(states)}")
    checker = _Checker(m)
    checker.handlers("on", m["on"])
    for name, st in states.items():
        st = st or {}
        checker.actions(f"{name}.enter", st.get("enter", []))
        checker.actions(f"{name}.exit", st.get("exit", []))
        checker.handlers(f"{name}.on", st.get("on", {}))
    return m


def _as_mapping(m: Any) -> Any:
    if not isinstance(m, str):
        return m
    try:
        return parse_script(m)
    except ScriptError as e:
        raise MachineError(str(e)) from e


def _yaml_on(d: dict) -> dict:
    """YAML 1.1 reads a bare ``on:`` key as True; give it its name back."""
    if True in d:
        d = {("on" if k is True else k): v for k, v in d.items()}
    return d


class _Checker:
    def __init__(self, m: dict[str, Any]) -> None:
        self.states = set(m["states"])
        self.outputs = set(m["outputs"])

    def handlers(self, where: str, on: Any) -> None:
        if not isinstance(on, dict):
            raise MachineError(f"{where}: expected a mapping of event -> actions")
        for event, acts in on.items():
            self.actions(f"{where}.{event}", acts)

    def actions(self, where: str, acts: Any) -> None:
        if not isinstance(acts, list):
            raise MachineError(f"{where}: expected a list of actions")
        for i, a in enumerate(acts):
            self.action(f"{where}[{i}]", a)

    def action(self, where: str, a: Any) -> None:
        verbs = [k for k in (a if isinstance(a, dict) else {}) if k in VERBS]
        if len(verbs) != 1:
            raise MachineError(f"{where}: an action needs exactly one of {', '.join(VERBS)}")
        getattr(self, f"_{verbs[0]}")(where, a)

    def expr(self, where: str, e: Any) -> None:
        if isinstance(e, str):
            try:
                parse(e)
            except ExprError as err:
                raise MachineError(f"{where}: {err}") from err

    def port(self, where: str, p: Any) -> None:
        if p not in self.outputs:
            raise MachineError(f"{where}: {p!r} is not an output port {sorted(self.outputs)}")

    def _send(self, where, a):
        self.port(where, a["send"])

    def _emit(self, where, a):
        spec = a["emit"] if isinstance(a["emit"], dict) else {"port": a["emit"]}
        self.port(where, spec.get("port"))
        for v in (spec.get("payload") or {}).values():
            self.expr(where, v)
        self.expr(where, spec.get("size"))

    def _drop(self, where, a):
        pass

    def _set(self, where, a):
        for v in (a["set"] or {}).values():
            self.expr(where, v)

    def _goto(self, where, a):
        if a["goto"] not in self.states:
            raise MachineError(f"{where}: goto {a['goto']!r}, which is not a state")

    def _after(self, where, a):
        self.expr(where, a["after"])
        if not a.get("event"):
            raise MachineError(f"{where}: 'after' needs an 'event' name")

    def _if(self, where, a):
        self.expr(where, a["if"])
        self.actions(f"{where}.then", a.get("then", []))
        self.actions(f"{where}.else", a.get("else", []))

    def _count(self, where, a):
        pass

    def _observe(self, where, a):
        for v in (a["observe"] or {}).values():
            self.expr(where, v)

    def _push(self, where, a):
        pass

    _pop = _push

    def _release(self, where, a):
        spec = a["release"] if isinstance(a["release"], dict) else {}
        if "from" not in spec:
            raise MachineError(f"{where}: release needs 'from' (a list variable) and 'to'")
        self.port(where, spec.get("to"))


# --- runtime ------------------------------------------------------------------------


class _Goto(Exception):
    def __init__(self, state: str) -> None:
        self.state = state


class Machine(Behavior):
    kind = "machine"
    description = "A state machine: states, timers, variables and routing rules, as data."
    icon = "fa-gears"
    params_schema = {
        "machine": {
            "type": "machine",
            "default": DEFAULT_MACHINE,
            "help": "states, handlers and actions: a script, or the same as a mapping",
        },
    }

    @classmethod
    def ports(cls, params):
        m = check_machine(params.get("machine") or DEFAULT_MACHINE)
        return list(m["inputs"]), list(m["outputs"])

    def on_start(self) -> None:
        self.m = check_machine(self.ctx.params.get("machine") or DEFAULT_MACHINE)
        self.vars = {k: _copy(v) for k, v in self.m["vars"].items()}
        self.state = self.m["initial"]
        self.generation = 0  # bumps on every state change; scopes timers
        self.last_error: str | None = None
        for name in ("errors", "absorbed", "unhandled", "transitions"):
            self.ctx.counter(name)
        for s in self.m["states"]:
            self.ctx.gauge(f"in_{s}").set(1.0 if s == self.state else 0.0, self.ctx.now)
        self._dispatch(None, self._state().get("enter") or [], None)

    # events ------------------------------------------------------------------

    def on_message(self, port, msg):
        acts = self._handler(port)
        if acts is None:
            self.ctx.counter("unhandled").add()
            self.ctx.drop(msg, f"unhandled in state {self.state}")
            return
        self._dispatch(port, acts, msg)

    def on_timer(self, key, data):
        generation, event, msg, keep = data
        if not keep and generation != self.generation:
            self._discard(msg, f"timer {event} cancelled")  # its state was left
            return
        acts = self._handler(event)
        if acts is None:
            self._discard(msg, f"unhandled timer {event} in state {self.state}", unhandled=True)
            return
        self._dispatch(event, acts, msg)

    def _discard(self, msg, reason: str, unhandled: bool = False) -> None:
        """Drop a message a timer was holding (timers without one need nothing)."""
        if msg is None:
            return
        if unhandled:
            self.ctx.counter("unhandled").add()
        self.ctx.drop(msg, reason)

    def _state(self) -> dict[str, Any]:
        return self.m["states"][self.state] or {}

    def _handler(self, event: str) -> list | None:
        on = self._state().get("on") or {}
        return on.get(event, self.m["on"].get(event))

    def _dispatch(self, event: str | None, acts: list, msg) -> None:
        run = _Run(self, msg)
        try:
            run.actions(acts)
            run.settle()
        except (ExprError, MachineError, IndexError, TypeError) as e:
            self.ctx.counter("errors").add()
            self.last_error = f"{self.state}/{event or 'start'}: {e}"
            if run.msg is not None and not run.consumed:
                self.ctx.drop(run.msg, "machine error")

    def summary_extra(self):
        vars_view = {k: (len(v) if isinstance(v, list) else v) for k, v in self.vars.items()}
        out = {"state": self.state, "states": list(self.m["states"]), "vars": vars_view}
        if self.last_error:
            out["last_error"] = self.last_error
        return out


class _Run:
    """One event being handled: the current message and the action interpreter."""

    def __init__(self, machine: Machine, msg) -> None:
        self.mc = machine
        self.ctx = machine.ctx
        self.msg = msg
        self.consumed = msg is None
        self.transitions = 0

    # expression environment -----------------------------------------------------

    def env(self) -> dict[str, Any]:
        params = {k: v for k, v in self.ctx.params.items() if k != "machine"}
        env = {**self.ctx.globals.consts, **params, **self.mc.vars, "now": self.ctx.now,
               "state": self.mc.state}  # fmt: skip
        if self.msg is not None:
            m = self.msg
            env["msg"] = {**m.payload, "_age": self.ctx.now - m.created_at, "_hops": m.hops,
                          "_size": m.size}  # fmt: skip
        return env

    def eval(self, e):
        rng = self.ctx.rng
        funcs = {
            "random": rng.random, "uniform": rng.uniform, "sample": lambda d: sample(d, rng),
            "exponential": lambda mean: rng.expovariate(1.0 / mean),
            "len": len, "min": min, "max": max, "abs": abs, "round": round,
            "int": int, "float": float,
        }  # fmt: skip
        return evaluate(e, self.env(), self.ctx.globals.bind(funcs))

    # actions -------------------------------------------------------------------

    def actions(self, acts: list) -> None:
        try:
            for a in acts:
                verb = next(k for k in a if k in VERBS)
                getattr(self, f"_{verb}")(a)
        except _Goto as g:
            self.transition(g.state)

    def transition(self, target: str) -> None:
        self.transitions += 1
        if self.transitions > MAX_TRANSITIONS:
            raise MachineError(f"more than {MAX_TRANSITIONS} transitions in one event (goto loop?)")
        mc, now = self.mc, self.ctx.now
        self.actions((mc._state().get("exit")) or [])
        self.ctx.gauge(f"in_{mc.state}").set(0.0, now)
        mc.state, mc.generation = target, mc.generation + 1
        self.ctx.gauge(f"in_{target}").set(1.0, now)
        self.ctx.counter("transitions").add()
        self.actions(mc._state().get("enter") or [])

    def settle(self) -> None:
        if not self.consumed:
            self.ctx.counter("absorbed").add()
            self.ctx.drop(self.msg, "absorbed")
            self.consumed = True

    def take(self):
        """The current message, now owned by whoever takes it."""
        if self.msg is None or self.consumed:
            return None
        self.consumed = True
        return self.msg

    def _send(self, a):
        msg = self.take() or self.ctx.message()
        self.ctx.send(a["send"], msg)

    def _emit(self, a):
        spec = a["emit"] if isinstance(a["emit"], dict) else {"port": a["emit"]}
        payload = {k: self.eval(v) for k, v in (spec.get("payload") or {}).items()}
        size = float(self.eval(spec["size"])) if "size" in spec else 1.0
        self.ctx.send(spec["port"], self.ctx.message(payload, size=size))

    def _drop(self, a):
        msg = self.take()
        if msg is not None:
            self.ctx.drop(msg, str(a["drop"]))

    def _set(self, a):
        values = {k: self.eval(v) for k, v in a["set"].items()}  # all read the old values
        self.mc.vars.update(values)

    def _goto(self, a):
        raise _Goto(a["goto"])

    def _after(self, a):
        delay = float(self.eval(a["after"]))
        msg = self.take()
        data = (self.mc.generation, a["event"], msg, bool(a.get("keep")))
        self.ctx.schedule(max(0.0, delay), a["event"], data)

    def _if(self, a):
        self.actions(a.get("then", []) if self.eval(a["if"]) else a.get("else", []))

    def _count(self, a):
        self.ctx.counter(str(a["count"])).add()

    def _observe(self, a):
        for name, v in a["observe"].items():
            self.ctx.samples(name).add(float(self.eval(v)))

    def _push(self, a):
        msg = self.take()
        if msg is not None:
            self.mc.vars.setdefault(a["push"], []).append(msg)

    def _pop(self, a):
        queue = self.mc.vars.get(a["pop"]) or []
        if not queue:
            raise MachineError(f"pop from empty {a['pop']!r}")
        self.settle()  # whatever we were holding is done
        self.msg, self.consumed = queue.pop(0), False

    def _release(self, a):
        spec = a["release"]
        queue = self.mc.vars.get(spec["from"]) or []
        self.mc.vars[spec["from"]] = []
        for msg in queue:
            self.ctx.send(spec["to"], msg)


def _copy(v: Any) -> Any:
    return list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v
