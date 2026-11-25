"""Agent wiring and tools, without calling a model: tool handlers are invoked directly."""

import asyncio
import shutil
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from server import agent
from server import app as app_module
from server.agent.session import AgentSession, events_for
from server.agent.tools import TOOL_FACTORIES, TOOL_NAMES
from server.kernel import load_model

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    libs = tmp_path / "libraries"
    shutil.copytree(ROOT / "libraries", libs)
    monkeypatch.setattr(app_module, "LIBRARIES", libs)
    monkeypatch.setattr(app_module, "RUNS_DB", tmp_path / "runs.sqlite3")
    monkeypatch.setattr(app_module, "LOCAL", tmp_path / "local")
    events = []

    async def send(event):
        events.append(event)

    workspace = agent.Workspace({}, lambda m: app_module.check_model(m))
    e = app_module._agent_env(workspace, send)
    e.events = events
    return e


def call(env, tool_name, /, **args):
    """Run one tool; return (text, is_error)."""
    make = TOOL_FACTORIES[TOOL_NAMES.index(tool_name)]
    result = asyncio.run(make(env).handler(args))
    return result["content"][0]["text"], result.get("is_error", False)


def pipeline(env):
    call(env, "add_entity", id="src", type="source", params={"interarrival": 1, "count": 20})
    call(env, "add_entity", id="srv", type="queueing/mm-server")
    call(env, "add_entity", id="out", type="sink")
    call(env, "connect", **{"from": "src.out", "to": "srv.in"})
    call(env, "connect", **{"from": "srv.out", "to": "out.in", "latency": 0.5})


def test_tool_names_match_factories(env):
    built = [make(env).name for make in TOOL_FACTORIES]
    assert built == TOOL_NAMES
    assert all(n.startswith("mcp__workbench__") for n in agent.tool_names())


def test_building_a_model_and_running_it(env):
    pipeline(env)
    text, error = call(env, "run", until=100)
    assert not error
    out_line = next(line for line in text.splitlines() if line.startswith("out (sink)"))
    assert "received=20" in out_line and "latency mean=" in out_line
    models = [e for e in env.events if e["type"] == "model"]
    assert len(models) == 5 and len(models[-1]["model"]["connections"]) == 2


def test_invalid_changes_are_rejected_and_not_kept(env):
    pipeline(env)
    before = env.workspace.model
    text, error = call(env, "connect", **{"from": "out.nope", "to": "srv.in"})
    assert error and "rejected" in text and "not an output" in text
    assert env.workspace.model == before
    text, error = call(env, "add_entity", id="src", type="sink")
    assert error and "already exists" in text


def test_update_merge_and_undo(env):
    pipeline(env)
    call(env, "update_entity", id="srv", params={"servers": 3})
    assert env.workspace.entity(env.workspace.model, "srv")["params"] == {"servers": 3}
    call(env, "update_entity", id="srv", params={"servers": None})
    assert env.workspace.entity(env.workspace.model, "srv")["params"] == {}
    call(env, "undo")
    assert env.workspace.entity(env.workspace.model, "srv")["params"] == {"servers": 3}


def test_remove_entity_drops_its_connections(env):
    pipeline(env)
    call(env, "remove_entity", id="srv")
    assert env.workspace.model["connections"] == []


def test_template_group_calibration_and_scenarios(env):
    call(env, "add_entity", id="src", type="queueing/poisson-source", params={"interarrival": 2})
    text, error = call(env, "insert_template", template="web/lb-pool", prefix="pool",
                       args={"n": 2, "server": "queueing/mm-server"})  # fmt: skip
    assert not error and "group pool" in text
    call(env, "add_entity", id="out", type="sink")
    call(env, "connect", **{"from": "src.out", "to": "pool.in"})
    call(env, "connect", **{"from": "pool.out", "to": "out.in"})
    text, error = call(env, "calibrate_group", group="pool", until=500)
    assert not error and "calibrated pool" in text
    call(env, "set_group", group="pool", mode="abstract")
    assert env.workspace.group(env.workspace.model, "pool")["mode"] == "abstract"

    call(env, "set_scenario", id="slow-net",
         patches=[{"link": "src.out->pool.in", "field": "latency", "value": 1.0}])  # fmt: skip
    text, error = call(env, "run", until=200, scenario="slow-net")
    assert not error, text
    text, error = call(env, "run_batch", seeds=2, until=200)
    assert not error and "baseline:" in text and "slow-net:" in text
    assert any(e["type"] == "batch" for e in env.events)


