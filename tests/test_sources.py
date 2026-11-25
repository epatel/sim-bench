"""M8: registered source trees, the read guard, provenance and proposals."""

import asyncio
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from server import agent
from server import app as app_module
from server.agent.session import READ_TOOLS, AgentSession, read_violation
from server.agent.tools import TOOL_FACTORIES, TOOL_NAMES
from server.kernel import ModelSpec
from server.sources import SourceError, SourceRegistry

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def project(tmp_path):
    """A small 'other project' to read."""
    p = tmp_path / "work" / "shop"
    (p / "src").mkdir(parents=True)
    (p / "src" / "worker.py").write_text("\n".join(f"line {i}" for i in range(1, 41)) + "\n")
    (p / ".env").write_text("TOKEN=nope\n")
    return p


@pytest.fixture
def registry(tmp_path, project):
    r = SourceRegistry(tmp_path / "local" / "sources.json")
    r.add("shop", str(project))
    return r


# --- registry and access checks ------------------------------------------------------------


def test_registry_round_trip(registry, project):
    assert [(s.name, s.path) for s in registry.all()] == [("shop", project.resolve())]
    registry.remove("shop")
    assert registry.all() == []


@pytest.mark.parametrize(
    "case",
    [
        ("Bad Name", None, "lowercase"),
        ("x", "relative/dir", "absolute"),
        ("x", "/definitely/not/here", "not a directory"),
        ("x", "/", "too broad"),
        ("x", str(Path.home()), "too broad"),
    ],
)
def test_registry_refuses_bad_entries(registry, project, case):
    name, path, message = case
    with pytest.raises(SourceError, match=message):
        registry.add(name, path or str(project))


def test_only_files_inside_sources_and_not_secrets_are_readable(registry, project):
    assert registry.check_readable(str(project / "src" / "worker.py"))
    for path, message in [
        (str(project.parent), "outside"),
        ("/etc/passwd", "outside"),
        (str(project / "src" / ".." / ".." / "other"), "outside"),
        (str(project / ".env"), "secret"),
        (None, "pass a path"),
    ]:
        with pytest.raises(SourceError, match=message):
            registry.check_readable(path)


def test_symlinks_out_of_a_source_are_refused(registry, project, tmp_path):
    (tmp_path / "outside.txt").write_text("x")
    (project / "link.txt").symlink_to(tmp_path / "outside.txt")
    with pytest.raises(SourceError, match="outside"):
        registry.check_readable(str(project / "link.txt"))


def test_snippet_shows_lines_around_a_reference(registry):
    snip = registry.snippet("shop:src/worker.py:20-21", context=2)
    assert snip["start"] == 18 and snip["lines"][0] == "line 18" and snip["lines"][-1] == "line 23"
    with pytest.raises(SourceError, match="secret"):
        registry.snippet("shop:.env")
    with pytest.raises(SourceError, match="no registered source"):
        registry.snippet("nope:a.py:1")


@pytest.mark.parametrize(
    "case",
    [
        ("Read", {"file_path": "{p}/src/worker.py"}, True),
        ("Read", {"file_path": "/etc/hosts"}, False),
        ("Read", {"file_path": "{p}/.env"}, False),
        ("Grep", {"pattern": "def", "path": "{p}"}, True),
        ("Grep", {"pattern": "def"}, False),  # no path: would search the agent's cwd
        ("Grep", {"pattern": "x", "path": "{p}", "glob": "/etc/*"}, False),
        ("Glob", {"pattern": "**/*.py", "path": "{p}"}, True),
        ("Glob", {"pattern": "../../**", "path": "{p}"}, False),
    ],
)
def test_read_guard(registry, project, case):
    tool, tool_input, ok = case
    args = {k: v.replace("{p}", str(project)) for k, v in tool_input.items()}
    assert (read_violation(registry, tool, args) is None) == ok


# --- the agent with sources --------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch, project):
    libs = tmp_path / "libraries"
    shutil.copytree(ROOT / "libraries", libs)
    monkeypatch.setattr(app_module, "LIBRARIES", libs)
    monkeypatch.setattr(app_module, "RUNS_DB", tmp_path / "runs.sqlite3")
    monkeypatch.setattr(app_module, "LOCAL", tmp_path / "local")
    app_module.sources().add("shop", str(project))
    events = []

    async def send(event):
        events.append(event)

    e = app_module._agent_env(agent.Workspace({}, lambda m: app_module.check_model(m)), send)
    e.events = events
    return e


def call(env, tool_name, /, **args):
    make = TOOL_FACTORIES[TOOL_NAMES.index(tool_name)]
    result = asyncio.run(make(env).handler(args))
    return result["content"][0]["text"], result.get("is_error", False)


