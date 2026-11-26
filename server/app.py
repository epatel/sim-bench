"""HTTP + WebSocket API for the workbench (milestones M2-M3).

REST:
  GET  /api/types                 item types (built-in + libraries) with ports and param schemas
  GET  /api/libraries             loaded libraries (name, description)
  GET  /api/templates             templates with their parameters
  POST /api/templates/{id}/instantiate   expand a template into entities + connections
  GET  /api/models                model files under projects/
  GET  /api/models/{path}         one model as JSON
  PUT  /api/models/{path}         validate and save a model as YAML
  POST /api/validate              check a model; returns ports per entity and group, or the error
  POST /api/calibrate             run one group expanded; propose an abstract stand-in

WebSocket /ws/run: one live simulation per connection. See ``RunSession``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import agent
from .kernel import (
    CompositeError,
    Library,
    LibraryError,
    ModelError,
    ModelSpec,
    Simulation,
    run_calibration,
)
from .kernel import scenario as scen
from .kernel.calibrate import Request as CalibrationRequest
from .kernel.yamlio import load_yaml
from .sources import SourceError, SourceRegistry
from .storage import RunStore

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects"
RUNS_DB = ROOT / "runs.sqlite3"
LOCAL = ROOT / ".simbench"  # machine-specific state (gitignored)
LIBRARIES = ROOT / "libraries"
WEB = ROOT / "web"

app = FastAPI(title="Simulation Workbench")
log = logging.getLogger("simbench")


# --- models on disk -----------------------------------------------------------


def model_path(rel: str) -> Path:
    """Resolve a model path under projects/, refusing anything outside it."""
    if not rel.endswith((".yaml", ".yml")):
        raise HTTPException(400, "model path must end in .yaml")
    path = (PROJECTS / rel).resolve()
    if not path.is_relative_to(PROJECTS.resolve()):
        raise HTTPException(400, "model path must stay inside projects/")
    return path


def dump_model(model: ModelSpec) -> str:
    data = model.model_dump(by_alias=True, exclude_none=True, exclude_defaults=True)
    data["entities"] = [_round_pos(e) for e in data.get("entities", [])]
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=None, width=100)


def _round_pos(entity: dict[str, Any]) -> dict[str, Any]:
    if "pos" in entity:
        entity["pos"] = [round(float(v), 1) for v in entity["pos"]]
    return entity


def load_library() -> Library:
    """Read libraries/ fresh on every call: it is small, and the agent edits it live."""
    try:
        return Library.load(LIBRARIES)
    except LibraryError as e:
        raise HTTPException(500, f"library error: {e}") from e


def check_model(data: Any, library: Library | None = None) -> tuple[ModelSpec, Simulation]:
    try:
        model = ModelSpec.model_validate(data)
        return model, Simulation(model, library=library or load_library())
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise ModelError(f"{where}: {first['msg']}") from e


@app.get("/api/types")
def list_types() -> list[dict[str, Any]]:
    lib = load_library()
    out = []
    for tid in lib.all_type_ids():
        r = lib.resolve(tid)
        ins, outs = r.behavior.ports(r.defaults)
        schema = {
            name: {**meta, "default": r.defaults.get(name)}
            for name, meta in r.behavior.params_schema.items()
        }
        # params a type adds for its own use (e.g. a machine's thresholds)
        schema.update(
            {
                k: {"type": _guess_type(v), "default": v}
                for k, v in r.defaults.items()
                if k not in schema
            }
        )
        item = lib.types.get(tid)
        out.append(
            {
                "source": item.source if item else None,
                "kind": tid,
                "base": r.chain[-1],
                "chain": r.chain,
                "library": r.library,
                "description": r.description,
                "icon": r.icon,
                "inputs": ins,
                "outputs": outs,
                "params": schema,
            }
        )
    return out


def _guess_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "string" if isinstance(value, str) else "object"


@app.get("/api/libraries")
def list_libraries() -> list[dict[str, Any]]:
    return list(load_library().libraries.values())


@app.get("/api/templates")
def list_templates() -> list[dict[str, Any]]:
    return [
        {
            "id": t.id,
            "library": t.library,
            "description": t.description,
            "icon": t.icon,
            "params": t.params,
            "inputs": t.inputs,
            "outputs": t.outputs,
        }
        for t in load_library().templates.values()
    ]


@app.post("/api/templates/{template_id:path}/instantiate")
def instantiate(template_id: str, body: dict[str, Any]) -> dict[str, Any]:
    lib = load_library()
    try:
        return lib.instantiate(template_id, body.get("args") or {}, body.get("prefix"))
    except LibraryError as e:
        raise HTTPException(422, str(e)) from e


@app.get("/api/models")
def list_models() -> list[dict[str, str]]:
    out = []
    for p in sorted(PROJECTS.rglob("*.yaml")):
        try:
            name = (load_yaml(p.read_text()) or {}).get("name", p.stem)
        except yaml.YAMLError:
            name = f"{p.stem} (unreadable)"
        out.append({"path": p.relative_to(PROJECTS).as_posix(), "name": name})
    return out


@app.get("/api/models/{rel:path}")
def get_model(rel: str) -> dict[str, Any]:
    path = model_path(rel)
    if not path.exists():
        raise HTTPException(404, f"no model {rel}")
    model = ModelSpec.model_validate(load_yaml(path.read_text()))
    return model.model_dump(by_alias=True)


@app.put("/api/models/{rel:path}")
def save_model(rel: str, body: dict[str, Any]) -> dict[str, Any]:
    path = model_path(rel)
    try:
        model, _ = check_model(body)
    except ModelError as e:
        raise HTTPException(422, str(e)) from e
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_model(model))
    return {"ok": True, "path": rel}


@app.post("/api/validate")
def validate(body: dict[str, Any]) -> dict[str, Any]:
    try:
        _, sim = check_model(body)
    except ModelError as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "ports": {k: {"inputs": i, "outputs": o} for k, (i, o) in sim.ports.items()},
        "groups": {
            g.id: {"inputs": list(g.inputs), "outputs": list(g.outputs), "mode": g.mode}
            for g in sim.model.groups
        },
    }


CALIBRATE_MAX_EVENTS = 2_000_000


@app.post("/api/calibrate")
def calibrate(body: dict[str, Any]) -> dict[str, Any]:
    """Run the model with one group expanded and propose an abstract stand-in for it.

    Body: {"model": {...}, "group": id, "until": float, "seed": int, "kind": auto|server|delay}
    """
    req = CalibrationRequest(
        group=body.get("group"),
        until=float(body.get("until", 1000.0)),
        seed=int(body.get("seed", 0)),
        kind=body.get("kind", "auto"),
    )
    try:
        model, _ = check_model(body.get("model"))
        sim, m, abstract = run_calibration(model, req, load_library(), CALIBRATE_MAX_EVENTS)
    except (ModelError, ValidationError, CompositeError) as e:
        raise HTTPException(422, str(e)) from e
    return {
        "group": req.group,
        "measured_until": sim.now,
        "events": sim.events_processed,
        "truncated": sim.now < req.until,
        "probe": m.probe,
        "abstract": abstract,
    }


# --- scenarios and batch runs (M6) ---------------------------------------------

BATCH_MAX_RUNS = 200
RUN_MAX_EVENTS = 2_000_000
_store: RunStore | None = None
_tasks: set[asyncio.Task] = set()  # keep background batches referenced


def store() -> RunStore:
    global _store
    if _store is None or _store.path != RUNS_DB:
        _store = RunStore(RUNS_DB)
    return _store


def _seed_list(seeds: Any) -> list[int]:
    if isinstance(seeds, int):
        return list(range(1, seeds + 1))
    return [int(s) for s in seeds or [1]]


@app.post("/api/batches")
async def start_batch(body: dict[str, Any]) -> dict[str, Any]:
    """Run baseline and scenarios over several seeds in the background.

    Body: {"model", "scenarios": ["baseline", id, ...], "seeds": n or [..], "until", "samples"}
    """
    try:
        model, _ = check_model(body.get("model"))
        scenarios = body.get("scenarios") or [scen.BASELINE]
        for sid in scenarios:
            scen.scenario_of(model, sid)
        seeds = _seed_list(body.get("seeds", 5))
    except ModelError as e:
        raise HTTPException(422, str(e)) from e
    if len(scenarios) * len(seeds) > BATCH_MAX_RUNS:
        raise HTTPException(422, f"at most {BATCH_MAX_RUNS} runs per batch")
    params = {
        "scenarios": scenarios,
        "seeds": seeds,
        "until": float(body.get("until", 1000.0)),
        "samples": int(body.get("samples", 40)),
    }
    batch_id = store().create_batch("batch", model.model_dump(by_alias=True), params)
    task = asyncio.create_task(asyncio.to_thread(_run_batch, batch_id, model, params))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"id": batch_id, "runs": len(scenarios) * len(seeds)}


def _run_batch(batch_id: int, model: ModelSpec, params: dict[str, Any]) -> None:
    db = store()
    try:
        library = load_library()
        for sid in params["scenarios"]:
            for seed in params["seeds"]:
                sim = scen.build(model, sid, seed, library)
                result = scen.run_one(sim, params["until"], params["samples"], RUN_MAX_EVENTS)
                db.add_run(batch_id, sid, seed, result)
        db.finish(batch_id)
    except Exception as e:  # report any failure on the batch instead of losing it
        db.finish(batch_id, f"{type(e).__name__}: {e}")


@app.get("/api/batches")
def list_batches() -> list[dict[str, Any]]:
    return store().batches()


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: int, metric: str | None = None) -> dict[str, Any]:
    """A batch with per-scenario stats for every metric, and one metric's time series."""
    b = store().batch(batch_id)
    if b is None:
        raise HTTPException(404, f"no batch {batch_id}")
    runs = b.pop("runs_detail")
    by_scenario = _group_runs(runs)
    b["aggregate"] = {
        sid: scen.aggregate([r["flat"] for r in rs]) for sid, rs in by_scenario.items()
    }
    b["metrics"] = sorted({p for r in runs for p in r["flat"]})
    b["truncated"] = any(r["truncated"] for r in runs)
    if metric:
        b["series"] = _series(by_scenario, metric)
    return b


