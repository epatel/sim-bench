import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient

from server import app as app_module

MODEL = {
    "name": "tiny",
    "entities": [
        {"id": "gen", "type": "source", "params": {"interarrival": 1.0, "count": 3}},
        {"id": "done", "type": "sink"},
    ],
    "connections": [{"from": "gen.out", "to": "done.in", "latency": 0.5}],
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "PROJECTS", tmp_path)
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "tiny.yaml").write_text(yaml.safe_dump(MODEL))
    return TestClient(app_module.app)


def test_types_have_ports_and_params(client):
    types = {t["kind"]: t for t in client.get("/api/types").json()}
    assert set(types) >= {"source", "server", "delay", "router", "sink"}
    assert types["server"]["inputs"] == ["in"] and types["server"]["outputs"] == ["out"]
    assert types["server"]["params"]["service"]["type"] == "dist"
    assert types["router"]["outputs"] == ["out"]  # from default routes


def test_list_and_get_model(client):
    assert client.get("/api/models").json() == [{"path": "examples/tiny.yaml", "name": "tiny"}]
    m = client.get("/api/models/examples/tiny.yaml").json()
    assert m["connections"][0]["from"] == "gen.out"


def test_save_round_trips_and_rejects_bad_models(client, tmp_path):
    r = client.put("/api/models/mine/new.yaml", json=MODEL)
    assert r.status_code == 200
    saved = yaml.safe_load((tmp_path / "mine" / "new.yaml").read_text())
    assert saved["connections"][0] == {"from": "gen.out", "to": "done.in", "latency": 0.5}

    bad = {**MODEL, "connections": [{"from": "gen.nope", "to": "done.in"}]}
    r = client.put("/api/models/mine/bad.yaml", json=bad)
    assert r.status_code == 422 and "not an output" in r.json()["detail"]


@pytest.mark.parametrize("path", ["../escape.yaml", "x.txt", "a/../../b.yaml", "/etc/x.yaml"])
def test_model_path_refuses_paths_outside_projects(client, path):
    with pytest.raises(HTTPException):
        app_module.model_path(path)
    # Over HTTP the client normalizes "..", so these never reach the handler at all.
    assert client.put(f"/api/models/{path}", json=MODEL).status_code >= 400


def test_validate_reports_ports_or_error(client):
    ok = client.post("/api/validate", json=MODEL).json()
    assert ok["ok"] and ok["ports"]["gen"] == {"inputs": [], "outputs": ["out"]}
    bad = client.post("/api/validate", json={"entities": [{"id": "a", "type": "zzz"}]}).json()
    assert not bad["ok"] and "unknown type" in bad["error"]
    dynamic = {
        **MODEL,
        "entities": [{**MODEL["entities"][0], "dynamic_ports": True}, *MODEL["entities"][1:]],
    }
    assert client.post("/api/validate", json=dynamic).json()["ok"]  # a canvas-only setting
    bad = client.post("/api/validate", json={**MODEL, "globals": "x = 1\ny ="}).json()
    assert not bad["ok"] and bad["error"].startswith("globals: line 2")


def test_websocket_load_step_and_reset(client):
    with client.websocket_connect("/ws/run") as ws:
        ws.send_json({"cmd": "step"})
        assert ws.receive_json()["type"] == "error"

        ws.send_json({"cmd": "load", "model": MODEL, "seed": 1})
        loaded = ws.receive_json()
        assert loaded["type"] == "loaded" and "gen" in loaded["ports"]
        # 3 deliveries by t=3.5: about 8 per real second at 8 * 3.5 / 3 sim units per second
        assert loaded["suggested_speed"] == pytest.approx(9.3)
        first = ws.receive_json()
        assert first["type"] == "frame" and first["now"] == 0 and "summary" in first

        # a step runs to the next delivery: sent at t=1, delivered after the 0.5 latency
        ws.send_json({"cmd": "step"})
        frame = ws.receive_json()
        assert frame["now"] == 1.5 and frame["step"]
        assert [r["type"] for r in frame["records"]] == ["send", "deliver"]
        assert frame["records"][1]["link"] == frame["records"][0]["link"]
        assert frame["records"][1]["entity"] == "done"

        for _ in range(3):  # two more deliveries, then nothing is left
            ws.send_json({"cmd": "step"})
            frame = ws.receive_json()
        assert frame["now"] == 3.5 and frame["pending"] == 0

        ws.send_json({"cmd": "reset"})
        assert ws.receive_json()["now"] == 0


