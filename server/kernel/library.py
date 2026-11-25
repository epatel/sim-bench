"""Item-type libraries and templates (milestone M3).

Layout on disk::

    libraries/<lib>/library.yaml        name, description
    libraries/<lib>/types/*.yaml        item types   -> referenced as "<lib>/<name>"
    libraries/<lib>/templates/*.yaml    templates    -> referenced as "<lib>/<name>"

An item type ``extends`` a built-in behavior kind (``server``) or another type
(``web/app-server``, or just ``app-server`` inside the same library) and overrides
parameter defaults. Defaults merge down the chain; entity params win last.

A template is a parameterized sub-model. Strings may contain ``{expr}``, where
expr uses template params, the loop index ``i``, integers and ``+ - * //``.
A string that is exactly ``{name}`` is replaced by the raw value (so a
distribution dict stays a dict). ``repeat: <expr>`` on an entity or connection
emits it for i = 1..N. A mapping ``{$each: <expr>, key: ..., value: ...}``
builds a dict, e.g. router routes ``{s1: 1, s2: 1, ...}``.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from .behaviors import BEHAVIORS, Behavior
from .expr import ExprError, evaluate
from .machine import Machine
from .yamlio import load_yaml

BUILTINS: dict[str, type[Behavior]] = {**BEHAVIORS, Machine.kind: Machine}


class LibraryError(ValueError):
    """A library file, type or template is invalid."""


@dataclass
class ItemType:
    id: str  # "<lib>/<name>" or a built-in kind
    library: str  # "" for built-ins
    extends: str  # resolved parent id ("" for built-ins)
    description: str = ""
    icon: str | None = None
    params: dict[str, Any] = field(default_factory=dict)  # default overrides


@dataclass
class Template:
    id: str
    library: str
    description: str
    icon: str
    params: dict[str, dict[str, Any]]
    entities: list[dict[str, Any]]
    connections: list[dict[str, Any]]
    inputs: dict[str, str]
    outputs: dict[str, str]


@dataclass
class Resolved:
    """What the kernel needs for one item type."""

    id: str
    behavior: type[Behavior]
    defaults: dict[str, Any]
    description: str
    icon: str
    library: str
    chain: list[str]  # from this type down to the built-in kind


class Library:
    """All item types (built-in + loaded) and templates."""

    def __init__(self, behaviors: dict[str, type[Behavior]] | None = None) -> None:
        self.behaviors = dict(behaviors or BUILTINS)
        self.types: dict[str, ItemType] = {}
        self.templates: dict[str, Template] = {}
        self.libraries: dict[str, dict[str, Any]] = {}

    # --- loading ----------------------------------------------------------

    @classmethod
    def load(cls, root: str | Path, behaviors=None) -> Library:
        lib = cls(behaviors)
        root = Path(root)
        if root.is_dir():
            for d in sorted(p for p in root.iterdir() if (p / "library.yaml").is_file()):
                lib._load_dir(d)
        lib.check()
        return lib

    def _load_dir(self, d: Path) -> None:
        name = d.name
        meta = _read(d / "library.yaml")
        self.libraries[name] = {"name": name, "description": meta.get("description", "")}
        for f in sorted((d / "behaviors").glob("*.py")):
            self._load_behaviors(name, f)
        for f in sorted((d / "types").glob("*.yaml")):
            self.add_type(self._type_from(name, f, _read(f)))
        for f in sorted((d / "templates").glob("*.yaml")):
            self.add_template(_template_from(name, f.stem, _read(f), f))

    def _load_behaviors(self, lib: str, path: Path) -> None:
        for cls in _behaviors_in(_load_module(lib, path)):
            bid = f"{lib}/{cls.kind}"
            if bid in self.behaviors:
                raise LibraryError(f"{path}: duplicate behavior {bid!r}")
            self.behaviors[bid] = cls

    def _type_from(self, lib: str, path: Path, data: dict[str, Any]) -> ItemType:
        parent = data.get("extends")
        if not parent:
            raise LibraryError(f"{path}: missing 'extends'")
        if "/" not in parent and parent not in self.behaviors:
            parent = f"{lib}/{parent}"  # same-library reference
        return ItemType(
            id=f"{lib}/{data.get('name', path.stem)}",
            library=lib,
            extends=parent,
            description=data.get("description", ""),
            icon=data.get("icon"),
            params=_type_params(data),
        )

    def add_type(self, t: ItemType) -> None:
        if t.id in self.types or t.id in self.behaviors:
            raise LibraryError(f"duplicate item type {t.id!r}")
        self.types[t.id] = t

    def add_template(self, t: Template) -> None:
        if t.id in self.templates:
            raise LibraryError(f"duplicate template {t.id!r}")
        self.templates[t.id] = t

    def check(self) -> None:
        """Resolve every type once so broken chains fail at load time."""
        for tid in self.types:
            self.resolve(tid)

    # --- resolution -------------------------------------------------------

    def chain(self, type_id: str) -> list[ItemType]:
        """The library types from ``type_id`` down to (not including) its behavior."""
        out: list[ItemType] = []
        cur = type_id
        while cur not in self.behaviors:
            if cur in (t.id for t in out):
                path = " -> ".join([t.id for t in out] + [cur])
                raise LibraryError(f"type {type_id!r}: 'extends' cycle {path}")
            t = self.types.get(cur)
            if t is None:
                where = f" (extended by {out[-1].id!r})" if out else ""
                raise LibraryError(f"unknown type {cur!r}{where}")
            out.append(t)
            cur = t.extends
        return out

    def resolve(self, type_id: str) -> Resolved:
        types = self.chain(type_id)
        base = types[-1].extends if types else type_id
        behavior = self.behaviors[base]
        defaults = {k: copy.deepcopy(v.get("default")) for k, v in behavior.params_schema.items()}
        for t in reversed(types):  # base first, most specific last
            defaults.update(copy.deepcopy(t.params))
        return Resolved(
            id=type_id,
            behavior=behavior,
            defaults=defaults,
            description=_first([t.description for t in types], behavior.description),
            icon=_first([t.icon for t in types], behavior.icon),
            library=types[0].library if types else _library_of(base),
            chain=[t.id for t in types] + [base],
        )

    def all_type_ids(self) -> list[str]:
        return list(self.behaviors) + sorted(self.types)

    # --- templates --------------------------------------------------------

    def instantiate(
        self, template_id: str, args: dict[str, Any] | None = None, prefix: str | None = None
    ) -> dict[str, Any]:
        """Expand a template into ``{"entities", "connections", "inputs", "outputs"}``.

        Entity ids are prefixed ``<prefix>_`` and every entity gets ``group: prefix``.
        """
        t = self.templates.get(template_id)
        if t is None:
            raise LibraryError(f"unknown template {template_id!r}")
        env = _bind_args(t, args or {})
        prefix = prefix or template_id.rsplit("/", 1)[-1].replace("-", "_")
        if not prefix or "." in prefix:
            raise LibraryError(f"bad prefix {prefix!r}")

        def ent_id(local: str) -> str:
            return f"{prefix}_{local}"

        def ref(r: str) -> str:
            entity, _, port = r.rpartition(".")
            if not entity:
                raise LibraryError(f"{template_id}: bad port reference {r!r}")
            return f"{ent_id(entity)}.{port}"

        entities = []
        for raw in t.entities:
            for scope in _repeat(raw, env):
                e = _subst({k: v for k, v in raw.items() if k != "repeat"}, scope)
                e["id"] = ent_id(str(e["id"]))
                e["group"] = prefix
                e.setdefault("params", {})
                entities.append(e)
        connections = []
        for raw in t.connections:
            for scope in _repeat(raw, env):
                c = _subst({k: v for k, v in raw.items() if k != "repeat"}, scope)
                c["from"], c["to"] = ref(str(c["from"])), ref(str(c["to"]))
                connections.append(c)
        return {
            "entities": entities,
            "connections": connections,
            "inputs": {k: ref(_subst(v, env)) for k, v in t.inputs.items()},
            "outputs": {k: ref(_subst(v, env)) for k, v in t.outputs.items()},
            "group": prefix,
        }


# --- template helpers ----------------------------------------------------------


# --- tier-3 Python behaviors ------------------------------------------------------
#
# Files under libraries/<lib>/behaviors/*.py are trusted project code, like any
# other source file in the repo: they are imported and run in the server process.
# They are never loaded from model files or the UI.

_MODULE_CACHE: dict[Path, tuple[int, ModuleType]] = {}


def _load_module(lib: str, path: Path) -> ModuleType:
    """Import a behavior file, reusing the module until the file changes."""
    mtime = path.stat().st_mtime_ns
    hit = _MODULE_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    # A flat name, unique per file version, kept in sys.modules: snapshots of a run
    # that uses an older version of the file can still find their classes.
    version = hashlib.sha1(f"{path.resolve()}:{mtime}".encode()).hexdigest()[:10]
    name = re.sub(r"\W", "_", f"simbench_behaviors_{lib}_{path.stem}_{version}")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        sys.modules.pop(name, None)
        raise LibraryError(f"{path}: {type(e).__name__}: {e}") from e
    _MODULE_CACHE[path] = (mtime, module)
    return module


def _is_behavior_class(obj: Any, module: ModuleType) -> bool:
    """A Behavior subclass defined in ``module`` (not imported into it) with a kind."""
    if not (isinstance(obj, type) and issubclass(obj, Behavior)):
        return False
    return obj.__module__ == module.__name__ and bool(obj.kind)


def _behaviors_in(module: ModuleType) -> list[type[Behavior]]:
    return [obj for obj in vars(module).values() if _is_behavior_class(obj, module)]


def _first(values: list[Any], default: Any) -> Any:
    return next((v for v in values if v), default)


def _library_of(behavior_id: str) -> str:
    """'web/token-bucket' -> 'web'; built-in kinds have no library."""
    return behavior_id.split("/")[0] if "/" in behavior_id else ""


def _type_params(data: dict[str, Any]) -> dict[str, Any]:
    """A type's param defaults; a top-level ``machine:`` is the ``machine`` param."""
    params = dict(data.get("params") or {})
    if "machine" in data:
        params["machine"] = data["machine"]
    return params


