from pathlib import Path

import pytest
import yaml

from server.kernel import Library, LibraryError, ModelError, ModelSpec, Simulation, load_model
from server.kernel.expr import ExprError, evaluate

ROOT = Path(__file__).resolve().parent.parent
LIB = Library.load(ROOT / "libraries")


def run(entities, connections, until=None, lib=LIB):
    model = ModelSpec.model_validate({"entities": entities, "connections": connections})
    sim = Simulation(model, seed=1, library=lib)
    sim.run(until=until)
    return sim, sim.summary()["entities"]


def machine_pipeline(machine, count=5, gap=1.0, outputs=("out",)):
    """source -> machine -> one sink per output."""
    ents = [
        {"id": "src", "type": "source", "params": {"interarrival": gap, "count": count}},
        {"id": "m", "type": "machine", "params": {"machine": machine}},
    ]
    conns = [{"from": "src.out", "to": "m.in"}]
    for o in outputs:
        ents.append({"id": f"sink_{o}", "type": "sink"})
        conns.append({"from": f"m.{o}", "to": f"sink_{o}.in"})
    return ents, conns


# --- expressions -------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr, env, want",
    [
        ("a + b * 2", {"a": 1, "b": 3}, 7),
        ("x >= 3 and not y", {"x": 3, "y": False}, True),
        ("'big' if n > 10 else 'small'", {"n": 4}, "small"),
        ("msg.kind == 'read'", {"msg": {"kind": "read"}}, True),
        ("msg['size'] // 2", {"msg": {"size": 7}}, 3),
        ("3 in xs", {"xs": [1, 2, 3]}, True),
        ("1 < x < 5", {"x": 3}, True),
    ],
)
def test_expressions(expr, env, want):
    assert evaluate(expr, env) == want


@pytest.mark.parametrize(
    "expr, message",
    [
        ("__import__('os')", "unsupported"),
        ("(lambda: 1)()", "unsupported"),
        ("x.__class__", "no field"),
        ("[i for i in xs]", "unsupported"),
        ("nope + 1", "unknown name"),
        ("1 / 0", "ZeroDivisionError"),
    ],
)
def test_expressions_reject_unsafe_or_broken_input(expr, message):
    with pytest.raises(ExprError, match=message):
        evaluate(expr, {"x": {}, "xs": [1]})


def test_only_given_functions_can_be_called():
    assert evaluate("max(a, 2)", {"a": 1}, {"max": max}) == 2
    with pytest.raises(ExprError, match="call"):
        evaluate("max(a, 2)", {"a": 1})


# --- state machines ------------------------------------------------------------------


def test_default_machine_forwards():
    _, e = run(*machine_pipeline(None))
    assert e["sink_out"]["received"]["value"] == 5
    assert e["m"]["state"] == "idle"


def test_routing_with_variables_and_conditions():
    machine = {
        "outputs": ["even", "odd"],
        "vars": {"n": 0},
        "initial": "s",
        "states": {
            "s": {
                "on": {
                    "in": [
                        {"set": {"n": "n + 1"}},
                        {"if": "n % 2 == 0", "then": [{"send": "even"}], "else": [{"send": "odd"}]},
                    ]
                }
            }
        },
    }
    _, e = run(*machine_pipeline(machine, count=7, outputs=("even", "odd")))
    assert e["sink_even"]["received"]["value"] == 3
    assert e["sink_odd"]["received"]["value"] == 4
    assert e["m"]["vars"] == {"n": 7}


def test_emit_builds_new_messages_with_payload():
    machine = {
        "initial": "s",
        "states": {
            "s": {
                "on": {
                    "in": [
                        {"emit": {"port": "out", "payload": {"double": "msg._hops * 2"}}},
                        {"drop": "replaced"},
                    ]
                }
            }
        },
    }
    sim, e = run(*machine_pipeline(machine, count=3))
    assert e["sink_out"]["received"]["value"] == 3


