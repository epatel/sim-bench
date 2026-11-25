import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from server import app as app_module
from server import storage
from server.kernel import ModelError, ModelSpec, load_model
from server.kernel import scenario as sc
from server.kernel.model import PatchSpec

ROOT = Path(__file__).resolve().parent.parent
WEB = load_model(ROOT / "projects/examples/web-backend.yaml")


def latency(result):
    return result["flat"]["entities.done.latency.mean"]


# --- patches -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"value": 1}, "exactly one of"),
        ({"entity": "a", "link": "x", "param": "p"}, "exactly one of"),
        ({"entity": "a", "value": 1}, "needs 'param'"),
        ({"link": "x", "value": 1}, "needs 'field'"),
        ({"group": "g", "mode": "abstract", "at": 5}, "only change before the run"),
        ({"entity": "a", "param": "p", "at": -1}, ">= 0"),
    ],
)
def test_patch_validation(patch, message):
    with pytest.raises(ValidationError, match=message):
        PatchSpec.model_validate(patch)


def test_static_patches_change_a_copy_of_the_model():
    s = WEB.scenario("db-slow")
    patched = sc.apply_static(WEB, s)
    assert patched.entity("db").params["service"] == {"dist": "exp", "mean": 0.04}
    assert WEB.entity("db").params["service"] == {"dist": "exp", "mean": 0.02}


def test_unknown_targets_and_scenarios_are_model_errors():
    with pytest.raises(ModelError, match="no scenario"):
        sc.build(WEB, "nope", 1)
    bad = WEB.model_copy(deep=True)
    bad.scenarios[0].patches[0].entity = "ghost"
    with pytest.raises(ModelError, match="no entity 'ghost'"):
        sc.build(bad, "db-slow", 1)


def test_scenarios_move_the_metrics_they_should():
    base = sc.run_one(sc.build(WEB, sc.BASELINE, 1), 300, 5)
    slow = sc.run_one(sc.build(WEB, "db-slow", 1), 300, 5)
    assert latency(slow) - latency(base) == pytest.approx(0.02, abs=0.005)  # db mean +20ms


def test_timed_link_failure_only_hits_after_its_time():
    sim = sc.build(WEB, "app1-link-down", 1)
    sim.run(until=99)
    assert sim.summary()["links"]["lb.app_a->app1.in"]["lost"]["value"] == 0
    sim.run(until=200)
    s = sim.summary()
    assert s["links"]["lb.app_a->app1.in"]["lost"]["value"] > 500
    assert s["patches"] == [{"t": 100.0, "patch": "at t=100: link lb.app_a->app1.in up = False"}]


def test_adding_servers_mid_run_drains_the_queue():
    model = ModelSpec.model_validate(
        {
            "entities": [
                {"id": "src", "type": "source", "params": {"interarrival": 0.1, "count": 50}},
                {"id": "srv", "type": "server", "params": {"service": 1.0}},
                {"id": "out", "type": "sink"},
            ],
            "connections": [
                {"from": "src.out", "to": "srv.in"},
                {"from": "srv.out", "to": "out.in"},
            ],
            "scenarios": [
                {
                    "id": "more",
                    "patches": [{"at": 2, "entity": "srv", "param": "servers", "value": 10}],
                }
            ],
        }
    )
    sim = sc.build(model, "more", 1)
    sim.run(until=1.95)  # just before the patch
    queued = sim.summary()["entities"]["srv"]["queue"]["value"]
    sim.run(until=2.0)
    s = sim.summary()["entities"]["srv"]
    assert queued > 10 and s["busy"]["value"] == 10 and s["queue"]["value"] == queued - 9


def test_router_ports_cannot_change_mid_run():
    data = WEB.model_dump(by_alias=True)
    data["scenarios"] = [
        {"id": "x", "patches": [{"at": 1, "entity": "lb", "param": "routes", "value": {"z": 1}}]}
    ]
    model = ModelSpec.model_validate(data)
    sim = sc.build(model, "x", 1)
    with pytest.raises(ValueError, match="cannot change mid-run"):
        sim.run(until=2)


# --- metrics, stats, batches ---------------------------------------------------------


def test_flat_metrics_paths():
    sim = sc.build(WEB, sc.BASELINE, 1)
    sim.run(until=10)
    flat = sc.flat_metrics(sim.summary())
    assert "entities.done.latency.p95" in flat
    assert "entities.app1.utilization" in flat
    assert "links.lb.app_a->app1.in.sent.value" in flat
    assert all(isinstance(v, float) for v in flat.values())


def test_stats_confidence_interval():
    s = sc.stats([1.0, 2.0, 3.0, 4.0])
    assert s["mean"] == 2.5 and s["n"] == 4
    assert s["ci95"] == pytest.approx(3.182 * s["std"] / 2)  # t(0.975, df=3)
    assert sc.stats([5.0]) == {"n": 1, "mean": 5.0, "std": 0.0, "ci95": 0.0}


