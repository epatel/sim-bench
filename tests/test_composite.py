from pathlib import Path

import pytest

from server.kernel import Library, ModelError, ModelSpec, Simulation, flatten, suggest_abstract

LIB = Library.load(Path(__file__).resolve().parent.parent / "libraries")


def pool_model(mode="expanded", abstract=None, entry="p_lb.in"):
    """source -> lb-pool(n mm-servers) -> sink, with the pool as group 'p'."""
    frag = LIB.instantiate("web/lb-pool", {"n": 2, "server": "queueing/mm-server"}, prefix="p")
    for e in frag["entities"]:
        if e["type"] == "queueing/mm-server":
            e["params"] = {"service": {"dist": "exp", "mean": 1.5}}
    group = {"id": "p", "inputs": {"in": "p_lb.in"}, "outputs": {"out": "p_join.out"}, "mode": mode}
    if abstract:
        group["abstract"] = abstract
    return ModelSpec.model_validate(
        {
            "entities": [
                {"id": "src", "type": "queueing/poisson-source"},
                *frag["entities"],
                {"id": "out", "type": "sink"},
            ],
            "connections": [
                {"from": "src.out", "to": entry},
                *frag["connections"],
                {"from": "p_join.out", "to": "out.in"},
            ],
            "groups": [group],
        }
    )


STAND_IN = {"type": "delay", "params": {"delay": 2.0}}


def test_expanded_group_is_probed_at_its_boundary():
    sim = Simulation(pool_model(), seed=1, library=LIB)
    sim.run(until=5000)
    s = sim.summary()
    g = s["groups"]["p"]
    assert g["entered"] == g["exited"] + g["inside"] and g["dropped"] == 0
    # nothing outside the group adds latency, so residence == end-to-end latency
    assert g["residence"]["mean"] == pytest.approx(s["entities"]["out"]["latency"]["mean"])
    assert g["arrival_rate"] == pytest.approx(1.0, rel=0.05)


def test_abstract_group_replaces_members_and_keeps_link_keys():
    model = pool_model(mode="abstract", abstract=STAND_IN)
    flat, _ = flatten(model)
    ids = [e.id for e in flat.entities]
    assert ids == ["src", "out", "p"]
    keys = {c.key(): (c.source, c.target) for c in flat.connections}
    assert keys == {
        "src.out->p_lb.in": ("src.out", "p.in"),
        "p_join.out->out.in": ("p.out", "out.in"),
    }

    sim = Simulation(model, seed=1, library=LIB)
    sim.run(until=200)
    s = sim.summary()
    assert s["entities"]["p"]["type"] == "delay"
    assert s["entities"]["out"]["latency"]["mean"] == pytest.approx(2.0)
    assert s["links"]["src.out->p_lb.in"]["sent"]["value"] > 100
    assert s["groups"] == {}  # abstract groups have no probe; their stand-in has metrics


@pytest.mark.parametrize("mode", ["expanded", "abstract"])
def test_outside_connections_may_name_the_boundary_port(mode):
    model = pool_model(mode=mode, abstract=STAND_IN, entry="p.in")
    sim = Simulation(model, seed=1, library=LIB)
    sim.run(until=100)
    s = sim.summary()
    assert s["entities"]["out"]["received"]["value"] > 50
    assert s["links"]["src.out->p.in"]["sent"]["value"] > 50


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda m: m.groups[0].__setattr__("mode", "abstract"), "no abstract behavior"),
        (lambda m: m.groups[0].inputs.update({"x": "src.out"}), "not a member"),
        (lambda m: m.groups[0].__setattr__("id", "src"), "same id as an entity"),
    ],
)
def test_bad_groups_are_model_errors(change, message):
    model = pool_model()
    change(model)
    with pytest.raises(ModelError, match=message):
        Simulation(model, library=LIB)


def test_abstract_group_cannot_be_entered_through_an_inner_port():
    model = pool_model(mode="abstract", abstract=STAND_IN, entry="p_s1.in")
    with pytest.raises(ModelError, match="not a boundary port"):
        Simulation(model, library=LIB)


def test_calibrated_stand_in_reproduces_the_expanded_latency():
    expanded = Simulation(pool_model(), seed=3, library=LIB)
    expanded.run(until=20_000)
    s = expanded.summary()
    abstract = suggest_abstract(s["groups"]["p"], s["time"], seed=3)
    assert abstract["type"] == "delay" and abstract["calibration"]["samples"] > 10_000

    coarse = Simulation(pool_model(mode="abstract", abstract=abstract), seed=4, library=LIB)
    coarse.run(until=20_000)
    want = s["entities"]["out"]["latency"]["mean"]
    got = coarse.summary()["entities"]["out"]["latency"]["mean"]
    assert got == pytest.approx(want, rel=0.05)
    # and the abstract run is cheaper (savings grow with the size of the group)
    assert coarse.events_processed < expanded.events_processed * 0.7


def test_probe_measures_loss_inside_the_group():
    model = pool_model()
    for e in model.entities:
        if e.id == "p_join":
            e.params = {"error_rate": 0.2}
    sim = Simulation(model, seed=5, library=LIB)
    sim.run(until=10_000)
    g = sim.summary()["groups"]["p"]
    assert g["loss"] == pytest.approx(0.2, abs=0.02)
    assert suggest_abstract(g, 10_000, 5)["params"]["error_rate"] == pytest.approx(0.2, abs=0.02)


def test_fork_carries_probe_state():
    sim = Simulation(pool_model(), seed=6, library=LIB)
    sim.run(until=300)
    fork = sim.fork()
    sim.run(until=600)
    fork.run(until=600)
    assert fork.summary() == sim.summary()


def test_suggest_abstract_needs_traffic():
    sim = Simulation(pool_model(), seed=1, library=LIB)
    with pytest.raises(ValueError, match="no message crossed"):
        suggest_abstract(sim.summary()["groups"]["p"], 0, 1)