def test_timers_hold_messages_and_are_cancelled_on_leaving_the_state():
    # hold each message 0.5 in "busy"; a message arriving while busy aborts the hold.
    # The timer starts in busy.enter: a timer belongs to the state that set it.
    machine = {
        "initial": "idle",
        "states": {
            "idle": {"on": {"in": [{"goto": "busy"}]}},
            "busy": {
                "enter": [{"after": 0.5, "event": "done"}],
                "on": {
                    "done": [{"send": "out"}, {"goto": "idle"}],
                    "in": [{"drop": "busy"}, {"goto": "idle"}],
                },
            },
        },
    }
    _, e = run(*machine_pipeline(machine, count=4, gap=1.0))  # slow: every hold completes
    assert e["sink_out"]["latency"]["mean"] == pytest.approx(0.5)
    assert e["sink_out"]["received"]["value"] == 4

    sim, e = run(*machine_pipeline(machine, count=4, gap=0.2))  # fast: holds get cancelled
    assert e["sink_out"]["received"]["value"] == 0  # 1st held msg cancelled, 2nd dropped, ...
    assert e["m"]["transitions"]["value"] == 4


def test_state_occupancy_is_a_time_weighted_gauge():
    machine = {
        "inputs": [],
        "initial": "a",
        "states": {
            "a": {"enter": [{"after": 1, "event": "t"}], "on": {"t": [{"goto": "b"}]}},
            "b": {"enter": [{"after": 3, "event": "t"}], "on": {"t": [{"goto": "a"}]}},
        },
    }
    sim, e = run([{"id": "m", "type": "machine", "params": {"machine": machine}}], [], until=400)
    assert e["m"]["in_a"]["avg"] == pytest.approx(0.25, abs=0.01)


def test_runtime_errors_are_reported_and_the_run_continues():
    machine = {
        "initial": "s",
        "states": {"s": {"on": {"in": [{"set": {"x": "msg.missing"}}, {"send": "out"}]}}},
    }
    _, e = run(*machine_pipeline(machine, count=3))
    assert e["m"]["errors"]["value"] == 3
    assert "no field 'missing'" in e["m"]["last_error"]
    assert e["sink_out"]["received"]["value"] == 0


def test_goto_loops_are_stopped():
    machine = {
        "initial": "a",
        "states": {
            "a": {"on": {"in": [{"goto": "b"}]}},
            "b": {"enter": [{"goto": "a"}]},
            # a has no enter, so a -> b -> a ... loops only through b.enter
        },
    }
    machine["states"]["a"]["enter"] = [{"goto": "b"}]
    _, e = run(*machine_pipeline(machine, count=1))
    assert e["m"]["errors"]["value"] >= 1
    assert "goto loop" in e["m"]["last_error"]


@pytest.mark.parametrize(
    "machine, message",
    [
        ({"initial": "x", "states": {"a": {}}}, "initial state"),
        ({"initial": "a", "states": {"a": {"on": {"in": [{"goto": "zz"}]}}}}, "not a state"),
        ({"initial": "a", "states": {"a": {"on": {"in": [{"send": "nope"}]}}}}, "not an output"),
        ({"initial": "a", "states": {"a": {"on": {"in": [{"fly": 1}]}}}}, "exactly one of"),
        ({"initial": "a", "states": {"a": {"on": {"in": [{"set": {"x": "1 +"}}]}}}}, "bad expr"),
        ({"initial": "a", "states": {"a": {"enter": [{"after": 1}]}}}, "needs an 'event'"),
    ],
)
def test_invalid_machines_are_model_errors(machine, message):
    with pytest.raises(ModelError, match=message):
        run(*machine_pipeline(machine))


def test_yaml_on_key_works_unquoted():
    machine = yaml.safe_load(
        """
        initial: s
        states:
          s:
            on:
              in: [{send: out}]
        """
    )
    assert True in machine["states"]["s"]  # what PyYAML does with a bare `on:`
    _, e = run(*machine_pipeline(machine, count=2))
    assert e["sink_out"]["received"]["value"] == 2


# --- shipped machine types -------------------------------------------------------------


def test_batcher_releases_full_batches_and_flushes_on_timeout():
    ents = [
        {"id": "src", "type": "source", "params": {"interarrival": 0.1, "count": 25}},
        {"id": "b", "type": "queueing/batcher", "params": {"size": 10, "timeout": 5.0}},
        {"id": "out", "type": "sink"},
    ]
    conns = [{"from": "src.out", "to": "b.in"}, {"from": "b.out", "to": "out.in"}]
    _, e = run(ents, conns)
    sizes = e["b"]["batch_size"]
    assert sizes["count"] == 3 and sizes["max"] == 10 and sizes["min"] == 5  # 10, 10, then 5
    assert e["out"]["received"]["value"] == 25


