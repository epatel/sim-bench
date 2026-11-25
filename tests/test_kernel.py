from pathlib import Path

import pytest

from server.kernel import ModelError, ModelSpec, Simulation, load_model
from server.kernel.__main__ import main as cli_main

EXAMPLES = Path(__file__).resolve().parent.parent / "projects" / "examples"


def model(entities, connections=()):
    return ModelSpec.model_validate({"entities": entities, "connections": list(connections)})


def pipeline(**link):
    return model(
        [
            {"id": "gen", "type": "source", "params": {"interarrival": 1.0, "count": 5}},
            {"id": "done", "type": "sink"},
        ],
        [{"from": "gen.out", "to": "done.in", **link}],
    )


def test_constant_pipeline_is_exact():
    sim = Simulation(pipeline(latency=0.5))
    sim.run()
    s = sim.summary()
    assert s["entities"]["done"]["received"]["value"] == 5
    lat = s["entities"]["done"]["latency"]
    assert lat["min"] == lat["max"] == 0.5
    assert sim.now == 5.5  # last emit at t=5, plus latency


def test_same_seed_same_result_and_different_seed_differs():
    m = load_model(EXAMPLES / "web-backend.yaml")
    a, b, c = Simulation(m, seed=7), Simulation(m, seed=7), Simulation(m, seed=8)
    for s in (a, b, c):
        s.run(until=50)
    assert a.summary() == b.summary()
    assert a.summary() != c.summary()


def test_mm1_matches_queueing_theory():
    sim = Simulation(load_model(EXAMPLES / "mm1.yaml"), seed=1)
    sim.run(until=100_000)
    s = sim.summary()
    w = s["entities"]["done"]["latency"]["mean"]
    util = s["entities"]["srv"]["utilization"]
    assert w == pytest.approx(5.0, rel=0.08)  # W = 1 / (mu - lambda)
    assert util == pytest.approx(0.8, rel=0.03)  # rho = lambda / mu


def test_fork_continues_identically():
    sim = Simulation(load_model(EXAMPLES / "web-backend.yaml"), seed=3)
    sim.run(until=20)
    fork = sim.fork()
    sim.run(until=60)
    fork.run(until=60)
    assert fork.summary() == sim.summary()


def test_fork_is_independent():
    sim = Simulation(load_model(EXAMPLES / "mm1.yaml"), seed=3)
    sim.run(until=10)
    fork = sim.fork()
    fork.run(until=100)
    assert sim.now == 10
    assert fork.now == 100


def test_bandwidth_serializes_transmissions():
    m = model(
        [
            {
                "id": "gen",
                "type": "source",
                "params": {"interarrival": 0.1, "count": 3, "size": 10},
            },
            {"id": "done", "type": "sink"},
        ],
        [{"from": "gen.out", "to": "done.in", "bandwidth": 10}],
    )
    deliveries = []
    sim = Simulation(m)
    sim.add_listener(lambda r: r["type"] == "deliver" and deliveries.append(r["t"]))
    sim.run()
    # each message needs 1.0 time units on the wire; sent at 0.1, 0.2, 0.3
    assert deliveries == pytest.approx([1.1, 2.1, 3.1])


def test_link_down_loses_everything():
    sim = Simulation(pipeline(up=False))
    sim.run()
    s = sim.summary()
    assert s["links"]["gen.out->done.in"]["lost"]["value"] == 5
    assert s["entities"]["done"]["received"]["value"] == 0


def test_server_queue_capacity_drops():
    m = model(
        [
            {"id": "gen", "type": "source", "params": {"interarrival": 0.1, "count": 10}},
            {"id": "srv", "type": "server", "params": {"service": 10, "queue_capacity": 2}},
            {"id": "done", "type": "sink"},
        ],
        [{"from": "gen.out", "to": "srv.in"}, {"from": "srv.out", "to": "done.in"}],
    )
    sim = Simulation(m)
    sim.run()
    srv = sim.summary()["entities"]["srv"]
    assert srv["dropped"]["value"] == 7  # 1 in service + 2 queued
    assert srv["completed"]["value"] == 3


def test_round_robin_router_alternates():
    m = model(
        [
            {"id": "gen", "type": "source", "params": {"interarrival": 1, "count": 6}},
            {
                "id": "lb",
                "type": "router",
                "params": {"mode": "round_robin", "routes": {"a": 1, "b": 1}},
            },
            {"id": "x", "type": "sink"},
            {"id": "y", "type": "sink"},
        ],
        [
            {"from": "gen.out", "to": "lb.in"},
            {"from": "lb.a", "to": "x.in"},
            {"from": "lb.b", "to": "y.in"},
        ],
    )
    sim = Simulation(m)
    sim.run()
    e = sim.summary()["entities"]
    assert e["x"]["received"]["value"] == e["y"]["received"]["value"] == 3


def test_unconnected_output_is_counted():
    sim = Simulation(model([{"id": "gen", "type": "source", "params": {"count": 2}}]))
    sim.run()
    assert sim.summary()["entities"]["gen"]["unrouted"]["value"] == 2


@pytest.mark.parametrize(
    "entities, connections, message",
    [
        ([{"id": "a", "type": "nope"}], [], "unknown type"),
        ([{"id": "a", "type": "sink"}, {"id": "a", "type": "sink"}], [], "duplicate entity"),
        ([{"id": "a", "type": "sink"}], [{"from": "a.out", "to": "a.in"}], "not an output"),
        ([{"id": "a", "type": "source"}], [{"from": "a.out", "to": "b.in"}], "unknown entity"),
        (
            [{"id": "a", "type": "source"}, {"id": "b", "type": "sink"}],
            [{"from": "a.out", "to": "b.in"}, {"from": "a.out", "to": "b.in", "id": "x"}],
            "more than once",
        ),
    ],
)
def test_invalid_models_are_rejected(entities, connections, message):
    with pytest.raises(ModelError, match=message):
        Simulation(model(entities, connections))


def test_summary_before_any_event():
    sim = Simulation(load_model(EXAMPLES / "web-backend.yaml"), seed=1)
    s = sim.summary()
    assert s["time"] == 0 and s["events"] == 0
    assert s["entities"]["app1"]["utilization"] == 0


def test_step_by_step_matches_run():
    m = load_model(EXAMPLES / "mm1.yaml")
    a, b = Simulation(m, seed=5), Simulation(m, seed=5)
    a.run(max_events=500)
    for _ in range(500):
        b.step()
    assert a.summary() == b.summary()


def test_cli_runs_examples(capsys):
    assert cli_main([str(EXAMPLES / "web-backend.yaml"), "--until", "10", "--seed", "1"]) == 0
    out = capsys.readouterr().out
    assert "[entities]" in out and "app1" in out
