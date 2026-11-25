from pathlib import Path

import pytest

from server.kernel import (
    Library,
    Measurement,
    ModelError,
    ModelSpec,
    Simulation,
    flatten,
    run_calibration,
    suggest_abstract,
)
from server.kernel.calibrate import Request, fit_servers, mmc_residence

LIB = Library.load(Path(__file__).resolve().parent.parent / "libraries")


def pool_model(mode="expanded", abstract=None, entry="p_lb.in", rate=1.0):
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
                {
                    "id": "src",
                    "type": "queueing/poisson-source",
                    "params": {"interarrival": {"dist": "exp", "mean": 1 / rate}},
                },  # fmt: skip
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


def measure(model, until, seed):
    sim = Simulation(model, seed=seed, library=LIB)
    sim.run(until=until)
    s = sim.summary()
    members = {e.id: s["entities"][e.id] for e in model.entities if e.group == "p"}
    return s, Measurement(s["groups"]["p"], s["time"], seed, members)


def sink_rate(model, until, seed):
    sim = Simulation(model, seed=seed, library=LIB)
    sim.run(until=until)
    return sim.summary()["entities"]["out"]["received"]["value"] / until


def test_delay_stand_in_reproduces_the_expanded_latency():
    s, m = measure(pool_model(), 20_000, seed=3)
    abstract = suggest_abstract(m, kind="delay")
    assert abstract["type"] == "delay" and abstract["calibration"]["samples"] > 10_000

    coarse = Simulation(pool_model(mode="abstract", abstract=abstract), seed=4, library=LIB)
    coarse.run(until=20_000)
    want = s["entities"]["out"]["latency"]["mean"]
    got = coarse.summary()["entities"]["out"]["latency"]["mean"]
    assert got == pytest.approx(want, rel=0.05)
    # and the abstract run is cheaper (savings grow with the size of the group)
    assert coarse.events_processed < s["events"] * 0.7


def test_server_stand_in_finds_the_capacity_and_matches_latency():
    # two M/M/1 servers (mean 1.5) behind a random balancer saturate at 2 / 1.5 = 1.33/s
    s, m = measure(pool_model(), 20_000, seed=3)
    abstract = suggest_abstract(m)  # auto: the pool has servers
    cal = abstract["calibration"]
    assert abstract["type"] == "server"
    assert cal["capacity"] == pytest.approx(4 / 3, rel=0.05)
    assert cal["bottleneck"] in ("p_s1", "p_s2")
    servers, service = abstract["params"]["servers"], abstract["params"]["service"]["mean"]
    assert servers / service == pytest.approx(cal["capacity"])
    assert cal["predicted_residence"] == pytest.approx(cal["residence_mean"], rel=0.1)

    coarse = Simulation(pool_model(mode="abstract", abstract=abstract), seed=4, library=LIB)
    coarse.run(until=20_000)
    want = s["entities"]["out"]["latency"]["mean"]
    got = coarse.summary()["entities"]["out"]["latency"]["mean"]
    assert got == pytest.approx(want, rel=0.12)


def test_server_stand_in_saturates_where_the_group_does():
    _, m = measure(pool_model(), 20_000, seed=3)
    server, delay = suggest_abstract(m), suggest_abstract(m, kind="delay")
    overload = 1.8  # well above the pool's capacity of 1.33/s
    real = sink_rate(pool_model(rate=overload), 5000, seed=7)
    as_server = sink_rate(pool_model("abstract", server, rate=overload), 5000, seed=7)
    as_delay = sink_rate(pool_model("abstract", delay, rate=overload), 5000, seed=7)
    assert real < 1.45
    assert as_server == pytest.approx(real, rel=0.08)
    assert as_delay == pytest.approx(overload, rel=0.05)  # never saturates


def test_run_calibration_forces_the_group_expanded():
    model = pool_model(mode="abstract", abstract=STAND_IN)
    sim, m, abstract = run_calibration(model, Request("p", until=3000, seed=2), LIB, 10**6)
    assert m.probe["exited"] > 2000 and set(m.members) >= {"p_s1", "p_s2", "p_lb"}
    assert abstract["type"] == "server" and sim.now == 3000
    with pytest.raises(ValueError, match="no group"):
        run_calibration(model, Request("zzz"), LIB, 1000)


def test_stand_in_kind_falls_back_or_refuses_without_servers():
    s, m = measure(pool_model(), 2000, seed=1)
    no_servers = Measurement(m.probe, m.now, 1, {})
    assert suggest_abstract(no_servers)["type"] == "delay"
    with pytest.raises(ValueError, match="capacity is unknown"):
        suggest_abstract(no_servers, kind="server")
    with pytest.raises(ValueError, match="kind must be"):
        suggest_abstract(m, kind="fast")


def test_mmc_formula_and_server_fit():
    assert mmc_residence(0.5, 1, 1.0) == pytest.approx(2.0)  # M/M/1: 1 / (mu - lambda)
    # M/M/2, lambda 1, service 1.5: Erlang C = 9/14, W = 1.5 + (9/14) * 1.5 / 0.5
    assert mmc_residence(1.0, 2, 1.5) == pytest.approx(1.5 + 9 / 14 * 3)
    assert mmc_residence(2.0, 1, 1.0) == float("inf")
    assert fit_servers(1.0, 4 / 3, 3.0) == 1  # W(1) = 3 exactly
    c = fit_servers(1.0, 4 / 3, 6.0)
    assert abs(mmc_residence(1.0, c, c * 0.75) - 6.0) <= abs(
        mmc_residence(1.0, c + 1, (c + 1) * 0.75) - 6.0
    )


def test_probe_measures_loss_inside_the_group():
    model = pool_model()
    for e in model.entities:
        if e.id == "p_join":
            e.params = {"error_rate": 0.2}
    sim = Simulation(model, seed=5, library=LIB)
    sim.run(until=10_000)
    g = sim.summary()["groups"]["p"]
    assert g["loss"] == pytest.approx(0.2, abs=0.02)
    stand_in = suggest_abstract(Measurement(g, 10_000, 5))
    assert stand_in["params"]["error_rate"] == pytest.approx(0.2, abs=0.02)


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
        suggest_abstract(Measurement(sim.summary()["groups"]["p"], 0, 1))