def test_run_one_samples_a_series():
    r = sc.run_one(sc.build(WEB, sc.BASELINE, 1), 100, samples=10)
    assert [round(t) for t, _ in r["series"]] == list(range(10, 101, 10))
    assert not r["truncated"]
    capped = sc.run_one(sc.build(WEB, sc.BASELINE, 1), 100, samples=10, max_events=500)
    assert capped["truncated"] and capped["events"] <= 600


# --- what-if from a running sim ---------------------------------------------------------


def test_whatif_baseline_is_the_plain_continuation():
    sim = sc.build(WEB, sc.BASELINE, 3)
    sim.run(until=50)
    out = sc.whatif(sim, WEB.scenario("db-slow"), horizon=100)
    assert out["from"] == 50 and sim.now == 50  # the original is untouched
    sim.run(until=150)
    assert out[sc.BASELINE]["flat"] == sc.flat_metrics(sim.summary())
    assert latency(out["db-slow"]) > latency(out[sc.BASELINE])


def test_whatif_timed_patches_are_relative_to_the_fork():
    sim = sc.build(WEB, sc.BASELINE, 3)
    sim.run(until=50)
    out = sc.whatif(sim, WEB.scenario("app1-link-down"), horizon=150)  # link fails at 150
    lost = out["app1-link-down"]["series"]
    by_t = {round(t): f["links.lb.app_a->app1.in.lost.value"] for t, f in lost}
    assert by_t[95] == 0 and by_t[200] > 0


# --- storage ----------------------------------------------------------------------------


def test_store_round_trip_and_refuses_newer_schema(tmp_path):
    db = storage.RunStore(tmp_path / "r.sqlite3")
    bid = db.create_batch("batch", {"name": "m"}, {"seeds": [1]})
    db.add_run(bid, "baseline", 1, {"events": 5, "wall_ms": 1.0, "truncated": False,
                                    "flat": {"a": 1.0}, "series": [[1.0, {"a": 1.0}]]})  # fmt: skip
    db.finish(bid)
    got = db.batch(bid)
    assert got["status"] == "done" and got["runs_detail"][0]["flat"] == {"a": 1.0}
    storage.RunStore(tmp_path / "r.sqlite3")  # re-opening is a no-op migration

    conn = sqlite3.connect(tmp_path / "r.sqlite3")
    conn.execute(f"PRAGMA user_version = {len(storage.MIGRATIONS) + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="newer code"):
        storage.RunStore(tmp_path / "r.sqlite3")


# --- API ------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "RUNS_DB", tmp_path / "runs.sqlite3")
    return TestClient(app_module.app)


def wait_done(client, batch_id, metric=None):
    for _ in range(200):
        b = client.get(f"/api/batches/{batch_id}", params={"metric": metric} if metric else {})
        if b.json()["status"] != "running":
            return b.json()
        time.sleep(0.05)
    raise AssertionError("batch did not finish")


def test_batch_api_compares_scenarios_over_seeds(client):
    body = {
        "model": WEB.model_dump(by_alias=True),
        "scenarios": ["baseline", "db-slow"],
        "seeds": 3,
        "until": 100,
        "samples": 5,
    }
    r = client.post("/api/batches", json=body)
    assert r.status_code == 200 and r.json()["runs"] == 6
    b = wait_done(client, r.json()["id"], metric="entities.done.latency.mean")
    assert b["status"] == "done" and b["runs"] == 6
    base = b["aggregate"]["baseline"]["entities.done.latency.mean"]
    slow = b["aggregate"]["db-slow"]["entities.done.latency.mean"]
    assert base["n"] == 3 and slow["mean"] > base["mean"] + base["ci95"]
    assert len(b["series"]["db-slow"]) == 5
    assert "entities.done.latency.mean" in b["metrics"]
    assert client.get("/api/batches").json()[0]["id"] == b["id"]


def test_batch_api_rejects_bad_requests(client):
    model = WEB.model_dump(by_alias=True)
    assert (
        client.post("/api/batches", json={"model": model, "scenarios": ["zz"]}).status_code == 422
    )
    too_many = {"model": model, "seeds": 500}
    assert client.post("/api/batches", json=too_many).status_code == 422
    assert client.get("/api/batches/9999").status_code == 404


def test_whatif_over_the_websocket(client):
    with client.websocket_connect("/ws/run") as ws:
        ws.send_json({"cmd": "load", "model": WEB.model_dump(by_alias=True), "seed": 1})
        ws.receive_json(), ws.receive_json()
        for _ in range(200):
            ws.send_json({"cmd": "step"})
            ws.receive_json()
        ws.send_json({"cmd": "whatif", "scenario": "db-slow", "horizon": 50})
        reply = ws.receive_json()
        assert reply["type"] == "whatif"
        ws.send_json({"cmd": "whatif", "scenario": "nope"})
        assert "no scenario" in ws.receive_json()["message"]
    b = wait_done(client, reply["batch"])
    assert b["kind"] == "whatif" and set(b["aggregate"]) == {"baseline", "db-slow"}
