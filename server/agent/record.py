"""Record an agent conversation as a test cassette (uses your login and the real model).

    .venv/bin/python -m server.agent.record tests/cassettes/NAME.jsonl \\
        --model projects/examples/mm1.yaml --prompt "..." [--prompt "..."] \\
        [--source shop=tests/fixtures/shop]

The conversation runs in a throwaway copy of the libraries, run store and source
registry. The cassette holds the wire traffic (see ``replay.py``) plus what the browser
received and the final model; ``tests/test_agent_replay.py`` replays every cassette
without the CLI and checks that our side answers the same way.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import claude_agent_sdk

from .. import app
from ..kernel.yamlio import load_yaml
from .replay import Paths, RecordingTransport, save_cassette
from .session import AgentSession, MakeTransport
from .workspace import Workspace

ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class Script:
    prompts: list[str]
    model_file: str | None = None  # repo-relative; None = an empty model
    sources: dict[str, str] = field(default_factory=dict)  # name -> repo-relative dir

    def start_model(self) -> dict[str, Any]:
        return load_yaml((ROOT / self.model_file).read_text()) if self.model_file else {}


@dataclass
class Outcome:
    events: list[dict[str, Any]]
    model: dict[str, Any]


def sandbox(tmp: Path) -> None:
    """Point the app at throwaway libraries, run store and local state under ``tmp``."""
    shutil.copytree(ROOT / "libraries", tmp / "libraries")
    app.LIBRARIES = tmp / "libraries"
    app.RUNS_DB = tmp / "runs.sqlite3"
    app.LOCAL = tmp / "local"
    app._store = None


def paths_for(tmp: Path, script: Script) -> Paths:
    named = {f"SOURCE:{n}": ROOT / d for n, d in script.sources.items()}
    named.update({"WORKDIR": workdir(tmp), "TMP": tmp, "ROOT": ROOT, "HOME": Path.home()})
    tokens = {}
    for name, path in named.items():
        tokens[f"{{{name}}}"] = str(path)
        if str(path.resolve()) != str(path):  # macOS: /var -> /private/var
            tokens[f"{{{name}_REAL}}"] = str(path.resolve())
    return Paths(tokens)


def workdir(tmp: Path) -> Path:
    return tmp / "local" / "agent-cwd"


async def converse(script: Script, tmp: Path, make_transport: MakeTransport) -> Outcome:
    """Run ``script`` against a sandboxed app, the way the chat panel would."""
    sandbox(tmp)
    events: list[dict[str, Any]] = []

    async def send(event: dict[str, Any]) -> None:
        events.append(event)

    workspace = Workspace({}, lambda m: app.check_model(m))
    env = app._agent_env(workspace, send)
    for name, rel in script.sources.items():
        env.sources.add(name, str((ROOT / rel).resolve()))
    session = AgentSession(env, send, workdir=workdir(tmp), transport=make_transport)
    model = script.start_model()
    try:
        for prompt in script.prompts:
            await session.ask(prompt, model)
            model = workspace.model  # the browser adopts every model the server pushes
    finally:
        await session.close()
    return Outcome(events, workspace.model)


async def record(script: Script, out: Path) -> Outcome:
    tmp = Path(tempfile.mkdtemp(prefix="simbench-record-"))
    paths = paths_for(tmp, script)
    recorders: list[RecordingTransport] = []

    def make_transport(options):
        recorders.append(RecordingTransport.subprocess(options, paths))
        return recorders[-1]

    try:
        outcome = await converse(script, tmp, make_transport)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if len(recorders) != 1:
        raise SystemExit(f"expected one CLI connection, got {len(recorders)}")
    meta = {
        "prompts": script.prompts,
        "model_file": script.model_file,
        "sources": script.sources,
        "recorded": date.today().isoformat(),
        "sdk": claude_agent_sdk.__version__,
        "events": paths.scrub_value(outcome.events),
        "final_model": paths.scrub_value(outcome.model),
    }
    save_cassette(out, meta, recorders[0].entries)
    return outcome


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("out", type=Path, help="cassette file (.jsonl)")
    ap.add_argument("--prompt", action="append", required=True, help="one user turn (repeat)")
    ap.add_argument("--model", help="starting model, repo-relative (default: empty)")
    ap.add_argument("--source", action="append", default=[], help="name=repo-relative dir")
    a = ap.parse_args()
    sources = dict(s.split("=", 1) for s in a.source)
    script = Script(a.prompt, a.model, sources)
    app.agent.strip_api_keys()
    outcome = asyncio.run(record(script, a.out))
    done = [e for e in outcome.events if e["type"] == "done"]
    tools = [e["name"] for e in outcome.events if e["type"] == "tool"]
    print(f"recorded {a.out}: {len(done)} turns, tools: {', '.join(tools) or 'none'}")


if __name__ == "__main__":
    main()
