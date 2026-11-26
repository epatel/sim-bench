"""Machine scripts: parsing into machine data, formatting back, and errors."""

import json
from pathlib import Path

import pytest

from server.kernel import Library, ModelSpec, Simulation
from server.kernel.machine import MachineError, check_machine
from server.kernel.machine_script import ScriptError, format_script, parse_script

ROOT = Path(__file__).resolve().parent.parent
LIB = Library.load(ROOT / "libraries")
SHIPPED = ["web/circuit-breaker", "queueing/batcher", "ecology/rabbits", "ecology/foxes"]


def test_a_script_becomes_machine_data():
    m = parse_script("""
inputs in, failure
outputs out, rejected
var failures = 0
var seen = []

on failure: pass        # absorbed everywhere

state closed:
  enter:
    failures = 0
    after window -> tick
  on in: send out
  on failure:
    failures += 1
    if failures >= threshold:
      goto open
state open
""")
    assert m == {
        "inputs": ["in", "failure"],
        "outputs": ["out", "rejected"],
        "vars": {"failures": 0, "seen": []},
        "on": {"failure": []},
        "initial": "closed",
        "states": {
            "closed": {
                "enter": [{"set": {"failures": "0"}}, {"after": "window", "event": "tick"}],
                "on": {
                    "in": [{"send": "out"}],
                    "failure": [
                        {"set": {"failures": "failures + (1)"}},
                        {"if": "failures >= threshold", "then": [{"goto": "open"}]},
                    ],
                },
            },
            "open": {},
        },
    }


@pytest.mark.parametrize(
    ("line", "action"),
    [
        ("x = y * 2", {"set": {"x": "y * 2"}}),
        ("x -= 1", {"set": {"x": "x - (1)"}}),
        ("a, b = b, a", {"set": {"a": "b", "b": "a"}}),
        ("send out", {"send": "out"}),
        ("emit out", {"emit": {"port": "out"}}),
        ('emit out {n: n + 1, "kind": "x"} size 2 * n',
         {"emit": {"port": "out", "payload": {"n": "n + 1", "kind": '"x"'}, "size": "2 * n"}}),
        ("drop too old", {"drop": "too old"}),
        ('drop "full # really"', {"drop": "full # really"}),
        ("after exponential(1 / r) -> fire", {"after": "exponential(1 / r)", "event": "fire"}),
        ("after 5 -> report keep", {"after": "5", "event": "report", "keep": True}),
        ("count hits", {"count": "hits"}),
        ("observe size", {"observe": {"size": "size"}}),
        ("observe wait = now - msg.t", {"observe": {"wait": "now - msg.t"}}),
        ("push q", {"push": "q"}),
        ("pop q", {"pop": "q"}),
        ("release q -> out", {"release": {"from": "q", "to": "out"}}),
    ],
)  # fmt: skip
def test_statements(line, action):
    m = parse_script(f"state s:\n  on in:\n    {line}\n")
    assert m["states"]["s"]["on"]["in"] == [action]


def test_elif_nests_in_else_and_pass_is_nothing():
    m = parse_script("""
state s:
  on in:
    if x > 2:
      send out
    elif x > 1:
      pass
    else:
      drop small
    count seen
""")
    assert m["states"]["s"]["on"]["in"] == [
        {"if": "x > 2", "then": [{"send": "out"}],
         "else": [{"if": "x > 1", "then": [], "else": [{"drop": "small"}]}]},
        {"count": "seen"},
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("state s:\n  on in:\n    frobnicate\n", "line 3: don't know how to read 'frobnicate'"),
        ("state s:\n  on in:\n    x = (1 +\n", "line 3: bad expression"),
        ("state s:\n  on in:\n    x = [i for i in y]\n", "line 3: unsupported expression"),
        ("state s:\n  on in:\n    else:\n      pass\n", "line 3: 'else' without an 'if'"),
        ("state s\nstate s\n", "line 2: state 's' is defined twice"),
        ("var x = y + 1\nstate s\n", "line 1: a variable starts as a literal"),
        ("state s:\n  in: send out\n", "line 2: inside a state, expected enter:"),
        ("state s:\n  on in:\n    after 3 tick\n", "line 3: expected 'after <expr> -> <event>'"),
        ("state s:\n  on in:\n    emit out {1: 2}\n", "line 3: payload keys are names"),
    ],
)
def test_errors_name_the_line(script, message):
    with pytest.raises(ScriptError, match=f"^{message}"):
        parse_script(script)


def test_check_machine_reads_scripts_and_reports_their_errors():
    assert check_machine("state a:\n  on in: goto a\n")["initial"] == "a"
    with pytest.raises(MachineError, match="goto 'b', which is not a state"):
        check_machine("state a:\n  on in: goto b\n")
    with pytest.raises(MachineError, match="line 1"):
        check_machine("stat a\n")


@pytest.mark.parametrize("tid", SHIPPED)
def test_shipped_machines_are_scripts_that_format_back_to_the_same_data(tid):
    script = LIB.resolve(tid).defaults["machine"]
    assert isinstance(script, str)
    assert parse_script(format_script(parse_script(script))) == parse_script(script)


def run(machine):
    model = ModelSpec.model_validate({
        "entities": [
            {"id": "src", "type": "source", "params": {"interarrival": {"dist": "exp", "mean": 1}}},
            {"id": "m", "type": "machine", "params": {"machine": machine}},
            {"id": "out", "type": "sink"},
        ],
        "connections": [{"from": "src.out", "to": "m.in"}, {"from": "m.out", "to": "out.in"}],
    })  # fmt: skip
    sim = Simulation(model, seed=2, library=LIB)
    sim.run(until=50)
    return json.dumps(sim.summary()["entities"], sort_keys=True)


def test_a_formatted_mapping_runs_exactly_like_the_mapping():
    mapping = {
        "vars": {"n": 0, "q": []},
        "initial": "idle",
        "states": {
            "idle": {"on": {"in": [{"set": {"n": "n + 1"}}, {"push": "q"},
                                   {"if": "n % 3 == 0", "then": [{"goto": "flush"}]}]}},
            "flush": {"enter": [{"observe": {"batch": "len(q)"}}, {"after": 0.5, "event": "go"}],
                      "on": {"in": [{"push": "q"}], "go": [{"release": {"from": "q", "to": "out"}},
                                                         {"goto": "idle"}]}},
        },
    }  # fmt: skip
    assert run(format_script(mapping)) == run(mapping)