def test_session_with_sources_can_read_only_through_the_guard(env, project, tmp_path):
    s = AgentSession(env, env.notify, workdir=tmp_path / "cwd")
    o = s.options()
    assert o.tools == READ_TOOLS and o.add_dirs == [str(project.resolve())]
    assert o.cwd == str(tmp_path / "cwd") and "Bash" not in o.allowed_tools
    assert "shop:" in o.system_prompt and "propose_changes" in o.system_prompt
    denied = asyncio.run(
        s.guard_read({"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}, None, None)
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    allowed = asyncio.run(
        s.guard_read(
            {"tool_name": "Read", "tool_input": {"file_path": str(project / "src/worker.py")}},
            None,
            None,
        )
    )
    assert allowed == {}


def test_list_sources_tool(env):
    text, error = call(env, "list_sources")
    assert not error and "- shop:" in text


PROPOSED_TYPE = {
    "library": "web",
    "name": "job-worker",
    "extends": "machine",
    "description": "Worker loop from src/worker.py",
    "source": ["shop:src/worker.py:10-20"],
    "params": {"retries": 2},
    "machine": {
        "initial": "idle",
        "states": {"idle": {"on": {"in": [{"send": "out"}]}}},
    },
}
PROPOSED_MODEL = {
    "name": "shop",
    "entities": [
        {"id": "jobs", "type": "source", "source": ["shop:src/worker.py:1-5"]},
        {"id": "worker", "type": "web/job-worker", "source": ["shop:src/worker.py:10-20"]},
        {"id": "done", "type": "sink"},
    ],
    "connections": [
        {"from": "jobs.out", "to": "worker.in"},
        {"from": "worker.out", "to": "done.in"},
    ],
}


def propose(env, **override):
    args = {
        "summary": "Job worker from shop",
        "notes": "service time assumed",
        "model_yaml": yaml.safe_dump(PROPOSED_MODEL),
        "types": [PROPOSED_TYPE],
        **override,
    }
    return call(env, "propose_changes", **args)


def test_proposal_is_validated_but_not_applied(env):
    text, error = propose(env)
    assert not error and "Stop now and wait" in text
    p = env.workspace.proposal
    assert p["types"][0]["name"] == "job-worker" and p["model"]["entities"][1]["source"]
    assert not (env.libraries_dir / "web/types/job-worker.yaml").exists()  # not written yet
    assert env.workspace.model["entities"] == []  # not applied yet
    assert env.events[-1]["type"] == "proposal"


def test_invalid_proposals_are_rejected(env):
    bad_type = {**PROPOSED_TYPE, "machine": {"initial": "nope", "states": {"idle": {}}}}
    text, error = propose(env, types=[bad_type])
    assert error and "initial state" in text
    text, error = propose(env, types=[])  # the model needs the type
    assert error and "unknown type" in text
    assert env.workspace.proposal is None


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, event):
        self.sent.append(event)


def chat_with_proposal(env):
    ws = FakeWS()
    chat = app_module.AgentChat(ws)
    chat.workspace = env.workspace
    propose(env)
    return chat, ws


def test_accepting_writes_types_and_adopts_the_model(env):
    chat, ws = chat_with_proposal(env)
    asyncio.run(chat.handle({"cmd": "accept"}))
    written = env.libraries_dir / "web/types/job-worker.yaml"
    assert written.exists() and "shop:src/worker.py:10-20" in written.read_text()
    assert [e["id"] for e in env.workspace.model["entities"]] == ["jobs", "worker", "done"]
    assert [e["type"] for e in ws.sent] == ["model", "proposal_resolved"]
    assert "accepted" in env.workspace.take_notes()
    env.workspace.undo()  # an accepted proposal is undoable
    assert env.workspace.model["entities"] == []


def test_rejecting_changes_nothing(env):
    chat, ws = chat_with_proposal(env)
    asyncio.run(chat.handle({"cmd": "reject"}))
    assert not (env.libraries_dir / "web/types/job-worker.yaml").exists()
    assert env.workspace.proposal is None and env.workspace.model["entities"] == []
    assert ws.sent[-1] == {"type": "proposal_resolved", "accepted": False}
    assert "rejected your proposal: Job worker from shop" in env.workspace.take_notes()


# --- API and provenance ---------------------------------------------------------------------


def test_sources_api_and_snippets(tmp_path, monkeypatch, project):
    monkeypatch.setattr(app_module, "LOCAL", tmp_path / "local")
    client = TestClient(app_module.app)
    assert (
        client.post("/api/sources", json={"name": "shop", "path": str(project)}).status_code == 200
    )
    assert client.post("/api/sources", json={"name": "root", "path": "/"}).status_code == 422
    assert client.get("/api/sources").json() == [{"name": "shop", "path": str(project.resolve())}]
    snip = client.get("/api/sources/snippet", params={"ref": "shop:src/worker.py:5"}).json()
    assert snip["first"] == 5 and "line 5" in snip["lines"]
    assert client.get("/api/sources/snippet", params={"ref": "shop:.env"}).status_code == 422
    assert client.delete("/api/sources/shop").json() == {"ok": True}


def test_entities_and_types_carry_provenance(env):
    m = ModelSpec.model_validate(PROPOSED_MODEL | {"entities": PROPOSED_MODEL["entities"][:1]})
    assert m.entities[0].source == ["shop:src/worker.py:1-5"]
    agent.write_types(env.libraries_dir, [PROPOSED_TYPE])
    types = {t["kind"]: t for t in app_module.list_types()}
    assert types["web/job-worker"]["source"] == ["shop:src/worker.py:10-20"]
