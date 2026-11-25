"""HTTP + WebSocket API for the workbench (milestones M2-M3).

REST:
  GET  /api/types                 item types (built-in + libraries) with ports and param schemas
  GET  /api/libraries             loaded libraries (name, description)
  GET  /api/templates             templates with their parameters
  POST /api/templates/{id}/instantiate   expand a template into entities + connections
  GET  /api/models                model files under projects/
  GET  /api/models/{path}         one model as JSON
  PUT  /api/models/{path}         validate and save a model as YAML
  POST /api/validate              check a model; returns ports per entity or the error

WebSocket /ws/run: one live simulation per connection. See ``RunSession``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .kernel import Library, LibraryError, ModelError, ModelSpec, Simulation

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects"
LIBRARIES = ROOT / "libraries"
WEB = ROOT / "web"

app = FastAPI(title="Simulation Workbench")


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
        out.append(
            {
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
            name = (yaml.safe_load(p.read_text()) or {}).get("name", p.stem)
        except yaml.YAMLError:
            name = f"{p.stem} (unreadable)"
        out.append({"path": p.relative_to(PROJECTS).as_posix(), "name": name})
    return out


@app.get("/api/models/{rel:path}")
def get_model(rel: str) -> dict[str, Any]:
    path = model_path(rel)
    if not path.exists():
        raise HTTPException(404, f"no model {rel}")
    model = ModelSpec.model_validate(yaml.safe_load(path.read_text()))
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
    }


# --- live runs ----------------------------------------------------------------

FPS = 30
MAX_EVENTS_PER_FRAME = 20_000
MAX_RECORDS_PER_FRAME = 400
SUMMARY_EVERY = 10  # frames


class RunSession:
    """One simulation driven by one browser over a WebSocket.

    Client -> server: {"cmd": "load", "model": {...}, "seed": int}
                      {"cmd": "play"} | {"cmd": "pause"} | {"cmd": "step"}
                      {"cmd": "reset"} | {"cmd": "speed", "value": float}
    Server -> client: {"type": "loaded", "ports": {...}}
                      {"type": "frame", "now", "playing", "events", "lagging",
                       "records": [...], "dropped_records": int, "summary"?: {...}}
                      {"type": "error", "message": str}
    ``speed`` is simulated time units per real second.
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

    def _collect(self, record: dict[str, Any]) -> None:
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
            try:
                self.library = load_library()  # the run keeps the types it was loaded with
                self.model, sim = check_model(msg.get("model"), self.library)
            except ModelError as e:
                await self.ws.send_json({"type": "error", "message": str(e)})
                return
            except HTTPException as e:
                await self.ws.send_json({"type": "error", "message": e.detail})
                return
            self.seed = int(msg.get("seed", 0))
            self._build()
            ports = {k: {"inputs": i, "outputs": o} for k, (i, o) in sim.ports.items()}
            await self.ws.send_json({"type": "loaded", "ports": ports})
            await self.send_frame(summary=True)
            return
        if self.sim is None:
            await self.ws.send_json({"type": "error", "message": "load a model first"})
            return
        if cmd == "play":
            self.playing = True
        elif cmd == "pause":
            self.playing = False
            await self.send_frame(summary=True)
        elif cmd == "step":
            self.playing = False
            self.sim.step()
            await self.send_frame(summary=True)
        elif cmd == "reset":
            self._build()
            await self.send_frame(summary=True)
        elif cmd == "speed":
            self.speed = max(1e-6, float(msg.get("value", 1.0)))
        else:
            await self.ws.send_json({"type": "error", "message": f"unknown cmd {cmd!r}"})

    async def send_frame(self, summary: bool = False, lagging: bool = False) -> None:
        sim = self.sim
        assert sim is not None
        frame: dict[str, Any] = {
            "type": "frame",
            "now": sim.now,
            "playing": self.playing,
            "events": sim.events_processed,
            "pending": sim.pending,
            "lagging": lagging,
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


# --- static frontend ----------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


app.mount("/", StaticFiles(directory=WEB), name="web")