def _group_runs(runs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        out.setdefault(r["scenario"], []).append(r)
    return out


def _series(by_scenario: dict[str, list[dict[str, Any]]], metric: str) -> dict[str, list]:
    return {
        sid: scen.mean_series([r["series"] for r in rs], metric) for sid, rs in by_scenario.items()
    }


# --- live runs ----------------------------------------------------------------

FPS = 30
MAX_EVENTS_PER_FRAME = 20_000
MAX_RECORDS_PER_FRAME = 400
SUMMARY_EVERY = 10  # frames
STEP_MAX_EVENTS = 100_000  # a step runs internal events until a message is delivered
SPEED_TRIAL_EVENTS = 5_000
DELIVERIES_PER_SECOND = 8  # what the suggested speed aims for on screen
SPEED_MIN, SPEED_MAX = 0.01, 1000.0  # the speed slider's range


def suggested_speed(sim: Simulation) -> float | None:
    """A play speed that shows about DELIVERIES_PER_SECOND message deliveries per second.

    Measured on a fork of the fresh run, so the live run is untouched. None when no
    message is delivered in the trial (the default speed then stays).
    """
    trial = sim.fork()
    delivered = 0

    def count(record: dict[str, Any]) -> None:
        nonlocal delivered
        delivered += record["type"] == "deliver"

    trial.add_listener(count)
    trial.run(max_events=SPEED_TRIAL_EVENTS)
    if delivered == 0 or trial.now <= 0:
        return None
    speed = DELIVERIES_PER_SECOND * trial.now / delivered
    return float(f"{min(max(speed, SPEED_MIN), SPEED_MAX):.2g}")


class RunSession:
    """One simulation driven by one browser over a WebSocket.

    Client -> server: {"cmd": "load", "model": {...}, "seed": int}
                      {"cmd": "play"} | {"cmd": "pause"} | {"cmd": "step"}
                      {"cmd": "reset"} | {"cmd": "speed", "value": float}
    Server -> client: {"type": "loaded", "ports": {...}, "suggested_speed": float | None}
                      {"type": "frame", "now", "playing", "events", "lagging", "step",
                       "records": [...], "dropped_records": int, "summary"?: {...}}
                      {"type": "error", "message": str}
    ``speed`` is simulated time units per real second. ``step`` runs until the next
    message is delivered (internal events such as timers run on the way).
    """

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.model: ModelSpec | None = None
        self.library: Library | None = None
        self.seed = 0
        self.sim: Simulation | None = None
        self.playing = False
        self.speed = 1.0
        self.records: list[dict[str, Any]] = []
        self.dropped_records = 0
        self.frame_no = 0
        self.delivered = 0  # deliveries since the current step began

    def _collect(self, record: dict[str, Any]) -> None:
        self.delivered += record["type"] == "deliver"
        if len(self.records) < MAX_RECORDS_PER_FRAME:
            self.records.append(record)
        else:
            self.dropped_records += 1

    def _build(self) -> None:
        assert self.model is not None
        self.sim = Simulation(self.model, seed=self.seed, library=self.library)
        self.sim.add_listener(self._collect)
        self.records.clear()
        self.playing = False

    async def handle(self, msg: dict[str, Any]) -> None:
        cmd = msg.get("cmd")
        if cmd == "load":
            return await self.load(msg)
        if cmd == "speed":  # a setting, valid before any model is loaded
            self.speed = max(1e-6, float(msg.get("value", 1.0)))
            return
        if self.sim is None:
            return await self.error("load a model first")
        handler = {
            "play": self.play,
            "pause": self.pause,
            "step": self.step,
            "reset": self.reset,
            "whatif": self.whatif,
        }.get(cmd)
        if handler is None:
            return await self.error(f"unknown cmd {cmd!r}")
        await handler(msg)

    async def error(self, message: str) -> None:
        await self.ws.send_json({"type": "error", "message": message})

    async def load(self, msg: dict[str, Any]) -> None:
        try:
            self.library = load_library()  # the run keeps the types it was loaded with
            self.model, sim = check_model(msg.get("model"), self.library)
        except ModelError as e:
            return await self.error(str(e))
        except HTTPException as e:
            return await self.error(e.detail)
        self.seed = int(msg.get("seed", 0))
        self._build()
        ports = {k: {"inputs": i, "outputs": o} for k, (i, o) in sim.ports.items()}
        speed = suggested_speed(sim)
        await self.ws.send_json({"type": "loaded", "ports": ports, "suggested_speed": speed})
        await self.send_frame(summary=True)

    async def play(self, msg: dict[str, Any]) -> None:
        self.playing = True

    async def pause(self, msg: dict[str, Any]) -> None:
        self.playing = False
        await self.send_frame(summary=True)

    async def step(self, msg: dict[str, Any]) -> None:
        self.playing = False
        self.delivered = 0
        for _ in range(STEP_MAX_EVENTS):
            if self.sim.step() is None or self.delivered:
                break
        await self.send_frame(summary=True, step=True)

    async def reset(self, msg: dict[str, Any]) -> None:
        self._build()
        await self.send_frame(summary=True)

    async def whatif(self, msg: dict[str, Any]) -> None:
        """Fork the paused run: continue it as is and with a scenario, side by side."""
        self.playing = False
        try:
            scenario = scen.scenario_of(self.model, msg.get("scenario"))
            if scenario is None:
                raise ModelError("pick a scenario to compare with")
            horizon = float(msg.get("horizon", 100.0))
            start = self.sim.now
            result = await asyncio.to_thread(scen.whatif, self.sim, scenario, horizon)
        except (ModelError, ValueError) as e:
            await self.ws.send_json({"type": "error", "message": f"what-if: {e}"})
            return
        params = {"scenarios": [scen.BASELINE, scenario.id], "from": start, "horizon": horizon}
        batch_id = store().create_batch("whatif", self.model.model_dump(by_alias=True), params)
        for sid in (scen.BASELINE, scenario.id):
            store().add_run(batch_id, sid, self.seed, result[sid])
        store().finish(batch_id)
        await self.ws.send_json({"type": "whatif", "batch": batch_id})

    async def send_frame(
        self, summary: bool = False, lagging: bool = False, step: bool = False
    ) -> None:
        sim = self.sim
        assert sim is not None
        frame: dict[str, Any] = {
            "type": "frame",
            "now": sim.now,
            "playing": self.playing,
            "events": sim.events_processed,
            "pending": sim.pending,
            "lagging": lagging,
            "step": step,  # the answer to a step: the client animates its deliveries
            "records": self.records,
            "dropped_records": self.dropped_records,
        }
        if summary:
            frame["summary"] = sim.summary()
        await self.ws.send_json(frame)
        self.records = []
        self.dropped_records = 0

    async def tick_loop(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(1 / FPS)
            now = time.monotonic()
            dt, last = now - last, now
            if not (self.playing and self.sim):
                continue
            target = self.sim.now + self.speed * dt
            n = self.sim.run(until=target, max_events=MAX_EVENTS_PER_FRAME)
            lagging = n >= MAX_EVENTS_PER_FRAME
            if self.sim.next_time is None:
                self.playing = False  # nothing left to happen
            self.frame_no += 1
            want_summary = self.frame_no % SUMMARY_EVERY == 0 or not self.playing
            await self.send_frame(summary=want_summary, lagging=lagging)

    async def recv_loop(self) -> None:
        while True:
            await self.handle(await self.ws.receive_json())


@app.websocket("/ws/run")
async def run_socket(ws: WebSocket) -> None:
    await ws.accept()
    session = RunSession(ws)
    ticker = asyncio.create_task(session.tick_loop())
    try:
        await session.recv_loop()
    except WebSocketDisconnect:
        pass
    finally:
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker


# --- the agent (M7) ---------------------------------------------------------------

REMOVED_KEYS = agent.strip_api_keys()  # the agent must run on the subscription login


def _agent_env(workspace: agent.Workspace, send) -> agent.Env:
    def start_batch(model: ModelSpec, scenarios: list[str], seeds: list[int], until: float) -> int:
        params = {"scenarios": scenarios, "seeds": seeds, "until": until, "samples": 40}
        batch_id = store().create_batch("batch", model.model_dump(by_alias=True), params)
        _run_batch(batch_id, model, params)  # the tool calls this in a worker thread
        return batch_id

    return agent.Env(
        workspace=workspace,
        load_library=lambda: Library.load(LIBRARIES),  # library errors go back to the agent
        libraries_dir=LIBRARIES,
        start_batch=start_batch,
        batch_result=get_batch,
        notify=send,
        sources=sources(),
    )


def sources() -> SourceRegistry:
    return SourceRegistry(LOCAL / "sources.json")


@app.get("/api/sources")
def list_sources() -> list[dict[str, str]]:
    return [{"name": s.name, "path": str(s.path)} for s in sources().all()]


@app.post("/api/sources")
def add_source(body: dict[str, Any]) -> dict[str, str]:
    try:
        s = sources().add(str(body.get("name", "")), str(body.get("path", "")))
    except SourceError as e:
        raise HTTPException(422, str(e)) from e
    return {"name": s.name, "path": str(s.path)}


@app.delete("/api/sources/{name}")
def remove_source(name: str) -> dict[str, bool]:
    sources().remove(name)
    return {"ok": True}


@app.get("/api/sources/snippet")
def source_snippet(ref: str) -> dict[str, Any]:
    """The code a provenance reference points to (only inside registered sources)."""
    try:
        return sources().snippet(ref)
    except SourceError as e:
        raise HTTPException(422, str(e)) from e


@app.get("/api/agent/status")
def agent_status() -> dict[str, Any]:
    from claude_agent_sdk import __version__ as sdk_version

    return {
        "sdk": sdk_version,
        "model": os.environ.get("SIMBENCH_AGENT_MODEL") or "default for your login",
        "api_keys_removed": REMOVED_KEYS,
        "tools": [n.rsplit("__", 1)[-1] for n in agent.tool_names()],
    }


class AgentChat:
    """One browser chat. Client: {"cmd": "prompt", "text", "model"} | interrupt | reset.
    Server: text, tool, tool_result, model, batch, done and error events."""

    def __init__(self, ws: WebSocket) -> None:
        self.send = ws.send_json
        self.workspace = agent.Workspace({}, lambda m: check_model(m))
        self.session = self.new_session()
        self.task: asyncio.Task | None = None

    def new_session(self) -> agent.AgentSession:
        env = _agent_env(self.workspace, self.send)
        return agent.AgentSession(env, self.send, workdir=LOCAL / "agent-cwd")

    async def handle(self, msg: dict[str, Any]) -> None:
        cmd = msg.get("cmd")
        if cmd == "prompt":
            await self.prompt(str(msg.get("text", "")), msg.get("model") or {})
        elif cmd == "interrupt":
            await self.session.interrupt()
        elif cmd == "reset":
            await self.session.close()
            self.session = self.new_session()
        elif cmd in ("accept", "reject"):
            await self.resolve(cmd == "accept")

    async def resolve(self, accepted: bool) -> None:
        """The user's answer to the agent's pending proposal."""
        p = self.workspace.proposal
        if p is None:
            return await self.send({"type": "error", "message": "there is no pending proposal"})
        if accepted:
            try:
                await asyncio.to_thread(agent.write_types, LIBRARIES, p["types"])
            except Exception as e:  # keep the proposal so the user can retry or reject
                return await self.send({"type": "error", "message": f"could not write types: {e}"})
            self.workspace.adopt(p["model"])
            await self.send({"type": "model", "model": self.workspace.model})
        self.workspace.proposal = None
        verdict = "accepted" if accepted else "rejected"
        self.workspace.notes.append(f"The user {verdict} your proposal: {p['summary']}")
        await self.send({"type": "proposal_resolved", "accepted": accepted})

    async def prompt(self, text: str, model: dict[str, Any]) -> None:
        if self.task and not self.task.done():
            await self.send({"type": "error", "message": "still working on the last request"})
            return
        self.task = asyncio.create_task(self.ask(text, model))  # so interrupts get through

    async def ask(self, text: str, model: dict[str, Any]) -> None:
        try:
            await self.session.ask(text, model)
        except Exception as e:  # report it in the chat (e.g. the CLI is not logged in)
            log.exception("agent prompt failed")
            await self.send({"type": "error", "message": f"{type(e).__name__}: {e}"})
            await self.send({"type": "done", "error": True})

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
        await self.session.close()


@app.websocket("/ws/agent")
async def agent_socket(ws: WebSocket) -> None:
    await ws.accept()
    chat = AgentChat(ws)
    try:
        while True:
            await chat.handle(await ws.receive_json())
    except WebSocketDisconnect:
        pass
    finally:
        await chat.close()


# --- static frontend ----------------------------------------------------------


# The browser revalidates every file (a cheap 304 when unchanged), so after an update it
# never keeps running stale JS modules; without this it guesses a cache lifetime.
NO_CACHE = {"Cache-Control": "no-cache"}


class FreshStaticFiles(StaticFiles):
    def file_response(self, *args: Any, **kwargs: Any):
        response = super().file_response(*args, **kwargs)
        response.headers.update(NO_CACHE)
        return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html", headers=NO_CACHE)


app.mount("/", FreshStaticFiles(directory=WEB), name="web")
