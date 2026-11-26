"""Visuals: scripts that compute what the Visual canvas shows from a run summary."""

from pathlib import Path

import pytest

from server.kernel import Library, ModelError, ModelSpec, Simulation
from server.kernel.machine_script import ScriptError
from server.kernel.visuals import parse_visual, run_visuals
from server.kernel.yamlio import load_yaml

ROOT = Path(__file__).resolve().parent.parent
LIB = Library.load(ROOT / "libraries")


def web_backend(visuals, globals_=None):
    data = load_yaml((ROOT / "projects/examples/web-backend.yaml").read_text())
    data["visuals"] = visuals
    if globals_:
        data["globals"] = globals_
    return ModelSpec.model_validate(data)


def run(model, until=50):
    sim = Simulation(model, seed=1, library=LIB)
    sim.run(until=until)
    s = sim.summary()
    return s, run_visuals(model.visuals, s, sim.globals)


def test_metrics_by_name_full_summary_other_entities_and_globals():
    code = """
load = busy + queue              # gauges: their current value
output = completed               # a counter: its value
title = "db " + str(round(m.residence.p95, 3))
max = limit
unit = "jobs"
"""
    model = web_backend(
        [{"id": "db_done", "entity": "db", "code": code, "widget": "bar"},
         {"id": "clock", "code": "output = round(now)\nother = entities.app1.completed.value"}],
        globals_="limit = 5000\n",
    )  # fmt: skip
    s, out = run(model)
    db = s["entities"]["db"]
    assert out["db_done"] == {
        "output": db["completed"]["value"], "title": f"db {round(db['residence']['p95'], 3)}",
        "unit": "jobs", "max": 5000,
    }  # fmt: skip
    assert out["clock"] == {"output": 50}


def test_runtime_errors_are_reported_per_visual():
    model = web_backend([
        {"id": "bad", "entity": "db", "code": "output = nosuch + 1"},
        {"id": "ok", "entity": "db", "code": "output = 1"},
    ])  # fmt: skip
    _, out = run(model)
    assert "unknown name 'nosuch'" in out["bad"]["error"]
    assert out["ok"] == {"output": 1}


@pytest.mark.parametrize(
    ("visuals", "message"),
    [
        ([{"id": "v", "code": "x = 1"}], "visuals.v: set output"),
        ([{"id": "v", "code": "output == 1"}], "visuals.v: line 1: expected 'name = <expr>'"),
        ([{"id": "v", "entity": "nope", "code": "output = 1"}], "visuals.v: no entity 'nope'"),
        ([{"id": "v", "code": "output = 1"}] * 2, "visuals: id 'v' is used twice"),
    ],
)
def test_bad_visuals_make_the_model_invalid(visuals, message):
    with pytest.raises(ModelError, match=f"^{message}"):
        Simulation(web_backend(visuals), library=LIB)


def test_scripts_parse_into_steps():
    assert parse_visual("a = 1  # one\noutput = a * 2\n").steps == (("a", "1"), ("output", "a * 2"))
    with pytest.raises(ScriptError):
        parse_visual("output = (\n")


def test_visuals_save_with_the_model():
    from server.app import dump_model

    model = web_backend([{"id": "q", "entity": "db", "widget": "bar", "code": "output = queue\n",
                          "pos": [32, 48]}])  # fmt: skip
    again = ModelSpec.model_validate(load_yaml(dump_model(model)))
    assert again.visuals == model.visuals
    assert "visuals" not in dump_model(web_backend([]))