def test_suggested_speed_follows_the_delivery_rate():
    from server.kernel import ModelSpec, Simulation

    def speed(interarrival):
        model = {**MODEL, "entities": [
            {"id": "gen", "type": "source", "params": {"interarrival": interarrival}},
            {"id": "done", "type": "sink"},
        ]}  # fmt: skip
        return app_module.suggested_speed(Simulation(ModelSpec.model_validate(model)))

    assert speed(1.0) == pytest.approx(8, rel=0.05)  # 1 delivery per sim unit
    assert speed(0.01) == pytest.approx(0.08, rel=0.05)
    assert speed(1e9) == app_module.SPEED_MAX  # clamped to the slider


def test_websocket_play_runs_to_completion(client):
    with client.websocket_connect("/ws/run") as ws:
        ws.send_json({"cmd": "load", "model": MODEL, "seed": 1})
        ws.receive_json(), ws.receive_json()
        ws.send_json({"cmd": "speed", "value": 1000})
        ws.send_json({"cmd": "play"})
        for _ in range(50):
            frame = ws.receive_json()
            if not frame["playing"]:
                break
        assert frame["summary"]["entities"]["done"]["received"]["value"] == 3


def test_frontend_is_revalidated_so_updates_are_never_stale(client):
    for path in ("/", "/js/main.js", "/css/app.css"):
        assert client.get(path).headers["cache-control"] == "no-cache", path


def test_index_and_static_files_are_served(client):
    assert "sim-bench" in client.get("/").text
    assert client.get("/js/main.js").status_code == 200
    assert client.get("/vendor/fontawesome/css/all.min.css").status_code == 200


# --- libraries and templates (M3) ------------------------------------------------


def test_types_include_library_types_with_merged_defaults(client):
    types = {t["kind"]: t for t in client.get("/api/types").json()}
    app = types["web/app-server"]
    assert app["library"] == "web" and app["base"] == "server"
    assert app["params"]["servers"]["default"] == 4  # library override
    assert app["params"]["service"]["type"] == "dist"  # schema comes from the base
    assert types["web/cache"]["outputs"] == ["hit", "miss"]
    assert types["server"]["library"] == ""


def test_libraries_and_templates_are_listed(client):
    libs = {lib["name"] for lib in client.get("/api/libraries").json()}
    assert {"web", "queueing"} <= libs
    templates = {t["id"]: t for t in client.get("/api/templates").json()}
    assert templates["web/lb-pool"]["params"]["n"]["default"] == 3


def test_instantiate_template(client):
    r = client.post(
        "/api/templates/web/lb-pool/instantiate", json={"args": {"n": 2}, "prefix": "p"}
    )
    assert r.status_code == 200
    frag = r.json()
    assert [e["id"] for e in frag["entities"]] == ["p_lb", "p_s1", "p_s2", "p_join"]
    bad = client.post("/api/templates/web/lb-pool/instantiate", json={"args": {"n": 0}})
    assert bad.status_code == 422 and ">= 1" in bad.json()["detail"]
    missing = client.post("/api/templates/web/nope/instantiate", json={})
    assert missing.status_code == 422


def test_library_model_validates_and_runs(client):
    model = {
        "entities": [
            {"id": "u", "type": "web/client", "params": {"count": 5}},
            {"id": "d", "type": "web/response"},
        ],
        "connections": [{"from": "u.out", "to": "d.in"}],
    }
    assert client.post("/api/validate", json=model).json()["ok"]
    with client.websocket_connect("/ws/run") as ws:
        ws.send_json({"cmd": "load", "model": model, "seed": 1})
        assert ws.receive_json()["type"] == "loaded"
        ws.receive_json()
        ws.send_json({"cmd": "speed", "value": 1000})
        ws.send_json({"cmd": "play"})
        for _ in range(50):
            frame = ws.receive_json()
            if not frame["playing"]:
                break
        done = frame["summary"]["entities"]["d"]
        assert done["received"]["value"] == 5 and done["item_type"] == "web/response"


# --- composites (M4) ---------------------------------------------------------------


def grouped_model(client, mode="expanded", abstract=None):
    frag = client.post(
        "/api/templates/web/lb-pool/instantiate",
        json={"args": {"n": 2, "server": "queueing/mm-server"}, "prefix": "p"},
    ).json()
    group = {"id": "p", "inputs": frag["inputs"], "outputs": frag["outputs"], "mode": mode}
    if abstract:
        group["abstract"] = abstract
    return {
        "entities": [
            {"id": "src", "type": "queueing/poisson-source", "params": {"interarrival": 2}},
            *frag["entities"],
            {"id": "out", "type": "sink"},
        ],
        "connections": [
            {"from": "src.out", "to": "p.in"},
            *frag["connections"],
            {"from": "p.out", "to": "out.in"},
        ],
        "groups": [group],
    }


