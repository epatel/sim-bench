"""The optional access token (server/auth.py) for running on a public host."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server import app as appmod
from server.auth import TokenGate, load_token, read_env_file

TOKEN = "s3cret-token"


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(appmod, "TOKEN", TOKEN)
    return TestClient(TokenGate(appmod.app, TOKEN))


def test_api_needs_the_token_as_a_bearer_header(gated):
    assert gated.get("/api/libraries").status_code == 401
    assert gated.get("/api/auth", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = gated.get("/api/auth", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.json() == {"ok": True, "gated": True}


def test_static_files_stay_public_and_the_root_is_the_landing_page(gated):
    assert gated.get("/js/main.js").status_code == 200
    root = gated.get("/")
    assert root.status_code == 200 and "access token" in root.text
    assert "Simulation Workbench" in gated.get("/index.html").text


def test_websockets_take_the_token_as_a_subprotocol(gated):
    with pytest.raises(WebSocketDisconnect), gated.websocket_connect("/ws/run") as ws:
        ws.receive_json()
    protocols = ["sim-bench", f"token.{TOKEN}"]
    with gated.websocket_connect("/ws/run", subprotocols=protocols) as ws:
        assert ws.accepted_subprotocol == "sim-bench"


def test_without_a_token_nothing_is_gated():
    client = TestClient(appmod.app)
    assert client.get("/api/auth").json() == {"ok": True, "gated": False}
    assert "Simulation Workbench" in client.get("/").text


def test_token_comes_from_the_environment_or_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\n\nexport SIMBENCH_TOKEN="abc"\nOTHER=1\n')
    assert read_env_file(env) == {"SIMBENCH_TOKEN": "abc", "OTHER": "1"}
    monkeypatch.delenv("SIMBENCH_TOKEN", raising=False)
    assert load_token(env) == "abc"
    assert load_token(tmp_path / "missing") is None
    monkeypatch.setenv("SIMBENCH_TOKEN", "fromenv")
    assert load_token(env) == "fromenv"
