"""The workbench tools the agent can call (an in-process MCP server).

Every tool returns short text the model can read. Mutating tools go through
``Workspace.change`` so an invalid result is rejected and reported, never kept.
Simulations run in a worker thread so the server stays responsive.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from claude_agent_sdk import create_sdk_mcp_server, tool

from ..kernel import Library, LibraryError, ModelError, ModelSpec, Simulation, run_calibration
from ..kernel import scenario as scen
from ..kernel.calibrate import Request as CalibrationRequest
from ..kernel.yamlio import load_yaml
from ..sources import SourceRegistry
from .workspace import ToolError, Workspace, link_key

SERVER_NAME = "workbench"
MAX_EVENTS = 2_000_000


@dataclass
class Env:
    """What the tools need from the server around them."""

    workspace: Workspace
    load_library: Callable[[], Library]
    libraries_dir: Path
    start_batch: Callable[[ModelSpec, list[str], list[int], float], int]  # -> batch id, blocking
    batch_result: Callable[[int, str | None], dict[str, Any]]
    notify: Callable[[dict[str, Any]], Awaitable[None]]  # push an event to the browser
    sources: SourceRegistry


def ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}


def obj(props: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    """A JSON-schema object (a short way to write tool input schemas)."""
    return {"type": "object", "properties": props, "required": list(required)}


STR = {"type": "string"}
NUM = {"type": "number"}
ANY_OBJ = {"type": "object"}
LINK_PROPS = {
    "latency": {"description": "number or distribution, e.g. {dist: exp, mean: 0.01}"},
    "loss": NUM,
    "bandwidth": NUM,
    "ordered": {"type": "boolean"},
    "up": {"type": "boolean"},
}


def build_server(env: Env):
    """The MCP server config, with every tool bound to ``env``."""
    tools = [make(env) for make in TOOL_FACTORIES]
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)


def tool_names() -> list[str]:
    return [f"mcp__{SERVER_NAME}__{name}" for name in TOOL_NAMES]


def guarded(env: Env, fn: Callable[[dict[str, Any]], Awaitable[str]], changes: bool):
    """Run a tool body; turn known errors into tool errors; push model changes."""

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            text = await fn(args)
        except (ToolError, ModelError, LibraryError, ValueError, KeyError) as e:
            return err(str(e))
        if changes:
            await env.notify({"type": "model", "model": env.workspace.model})
        return ok(text)

    return handler


# --- reading ---------------------------------------------------------------------------


def make_get_model(env: Env):
    @tool(
        "get_model",
        "The current model as YAML (entities, connections, groups, scenarios).",
        obj({}),
    )
    async def get_model(args):
        return ok(_yaml(env.workspace.model))

    return get_model


def make_list_types(env: Env):
    async def body(args):
        lib = env.load_library()
        lines = ["Item types (id: base; inputs -> outputs; params with defaults):"]
        for tid in lib.all_type_ids():
            r = lib.resolve(tid)
            ins, outs = r.behavior.ports(r.defaults)
            params = ", ".join(
                f"{k}={json.dumps(v)}" for k, v in r.defaults.items() if k != "machine"
            )
            lines.append(f"- {tid}: {r.chain[-1]}; {ins} -> {outs}; {params}  # {r.description}")
        lines.append("Templates (id: args):")
        for t in lib.templates.values():
            args_ = ", ".join(f"{k}={m.get('default')!r}" for k, m in t.params.items())
            lines.append(f"- {t.id}: {args_}  # {t.description}")
        return "\n".join(lines)

    return tool(
        "list_types",
        "Item types and templates you can use, with ports and default params.",
        obj({}),
    )(guarded(env, body, changes=False))


# --- editing entities and links -----------------------------------------------------------


def make_add_entity(env: Env):
    async def body(a):
        def change(m):
            if any(e["id"] == a["id"] for e in m["entities"]):
                raise ToolError(f"entity {a['id']!r} already exists")
            e = {"id": a["id"], "type": a["type"], "params": a.get("params") or {}}
            if a.get("name"):
                e["name"] = a["name"]
            m["entities"].append(e)

        env.workspace.change(change)
        return f"added {a['id']} ({a['type']})"

    schema = obj({"id": STR, "type": STR, "params": ANY_OBJ, "name": STR}, ("id", "type"))
    return tool(
        "add_entity", "Add an entity of an item type (see list_types). Ids: short, no dots.", schema
    )(guarded(env, body, changes=True))


def make_update_entity(env: Env):
    async def body(a):
        def change(m):
            e = env.workspace.entity(m, a["id"])
            _merge_params(e["params"], a.get("params") or {})
            e.update({f: a[f] for f in ("type", "name") if a.get(f)})

        env.workspace.change(change)
        return f"updated {a['id']}"

    schema = obj({"id": STR, "params": ANY_OBJ, "type": STR, "name": STR}, ("id",))
    desc = "Change an entity's params (merged; a null value restores the default), type or label."
    return tool("update_entity", desc, schema)(guarded(env, body, changes=True))


def make_remove_entity(env: Env):
    async def body(a):
        def change(m):
            env.workspace.entity(m, a["id"])
            m["entities"] = [e for e in m["entities"] if e["id"] != a["id"]]
            m["connections"] = [c for c in m["connections"] if a["id"] not in _ends(c)]

        env.workspace.change(change)
        return f"removed {a['id']} and its connections"

    return tool(
        "remove_entity",
        "Remove an entity and every connection touching it.",
        obj({"id": STR}, ("id",)),
    )(guarded(env, body, changes=True))


def make_connect(env: Env):
    async def body(a):
        def change(m):
            m["connections"] = [c for c in m["connections"] if c["from"] != a["from"]]
            c = {"from": a["from"], "to": a["to"]}
            c.update({k: a[k] for k in LINK_PROPS if k in a})
            m["connections"].append(c)

        env.workspace.change(change)
        return (
            f"connected {a['from']} -> {a['to']} (an output has one link; any old one was replaced)"
        )

    schema = obj({"from": STR, "to": STR, **LINK_PROPS}, ("from", "to"))
    desc = "Connect an output port to an input port: from 'entity.port' to 'entity.port'."
    return tool("connect", desc, schema)(guarded(env, body, changes=True))


def make_update_link(env: Env):
    async def body(a):
        env.workspace.change(lambda m: env.workspace.connection(m, a["link"]).update(
            {k: a[k] for k in LINK_PROPS if k in a}
        ))  # fmt: skip
        return f"updated link {a['link']}"

    schema = obj({"link": STR, **LINK_PROPS}, ("link",))
    return tool(
        "update_link", "Change a connection's latency, loss, bandwidth, ordered or up.", schema
    )(guarded(env, body, changes=True))


def make_disconnect(env: Env):
    async def body(a):
        def change(m):
            env.workspace.connection(m, a["link"])
            m["connections"] = [c for c in m["connections"] if link_key(c) != a["link"]]

        env.workspace.change(change)
        return f"removed link {a['link']}"

    return tool(
        "disconnect", "Remove a connection by its key 'a.out->b.in'.", obj({"link": STR}, ("link",))
    )(guarded(env, body, changes=True))


def make_replace_model(env: Env):
    async def body(a):
        data = load_yaml(a["yaml"])
        if not isinstance(data, dict):
            raise ToolError("the YAML must be a mapping with entities and connections")
        env.workspace.change(lambda m: (m.clear(), m.update(data)))
        return "model replaced"

    desc = "Replace the whole model with YAML (same format as get_model). Use for big rewrites."
    return tool("replace_model", desc, obj({"yaml": STR}, ("yaml",)))(
        guarded(env, body, changes=True)
    )


def make_undo(env: Env):
    async def body(a):
        env.workspace.undo()
        return "undid the last change"

    return tool("undo", "Undo your last model change.", obj({}))(guarded(env, body, changes=True))


# --- templates, groups, calibration -------------------------------------------------------


def make_insert_template(env: Env):
    async def body(a):
        lib = env.load_library()
        frag = lib.instantiate(a["template"], a.get("args") or {}, a.get("prefix"))

        def change(m):
            if any(g["id"] == frag["group"] for g in m["groups"]):
                raise ToolError(f"group {frag['group']!r} exists; pass another prefix")
            m["entities"].extend(frag["entities"])
            m["connections"].extend(frag["connections"])
            m["groups"].append(
                {"id": frag["group"], "template": a["template"],
                 "inputs": frag["inputs"], "outputs": frag["outputs"]}
            )  # fmt: skip

        env.workspace.change(change)
        ports = f"inputs {frag['inputs']}, outputs {frag['outputs']}"
        g = frag["group"]
        return f"inserted {a['template']} as group {g}: {ports}. Connect to '{g}.<port>'."

    schema = obj({"template": STR, "args": ANY_OBJ, "prefix": STR}, ("template",))
    return tool(
        "insert_template", "Insert a template as a composite group (see list_types).", schema
    )(guarded(env, body, changes=True))


def make_set_group(env: Env):
    async def body(a):
        def change(m):
            g = env.workspace.group(m, a["group"])
            if a.get("abstract_type"):
                g["abstract"] = {
                    "type": a["abstract_type"],
                    "params": a.get("abstract_params") or {},
                }
            if a.get("mode"):
                g["mode"] = a["mode"]

        env.workspace.change(change)
        return f"group {a['group']} updated"

    schema = obj(
        {"group": STR, "mode": {"enum": ["expanded", "abstract"]}, "abstract_type": STR,
         "abstract_params": ANY_OBJ},
        ("group",),
    )  # fmt: skip
    desc = (
        "Set a group's mode, or its abstract stand-in (type + params). Abstract needs a stand-in."
    )
    return tool("set_group", desc, schema)(guarded(env, body, changes=True))


def make_calibrate_group(env: Env):
    async def body(a):
        model = ModelSpec.model_validate(env.workspace.model)
        req = CalibrationRequest(a["group"], float(a.get("until", 1000)), int(a.get("seed", 1)),
                                 a.get("kind", "auto"))  # fmt: skip
        _, m, abstract = await asyncio.to_thread(
            run_calibration, model, req, env.load_library(), MAX_EVENTS
        )
        env.workspace.change(
            lambda md: env.workspace.group(md, req.group).update({"abstract": abstract})
        )
        return calibration_text(req.group, m.probe, abstract)

    schema = obj(
        {"group": STR, "until": NUM, "seed": {"type": "integer"},
         "kind": {"enum": ["auto", "server", "delay"]}},
        ("group",),
    )  # fmt: skip
    desc = (
        "Run the model with a group expanded and fit its abstract stand-in from the measurements. "
        "kind 'server' (default when the group has servers) saturates at the group's measured "
        "capacity; 'delay' reproduces latency at this load only."
    )
    return tool("calibrate_group", desc, schema)(guarded(env, body, changes=True))


def calibration_text(gid: str, probe: dict, abstract: dict) -> str:
    r, cal = probe["residence"], abstract["calibration"]
    head = (f"calibrated {gid}: residence mean {r['mean']:.4g} (p95 {r.get('p95') or 0:.4g}), "
            f"loss {probe['loss']:.3g}, arrival rate {probe['arrival_rate']:.4g}. ")  # fmt: skip
    stand_in = f"Stand-in {abstract['type']}: {json.dumps(abstract['params'])}. "
    if abstract["type"] == "server":
        return (
            head
            + stand_in
            + (
                f"Capacity {cal['capacity']:.4g}/s (bottleneck {cal['bottleneck']}, "
                f"{cal['bottleneck_utilization']:.0%} busy). {cal['note']}."
            )
        )
    return head + stand_in + "No capacity limit: valid near this load only."


# --- types ------------------------------------------------------------------------------------


def make_define_type(env: Env):
    async def body(a):
        created = write_types(env.libraries_dir, [a])
        return f"{'created' if created else 'updated'} type {a['library']}/{a['name']}"

    schema = obj(
        {"library": STR, "name": STR, "extends": STR, "description": STR, "icon": STR,
         "params": ANY_OBJ, "machine": ANY_OBJ, "source": {"type": "array", "items": STR}},
        ("library", "name", "extends"),
    )  # fmt: skip
    desc = (
        "Create or update a reusable item type as YAML in libraries/<library>/types/. "
        "extends: a built-in (source, server, delay, router, sink, machine) or another type. "
        "For custom logic extend 'machine' and pass machine: {inputs, outputs, vars, initial, "
        "states: {s: {enter: [...], on: {port_or_event: [actions]}}}}; actions: send, emit, drop, "
        "set, goto, after (+event), if/then/else, count, observe, push, pop, release. "
        "Expressions are Python-like over vars, params, msg, now. You cannot write Python."
    )
    return tool("define_type", desc, schema)(guarded(env, body, changes=False))


# --- scenarios and runs -------------------------------------------------------------------------


def make_set_scenario(env: Env):
    async def body(a):
        def change(m):
            m["scenarios"] = [s for s in m["scenarios"] if s["id"] != a["id"]]
            if not a.get("remove"):
                m["scenarios"].append(
                    {"id": a["id"], "name": a.get("name"), "patches": a.get("patches") or []}
                )

        env.workspace.change(change)
        return f"scenario {a['id']} {'removed' if a.get('remove') else 'saved'}"

    schema = obj(
        {"id": STR, "name": STR, "remove": {"type": "boolean"},
         "patches": {"type": "array", "items": ANY_OBJ}},
        ("id",),
    )  # fmt: skip
    desc = (
        "Add, replace or remove a what-if scenario. Patches: {entity, param, value} | "
        "{link, field: latency|loss|bandwidth|ordered|up, value} | {group, mode}; add 'at': t to "
        "apply one during the run (fault injection)."
    )
    return tool("set_scenario", desc, schema)(guarded(env, body, changes=True))


def make_run(env: Env):
    async def body(a):
        model = ModelSpec.model_validate(env.workspace.model)
        sim = scen.build(model, a.get("scenario"), int(a.get("seed", 1)), env.load_library())
        result = await asyncio.to_thread(
            scen.run_one, sim, float(a.get("until", 1000)), 1, MAX_EVENTS
        )
        note = " (stopped at the event cap)" if result["truncated"] else ""
        return f"t={sim.now:.6g}, {result['events']} events{note}\n" + brief(result["summary"])

    schema = obj({"until": NUM, "seed": {"type": "integer"}, "scenario": STR})
    desc = "Run the model (optionally a scenario) headless and get its key metrics."
    return tool("run", desc, schema)(guarded(env, body, changes=False))


def make_run_batch(env: Env):
    async def body(a):
        model = ModelSpec.model_validate(env.workspace.model)
        scenarios = a.get("scenarios") or [scen.BASELINE] + [s.id for s in model.scenarios]
        seeds = list(range(1, int(a.get("seeds", 5)) + 1))
        batch_id = await asyncio.to_thread(
            env.start_batch, model, scenarios, seeds, float(a.get("until", 1000))
        )
        await env.notify({"type": "batch", "id": batch_id})
        return compare(env.batch_result(batch_id, None), a.get("metrics"))

    schema = obj(
        {"scenarios": {"type": "array", "items": STR}, "seeds": {"type": "integer"}, "until": NUM,
         "metrics": {"type": "array", "items": STR}},
    )  # fmt: skip
    desc = (
        "Compare baseline and scenarios over several seeds: mean ± 95% CI per metric. The user "
        "sees the charts. Metric paths look like entities.<id>.latency.mean (default: latency and "
        "throughput at sinks, server utilization)."
    )
    return tool("run_batch", desc, schema)(guarded(env, body, changes=False))


# --- reading other projects (M8) ---------------------------------------------------------------


def make_list_sources(env: Env):
    async def body(a):
        sources = env.sources.all()
        if not sources:
            return "No source projects are registered. The user can add them in the Agent tab."
        lines = [f"- {s.name}: {s.path}" for s in sources]
        return (
            "Registered sources (read-only; refer to code as name:relative/path:line):\n"
            + "\n".join(lines)
        )

    return tool("list_sources", "The other projects you may read (Read/Grep/Glob).", obj({}))(
        guarded(env, body, changes=False)
    )


def make_propose_changes(env: Env):
    async def body(a):
        data = load_yaml(a["model_yaml"])
        if not isinstance(data, dict):
            raise ToolError("model_yaml must be a mapping with entities and connections")
        types = a.get("types") or []
        model = await asyncio.to_thread(_validate_proposal, env.libraries_dir, data, types)
        env.workspace.proposal = {
            "summary": a["summary"],
            "notes": a.get("notes", ""),
            "model": model.model_dump(by_alias=True, exclude_none=True),
            "types": types,
        }
        await env.notify({"type": "proposal", "proposal": env.workspace.proposal})
        return (
            "The proposal is on the user's canvas as a diff. Stop now and wait: they will accept "
            "or reject it, and you will be told which with their next message."
        )

    schema = obj(
        {"summary": STR, "notes": STR, "model_yaml": STR,
         "types": {"type": "array", "items": ANY_OBJ}},
        ("summary", "model_yaml"),
    )  # fmt: skip
    desc = (
        "Propose a model built from source code, for the user to review: the full proposed model "
        "as YAML (entities may carry source: ['name:path:line-line']) plus any new item types "
        "(each {library, name, extends, ..., source}). Nothing is applied until the user accepts. "
        "notes: your assumptions and open questions."
    )
    return tool("propose_changes", desc, schema)(guarded(env, body, changes=False))


def _validate_proposal(libraries_dir: Path, data: dict[str, Any], types: list[dict]) -> ModelSpec:
    """Check the model against a scratch copy of the libraries with the new types added."""
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "libraries"
        shutil.copytree(libraries_dir, scratch)
        write_types(scratch, types)
        model = ModelSpec.model_validate(data)
        Simulation(model, library=Library.load(scratch))
    return model


# --- text reports ------------------------------------------------------------------------------


TYPE_KEYS = ("name", "extends", "description", "icon", "params", "machine", "source")


def write_types(libraries_dir: Path, types: list[dict[str, Any]]) -> int:
    """Write YAML item types into their libraries; undo them all if the library breaks.

    Returns how many were new. Library and type names must be safe file names.
    """
    backups: dict[Path, str | None] = {}
    try:
        for t in types:
            path = _type_path(libraries_dir, t)
            backups.setdefault(path, path.read_text() if path.exists() else None)
            path.parent.mkdir(exist_ok=True)
            path.write_text(yaml.safe_dump({k: t[k] for k in TYPE_KEYS if k in t}, sort_keys=False))
        Library.load(libraries_dir)
    except Exception:
        for path, old in backups.items():
            path.write_text(old) if old is not None else path.unlink(missing_ok=True)
        raise
    return sum(old is None for old in backups.values())


def _type_path(libraries_dir: Path, t: dict[str, Any]) -> Path:
    lib_dir = libraries_dir / _safe_name(t.get("library", ""), "library")
    if not (lib_dir / "library.yaml").exists():
        raise ToolError(f"no library {t.get('library')!r}")
    if not t.get("extends"):
        raise ToolError(f"type {t.get('name')!r} needs 'extends'")
    return lib_dir / "types" / f"{_safe_name(t.get('name', ''), 'type name')}.yaml"


def _merge_params(params: dict[str, Any], changes: dict[str, Any]) -> None:
    """Apply param changes; a None value removes the param (back to the type default)."""
    for k, v in changes.items():
        params.pop(k, None) if v is None else params.__setitem__(k, v)


_SAFE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")


def _safe_name(value: str, what: str) -> str:
    if not _SAFE_NAME.match(value or ""):
        raise ToolError(f"{what} {value!r} must be lowercase letters, digits and '-'")
    return value


def _yaml(model: dict[str, Any]) -> str:
    return yaml.safe_dump(model, sort_keys=False, default_flow_style=None, width=110)


def _ends(c: dict[str, Any]) -> tuple[str, str]:
    return c["from"].rpartition(".")[0], c["to"].rpartition(".")[0]


def brief(summary: dict[str, Any]) -> str:
    """Key numbers per entity, link and group, a line each."""
    lines = [
        f"{eid} ({s.get('item_type', s.get('type'))}): {_brief_metrics(s)}"
        for eid, s in summary["entities"].items()
    ]
    for key, m in summary["links"].items():
        if m.get("lost", {}).get("value"):
            lines.append(f"link {key}: lost {m['lost']['value']:.0f} of {m['sent']['value']:.0f}")
    for gid, g in summary.get("groups", {}).items():
        lines.append(
            f"group {gid}: residence mean {g['residence'].get('mean', 0):.4g}, loss {g['loss']:.3g}"
        )
    return "\n".join(lines)


def _brief_metrics(s: dict[str, Any]) -> str:
    parts = []
    for name, m in s.items():
        if not isinstance(m, dict) or "kind" not in m:
            if isinstance(m, (int, float, str)) and name not in ("type", "item_type"):
                parts.append(f"{name}={_num(m)}")
            continue
        parts.append(_metric(name, m))
    return ", ".join(p for p in parts if p)


def _metric(name: str, m: dict[str, Any]) -> str:
    if m["kind"] == "counter":
        return f"{name}={m['value']:.0f}" if m["value"] else ""
    if m["kind"] == "gauge":
        return f"{name} avg={m['avg']:.3g}"
    return f"{name} mean={m['mean']:.4g} p95={m['p95']:.4g}" if m.get("count") else ""


def _num(v: Any) -> str:
    return f"{v:.4g}" if isinstance(v, float) else str(v)


def compare(batch: dict[str, Any], metrics: list[str] | None) -> str:
    """mean ± CI per scenario for a few metrics, as text."""
    agg = batch["aggregate"]
    paths = metrics or _default_metrics(batch["metrics"])
    lines = [f"batch {batch['id']}: {batch['runs']} runs, status {batch['status']}"]
    for p in paths:
        lines.append(p)
        for sid in batch["params"]["scenarios"]:
            st = agg.get(sid, {}).get(p)
            if st:
                lines.append(f"  {sid}: {st['mean']:.4g} ± {st['ci95']:.2g} (n={st['n']})")
    return "\n".join(lines)


def _default_metrics(paths: list[str]) -> list[str]:
    wanted = (".latency.mean", ".received.value", ".utilization", ".dropped.value")
    return [p for p in paths if p.endswith(wanted)][:12]


TOOL_FACTORIES = [
    make_get_model, make_list_types, make_add_entity, make_update_entity, make_remove_entity,
    make_connect, make_update_link, make_disconnect, make_replace_model, make_undo,
    make_insert_template, make_set_group, make_calibrate_group, make_define_type,
    make_set_scenario, make_run, make_run_batch, make_list_sources, make_propose_changes,
]  # fmt: skip
TOOL_NAMES = [
    "get_model", "list_types", "add_entity", "update_entity", "remove_entity", "connect",
    "update_link", "disconnect", "replace_model", "undo", "insert_template", "set_group",
    "calibrate_group", "define_type", "set_scenario", "run", "run_batch", "list_sources",
    "propose_changes",
]  # fmt: skip