def test_validate_reports_group_ports(client):
    res = client.post("/api/validate", json=grouped_model(client)).json()
    assert res["ok"], res
    assert res["groups"]["p"] == {"inputs": ["in"], "outputs": ["out"], "mode": "expanded"}
    abstract = grouped_model(client, "abstract", {"type": "delay", "params": {"delay": 1}})
    res = client.post("/api/validate", json=abstract).json()
    assert res["ok"] and res["ports"]["p"] == {"inputs": ["in"], "outputs": ["out"]}
    assert "p_lb" not in res["ports"]


def test_calibrate_proposes_a_stand_in(client):
    model = grouped_model(client, "abstract", {"type": "delay", "params": {"delay": 99}})
    r = client.post("/api/calibrate", json={"model": model, "group": "p", "until": 2000, "seed": 1})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["probe"]["exited"] > 500  # it ran expanded, despite the model saying abstract
    assert res["abstract"]["type"] == "server"  # the pool has servers, so it gets a capacity
    assert res["abstract"]["calibration"]["arrival_rate"] == pytest.approx(0.5, rel=0.1)
    body = {"model": model, "group": "p", "until": 2000, "seed": 1, "kind": "delay"}
    assert client.post("/api/calibrate", json=body).json()["abstract"]["type"] == "delay"
    body["kind"] = "fast"
    assert client.post("/api/calibrate", json=body).status_code == 422
    bad = client.post("/api/calibrate", json={"model": model, "group": "zzz"})
    assert bad.status_code == 422


def test_saved_model_keeps_groups(client, tmp_path):
    model = grouped_model(client, "abstract", {"type": "delay", "params": {"delay": 1}})
    assert client.put("/api/models/g.yaml", json=model).status_code == 200
    again = client.get("/api/models/g.yaml").json()
    assert again["groups"][0]["mode"] == "abstract"
    assert again["groups"][0]["abstract"]["params"] == {"delay": 1}


def test_types_expose_machine_and_python_behavior_params(client):
    types = {t["kind"]: t for t in client.get("/api/types").json()}
    cb = types["web/circuit-breaker"]
    assert cb["base"] == "machine" and cb["inputs"] == ["in", "failure"]
    assert cb["params"]["threshold"] == {"type": "int", "default": 5}
    assert cb["params"]["machine"]["type"] == "machine"
    assert cb["params"]["machine"]["default"].startswith("inputs in, failure")  # a script
    tb = types["web/token-bucket"]
    assert tb["library"] == "web" and tb["outputs"] == ["out", "rejected"]
    assert tb["params"]["rate"]["default"] == 10.0


def test_speed_can_be_set_before_loading(client):
    with client.websocket_connect("/ws/run") as ws:
        ws.send_json({"cmd": "speed", "value": 5})
        ws.send_json({"cmd": "load", "model": MODEL, "seed": 1})
        assert ws.receive_json()["type"] == "loaded"  # no error came first


def test_frames_carry_the_visuals(client):
    model = {**MODEL, "visuals": [{"id": "got", "entity": "done", "code": "output = received"}]}
    with client.websocket_connect("/ws/run") as ws:
        ws.send_json({"cmd": "load", "model": model, "seed": 1})
        ws.receive_json()  # loaded
        assert ws.receive_json()["visuals"] == {"got": {"output": 0}}
        ws.send_json({"cmd": "step"})
        assert ws.receive_json()["visuals"] == {"got": {"output": 1}}


def test_visuals_can_be_swapped_while_running(client):
    with client.websocket_connect("/ws/run") as ws:
        ws.send_json({"cmd": "load", "model": MODEL, "seed": 1})
        ws.receive_json(), ws.receive_json()  # loaded, first frame
        ws.send_json({"cmd": "visuals", "visuals": [{"id": "t", "code": "output = now + 1"}]})
        assert ws.receive_json()["visuals"] == {"t": {"output": 1}}
        ws.send_json({"cmd": "visuals", "visuals": [{"id": "t", "code": "nope"}]})
        assert ws.receive_json()["message"].startswith("visuals: visuals.t: line 1")
