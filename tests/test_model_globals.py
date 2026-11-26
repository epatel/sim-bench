"""Model globals: constants and functions shared by every machine in a model."""

from pathlib import Path

import pytest

from server.kernel import Library, ModelError, ModelSpec, Simulation
from server.kernel.expr import ExprError
from server.kernel.machine_script import ScriptError
from server.kernel.model_globals import PURE, parse_globals

ROOT = Path(__file__).resolve().parent.parent
LIB = Library.load(ROOT / "libraries")


def test_constants_are_computed_in_order_with_functions_and_pure_builtins():
    g = parse_globals("""
meadow = 2000
half = scale(meadow) / 2        # a function defined further down is fine
top = max(half, 3)

def scale(k): return k / 1000
def logistic(n, cap):
  return n * (1 - n / cap)
""")
    assert g.consts == {"meadow": 2000, "half": 1.0, "top": 3}
    assert g.bind(PURE)["logistic"](500, 1000) == 250


def test_functions_see_constants_and_may_recurse_within_a_limit():
    g = parse_globals("base = 1\ndef fact(n): return base if n <= 1 else n * fact(n - 1)\n")
    assert g.bind(PURE)["fact"](5) == 120
    with pytest.raises(ExprError, match="nested more than 50 deep"):
        g.bind(PURE)["fact"](80)
    with pytest.raises(ExprError, match="takes 1 arguments, got 2"):
        g.bind(PURE)["fact"](1, 2)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("x == 1\n", "line 1: expected 'name = <expr>'"),
        ("def f(x):\n  y = x\n", "line 1: def f: the body is one 'return <expr>' line"),
        ("def f(x, x): return x\n", "line 1: def f: an argument name is repeated"),
        ("a = 1\na = 2\n", "line 2: constant 'a' is defined twice"),
        ("a = b + 1\n", "line 1: a: unknown name 'b'"),
        ("a = random()\n", r"line 1: a: unsupported expression \(call\)"),
        ("def f(x): return (x\n", "line 1: bad expression"),
    ],
)
def test_errors_name_the_line(source, message):
    with pytest.raises(ScriptError, match=f"^{message}"):
        parse_globals(source)


MACHINE = """
var n = 0
state s:
  on in:
    n += step(capacity)
    observe n
    observe noisy = jitter(1)
"""


def model(globals_, params=None):
    return ModelSpec.model_validate({
        "globals": globals_,
        "entities": [
            {"id": "src", "type": "source", "params": {"interarrival": 1, "count": 4}},
            {"id": "m", "type": "machine",
             "params": {"machine": MACHINE, **(params or {})}},
        ],
        "connections": [{"from": "src.out", "to": "m.in"}],
    })  # fmt: skip


GLOBALS = """
capacity = 10                  # a param of the same name wins inside the machine
def step(cap): return cap / 10
def jitter(x): return x * random()
"""


def test_machines_use_the_globals_and_their_params_shadow_constants():
    sim = Simulation(model(GLOBALS), seed=1, library=LIB)
    sim.run()
    s = sim.summary()["entities"]["m"]
    assert s["vars"]["n"] == 4  # 4 messages, step(10) = 1 each
    assert 0 <= s["noisy"]["mean"] <= 1

    sim = Simulation(model(GLOBALS, {"capacity": 20}), seed=1, library=LIB)
    sim.run()
    assert sim.summary()["entities"]["m"]["vars"]["n"] == 8


def test_random_in_a_function_uses_the_entity_stream_so_runs_repeat_and_fork():
    def noisy(sim):
        return sim.summary()["entities"]["m"]["noisy"]["mean"]

    a = Simulation(model(GLOBALS), seed=3, library=LIB)
    b = Simulation(model(GLOBALS), seed=3, library=LIB)
    a.run(until=2)
    fork = a.fork()
    a.run()
    b.run()
    fork.run()
    assert noisy(a) == noisy(b) == noisy(fork)


def test_bad_globals_make_the_model_invalid():
    with pytest.raises(ModelError, match="globals: line 2"):
        Simulation(model("a = 1\nb = = 2\n"), library=LIB)
