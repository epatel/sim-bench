"""Replay recorded agent conversations without the CLI or a model.

Each cassette in tests/cassettes/ was recorded with ``python -m server.agent.record``.
The CLI's side is played back; our side (SDK, tool handlers, read guard, permission
callback) runs for real and must answer exactly as it did when recorded. If a change
to the tools makes a cassette stale, re-record it with the command in its ``meta``.
"""

import asyncio
from pathlib import Path

import pytest

from server import app
from server.agent import session
from server.agent.record import Script, converse, paths_for
from server.agent.replay import ReplayTransport, load_cassette

CASSETTES = sorted((Path(__file__).parent / "cassettes").glob("*.jsonl"))
CASSETTES_BY_NAME = {p.stem: p for p in CASSETTES}


@pytest.fixture(autouse=True)
def restore_app_globals(monkeypatch):
    # converse() points the app at a sandbox; put the real paths back afterwards
    for name in ("LIBRARIES", "RUNS_DB", "LOCAL", "_store"):
        monkeypatch.setattr(app, name, getattr(app, name))


def replay(path, tmp_path):
    """Replay one cassette; return (transport, outcome, meta, paths)."""
    meta, entries = load_cassette(path)
    script = Script(meta["prompts"], meta["model_file"], meta["sources"])
    paths = paths_for(tmp_path, script)
    transports: list[ReplayTransport] = []

    def make_transport(options):
        transports.append(ReplayTransport(entries, paths))
        return transports[-1]

    outcome = asyncio.run(asyncio.wait_for(converse(script, tmp_path, make_transport), 60))
    [transport] = transports
    return transport, outcome, meta, paths


@pytest.mark.parametrize("path", CASSETTES, ids=lambda p: p.stem)
def test_cassette_replays_identically(path, tmp_path):
    transport, outcome, meta, paths = replay(path, tmp_path)
    assert transport.mismatches == []
    assert transport.leftover() == []
    assert paths.scrub_value(outcome.events) == meta["events"]
    assert paths.scrub_value(outcome.model) == meta["final_model"]


def test_replay_catches_a_tool_that_answers_differently(tmp_path, monkeypatch):
    check = app.check_model

    def reject_two_servers(model, library=None):
        if any(e.get("params", {}).get("servers") == 2 for e in model.get("entities", [])):
            raise app.ModelError("servers=2 rejected by this test")
        return check(model, library)

    monkeypatch.setattr(app, "check_model", reject_two_servers)
    transport, *_ = replay(CASSETTES_BY_NAME["scale-and-run"], tmp_path)
    assert any("differs" in m for m in transport.mismatches)


def test_replay_catches_a_read_guard_that_lets_secrets_through(tmp_path, monkeypatch):
    # the recorded conversation tried to read .env and was denied
    monkeypatch.setattr(session, "read_violation", lambda *a: None)
    transport, *_ = replay(CASSETTES_BY_NAME["shop-from-source"], tmp_path)
    assert any("looks like a secret" in m for m in transport.mismatches)


@pytest.mark.parametrize("path", CASSETTES, ids=lambda p: p.stem)
def test_recorded_session_saw_only_the_workbench_server(path):
    """Isolation: the CLI loaded no MCP servers or tools besides ours (strict_mcp_config)."""
    _, entries = load_cassette(path)
    [init] = [e["msg"] for e in entries if e["msg"].get("subtype") == "init"]
    assert [s["name"] for s in init["mcp_servers"]] == ["workbench"]
    mcp_tools = [t for t in init["tools"] if t.startswith("mcp__")]
    assert all(t.startswith("mcp__workbench__") for t in mcp_tools)