def _read(path: Path) -> dict[str, Any]:
    try:
        data = load_yaml(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise LibraryError(f"{path}: {e}") from e
    if not isinstance(data, dict):
        raise LibraryError(f"{path}: expected a mapping")
    return data


def _template_from(lib: str, stem: str, data: dict[str, Any], path: Path) -> Template:
    name = data.get("name", stem)
    params = data.get("params") or {}
    for pname, meta in params.items():
        if not isinstance(meta, dict) or "type" not in meta:
            raise LibraryError(f"{path}: param {pname!r} needs a 'type'")
    if not data.get("entities"):
        raise LibraryError(f"{path}: template has no entities")
    return Template(
        id=f"{lib}/{name}",
        library=lib,
        description=data.get("description", ""),
        icon=data.get("icon", "fa-layer-group"),
        params=params,
        entities=data.get("entities") or [],
        connections=data.get("connections") or [],
        inputs=data.get("inputs") or {},
        outputs=data.get("outputs") or {},
    )


def _bind_args(t: Template, args: dict[str, Any]) -> dict[str, Any]:
    unknown = set(args) - set(t.params)
    if unknown:
        raise LibraryError(f"{t.id}: unknown argument(s) {sorted(unknown)}")
    env = {}
    for name, meta in t.params.items():
        v = args.get(name, meta.get("default"))
        if v is None:
            raise LibraryError(f"{t.id}: argument {name!r} is required")
        kind = meta["type"]
        try:
            if kind == "int":
                if isinstance(v, bool) or int(v) != float(v):
                    raise ValueError
                v = int(v)
            elif kind == "float":
                v = float(v)
            elif kind == "string":
                v = str(v)
        except (TypeError, ValueError):
            raise LibraryError(f"{t.id}: argument {name!r} must be {kind}, got {v!r}") from None
        if kind == "int" and "min" in meta and v < meta["min"]:
            raise LibraryError(f"{t.id}: argument {name!r} must be >= {meta['min']}")
        env[name] = v
    return env


def _repeat(raw: dict[str, Any], env: dict[str, Any]):
    if "repeat" not in raw:
        yield env
        return
    n = _eval(str(raw["repeat"]), env)
    for i in range(1, int(n) + 1):
        yield {**env, "i": i}


_FIELD = re.compile(r"\{([^{}]+)\}")


def _eval(expr: str, env: dict[str, Any]) -> Any:
    """Evaluate a template expression (no function calls allowed)."""
    try:
        return evaluate(expr, env)
    except ExprError as e:
        raise LibraryError(str(e)) from e


def _subst(value: Any, env: dict[str, Any]) -> Any:
    if isinstance(value, str):
        whole = _FIELD.fullmatch(value)
        if whole:
            return copy.deepcopy(_eval(whole.group(1), env))
        return _FIELD.sub(lambda m: str(_eval(m.group(1), env)), value)
    if isinstance(value, list):
        return [_subst(v, env) for v in value]
    if isinstance(value, dict):
        if "$each" in value:
            n = int(_eval(str(value["$each"]), env))
            out = {}
            for i in range(1, n + 1):
                scope = {**env, "i": i}
                out[str(_subst(value["key"], scope))] = _subst(value.get("value"), scope)
            return out
        return {_subst(k, env): _subst(v, env) for k, v in value.items()}
    return value