def test_get_model_and_list_types(env):
    pipeline(env)
    text, _ = call(env, "get_model")
    assert "srv" in text and "queueing/mm-server" in text
    text, _ = call(env, "list_types")
    assert "web/circuit-breaker: machine" in text and "web/lb-pool" in text


def test_define_type_writes_yaml_and_rolls_back_bad_ones(env):
    text, error = call(env, "define_type", library="web", name="slow-db", extends="web/database",
                       params={"service": {"dist": "exp", "mean": 0.2}})  # fmt: skip
    assert not error and (env.libraries_dir / "web/types/slow-db.yaml").exists()
    assert "web/slow-db" in call(env, "list_types")[0]

    text, error = call(env, "define_type", library="web", name="loop", extends="web/loop")
    assert error and "cycle" in text
    assert not (env.libraries_dir / "web/types/loop.yaml").exists()

    text, error = call(env, "define_type", library="../etc", name="x", extends="sink")
    assert error and "lowercase" in text


def test_replace_model_from_yaml(env):
    yaml_text = (ROOT / "projects/examples/mm1.yaml").read_text()
    text, error = call(env, "replace_model", yaml=yaml_text)
    assert not error and [e["id"] for e in env.workspace.model["entities"]] == [
        "gen",
        "srv",
        "done",
    ]


# --- session wiring ---------------------------------------------------------------------------


def test_session_is_isolated_and_limited_to_workbench_tools(env):
    s = AgentSession(env, env.notify, workdir=Path(env.libraries_dir).parent / "cwd")
    o = s.options()
    assert o.tools == [] and o.setting_sources == [] and o.skills == [] and o.add_dirs == []
    assert o.strict_mcp_config  # no claude.ai connectors or user MCP servers
    assert set(o.allowed_tools) == set(agent.tool_names())
    deny = asyncio.run(s.can_use_tool("Bash", {"command": "ls"}, None))
    allow = asyncio.run(s.can_use_tool("mcp__workbench__run", {}, None))
    assert deny.behavior == "deny" and allow.behavior == "allow"


def test_api_keys_are_stripped(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert agent.strip_api_keys() == ["ANTHROPIC_API_KEY"]
    import os

    assert "ANTHROPIC_API_KEY" not in os.environ


def test_sdk_messages_become_chat_events():
    msg = AssistantMessage(
        content=[TextBlock("hello"), ToolUseBlock("t1", "mcp__workbench__run", {"until": 5})],
        model="m",
    )
    assert events_for(msg) == [
        {"type": "text", "text": "hello"},
        {"type": "tool", "id": "t1", "name": "run", "input": {"until": 5}},
    ]
    result = UserMessage(content=[ToolResultBlock("t1", [{"type": "text", "text": "ok"}], False)])
    assert events_for(result) == [{"type": "tool_result", "id": "t1", "error": False, "text": "ok"}]
    done = ResultMessage("success", 10, 5, False, 2, "s", total_cost_usd=0.01)
    assert events_for(done)[0] == {"type": "done", "error": False, "subtype": "success", "turns": 2,
                                   "duration_ms": 10, "cost_usd": 0.01}  # fmt: skip


def test_status_endpoint():
    from fastapi.testclient import TestClient

    status = TestClient(app_module.app).get("/api/agent/status").json()
    assert "run_batch" in status["tools"] and status["sdk"]


def test_examples_still_load():
    assert load_model(ROOT / "projects/examples/web-backend.yaml").scenarios