def test_circuit_breaker_opens_on_failures_and_recovers():
    sim = Simulation(load_model(ROOT / "projects/examples/resilience.yaml"), seed=1, library=LIB)
    sim.run(until=200)
    e = sim.summary()["entities"]
    b = e["breaker"]
    assert b["opened"]["value"] > 5
    assert 0.1 < b["in_open"]["avg"] < 0.9
    assert b["unhandled"]["value"] == 0 and b["errors"]["value"] == 0
    assert e["shed"]["received"]["value"] > 0  # rejected while open
    # the token bucket held throughput near its rate of 20 per time unit
    assert e["limiter"]["passed"]["value"] / 200 == pytest.approx(20, rel=0.05)


def test_machines_and_python_behaviors_survive_fork():
    sim = Simulation(load_model(ROOT / "projects/examples/resilience.yaml"), seed=2, library=LIB)
    sim.run(until=30)
    fork = sim.fork()
    sim.run(until=80)
    fork.run(until=80)
    assert fork.summary() == sim.summary()


# --- Python behaviors ---------------------------------------------------------------------


def test_python_behavior_is_registered_under_its_library(tmp_path):
    types = {t: LIB.resolve(t) for t in LIB.all_type_ids()}
    tb = types["web/token-bucket"]
    assert tb.library == "web" and tb.chain == ["web/token-bucket"]
    assert tb.defaults == {"rate": 10.0, "burst": 20.0}


def behavior_code(cls: str, kind: str, body: str, schema: str = "") -> str:
    """Source for a one-port-in, one-port-out Python behavior."""
    return (
        "from server.kernel import Behavior\n"
        f"class {cls}(Behavior):\n"
        f"    kind = '{kind}'\n"
        f"{schema}"
        "    @classmethod\n"
        "    def ports(cls, params):\n"
        "        return ['in'], ['out']\n"
        "    def on_message(self, port, msg):\n"
        f"{body}"
    )


def through(item_type: str, count: int):
    """source -> one entity "e" of item_type -> sink "out"."""
    ents = [
        {"id": "src", "type": "source", "params": {"count": count}},
        {"id": "e", "type": item_type},
        {"id": "out", "type": "sink"},
    ]
    conns = [{"from": "src.out", "to": "e.in"}, {"from": "e.out", "to": "out.in"}]
    return ents, conns


def write_lib(tmp_path, code):
    (tmp_path / "t" / "behaviors").mkdir(parents=True)
    (tmp_path / "t" / "library.yaml").write_text("name: t\n")
    (tmp_path / "t" / "behaviors" / "b.py").write_text(code)
    return tmp_path


def test_broken_python_behavior_is_a_library_error(tmp_path):
    root = write_lib(tmp_path, "raise RuntimeError('boom')\n")
    with pytest.raises(LibraryError, match="RuntimeError: boom"):
        Library.load(root)


def test_python_behavior_can_be_extended_by_a_yaml_type(tmp_path):
    root = write_lib(
        tmp_path,
        behavior_code(
            "Echo",
            "echo",
            "        self.ctx.counter('seen').add(self.ctx.params['times'])\n"
            "        self.ctx.send('out', msg)\n",
            schema="    params_schema = {'times': {'type': 'int', 'default': 1}}\n",
        ),
    )
    (root / "t" / "types").mkdir()
    (root / "t" / "types" / "loud.yaml").write_text("extends: echo\nparams: {times: 3}\n")
    _, e = run(*through("t/loud", count=2), lib=Library.load(root))
    assert e["e"]["seen"]["value"] == 6 and e["out"]["received"]["value"] == 2


def test_model_and_library_yaml_keep_on_as_a_key():
    from server.kernel.yamlio import load_yaml

    assert load_yaml("on: 1\nyes: no\nt: true") == {"on": 1, "yes": "no", "t": True}
    m = load_yaml("initial: a\non: {y: []}\nstates: {a: {on: {x: []}}}")  # a machine mapping
    assert "on" in m and True not in m and "on" in m["states"]["a"]


def test_fork_survives_a_reloaded_behavior_file(tmp_path):
    root = write_lib(tmp_path, behavior_code("Pass", "pass", "        self.ctx.send('out', msg)\n"))
    sim, _ = run(*through("t/pass", count=3), until=1, lib=Library.load(root))
    path = root / "t" / "behaviors" / "b.py"
    path.write_text(path.read_text() + "\n# edited\n")
    import os

    os.utime(path, ns=(1, 2))  # a different mtime -> a new module version
    Library.load(root)
    sim.fork()  # would raise PicklingError if the old class were shadowed
