from pathlib import Path

import pytest
import yaml

from server.kernel import Library, LibraryError, ModelError, ModelSpec, Simulation, load_model

ROOT = Path(__file__).resolve().parent.parent
LIBS = ROOT / "libraries"


@pytest.fixture(scope="module")
def lib():
    return Library.load(LIBS)


def write(root: Path, rel: str, data: dict) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data))


def make_lib(tmp_path, types: dict[str, dict], templates: dict[str, dict] | None = None):
    write(tmp_path, "t/library.yaml", {"name": "t"})
    for name, data in types.items():
        write(tmp_path, f"t/types/{name}.yaml", data)
    for name, data in (templates or {}).items():
        write(tmp_path, f"t/templates/{name}.yaml", data)
    return Library.load(tmp_path)


# --- types ---------------------------------------------------------------------


def test_shipped_libraries_load(lib):
    assert {"web/app-server", "web/database", "queueing/mm-server"} <= set(lib.types)
    assert {"web/lb-pool", "web/cached-db", "queueing/tandem"} <= set(lib.templates)


def test_defaults_merge_down_the_chain(tmp_path):
    lib = make_lib(
        tmp_path,
        {
            "base": {"extends": "server", "params": {"servers": 4, "service": 0.5}},
            "fast": {"extends": "base", "params": {"service": 0.1}, "icon": "fa-bolt"},
        },
    )
    r = lib.resolve("t/fast")
    assert r.chain == ["t/fast", "t/base", "server"]
    assert r.defaults["servers"] == 4  # from t/base
    assert r.defaults["service"] == 0.1  # overridden by t/fast
    assert r.defaults["error_rate"] == 0.0  # built-in schema default
    assert r.icon == "fa-bolt" and r.library == "t"


def test_entity_params_override_type_defaults(lib):
    m = ModelSpec.model_validate(
        {
            "entities": [
                {"id": "a", "type": "web/app-server", "params": {"servers": 1}},
            ]
        }
    )
    sim = Simulation(m, library=lib)
    params = sim.entities["a"].ctx.params
    assert params["servers"] == 1 and params["queue_capacity"] == 50
    assert m.entities[0].params == {"servers": 1}  # the model itself is untouched


def test_router_type_ports_come_from_its_routes(lib):
    m = ModelSpec.model_validate({"entities": [{"id": "c", "type": "web/cache"}]})
    assert Simulation(m, library=lib).ports["c"] == (["in"], ["hit", "miss"])


@pytest.mark.parametrize(
    "types, message",
    [
        ({"a": {"extends": "b"}, "b": {"extends": "a"}}, "cycle"),
        ({"a": {"extends": "nowhere/x"}}, "unknown type"),
        ({"a": {"params": {}}}, "missing 'extends'"),
    ],
)
def test_broken_types_fail_at_load(tmp_path, types, message):
    with pytest.raises(LibraryError, match=message):
        make_lib(tmp_path, types)


def test_unknown_library_type_is_a_model_error(lib):
    m = ModelSpec.model_validate({"entities": [{"id": "a", "type": "web/nope"}]})
    with pytest.raises(ModelError, match="unknown type"):
        Simulation(m, library=lib)


def test_library_example_runs(lib):
    sim = Simulation(load_model(ROOT / "projects/examples/web-library.yaml"), seed=1, library=lib)
    sim.run(until=60)
    e = sim.summary()["entities"]
    assert e["done"]["received"]["value"] > 1000
    assert e["app1"]["item_type"] == "web/app-server" and e["app1"]["type"] == "server"
    hits, misses = e["cache"]["to_hit"]["value"], e["cache"]["to_miss"]["value"]
    assert hits / (hits + misses) == pytest.approx(0.85, abs=0.03)


# --- templates -------------------------------------------------------------------


def test_lb_pool_expands(lib):
    frag = lib.instantiate("web/lb-pool", {"n": 3}, prefix="pool")
    ids = [e["id"] for e in frag["entities"]]
    assert ids == ["pool_lb", "pool_s1", "pool_s2", "pool_s3", "pool_join"]
    assert all(e["group"] == "pool" for e in frag["entities"])
    lb = frag["entities"][0]
    assert lb["params"]["routes"] == {"s1": 1, "s2": 1, "s3": 1}
    assert frag["entities"][1]["type"] == "web/app-server"
    assert len(frag["connections"]) == 6
    assert frag["inputs"] == {"in": "pool_lb.in"} and frag["outputs"] == {"out": "pool_join.out"}


def test_template_fragment_simulates_when_wired(lib):
    frag = lib.instantiate("web/lb-pool", {"n": 2, "server": "queueing/mm-server"}, prefix="p")
    model = ModelSpec.model_validate(
        {
            "entities": [
                {"id": "src", "type": "source", "params": {"interarrival": 1, "count": 20}},
                *frag["entities"],
                {"id": "out", "type": "sink"},
            ],
            "connections": [
                {"from": "src.out", "to": frag["inputs"]["in"]},
                *frag["connections"],
                {"from": frag["outputs"]["out"], "to": "out.in"},
            ],
        }
    )
    sim = Simulation(model, seed=2, library=lib)
    sim.run()
    assert sim.summary()["entities"]["out"]["received"]["value"] == 20


def test_tandem_uses_arithmetic(lib):
    frag = lib.instantiate("queueing/tandem", {"n": 4}, prefix="t")
    pairs = [(c["from"], c["to"]) for c in frag["connections"]]
    assert pairs == [("t_q1.out", "t_q2.in"), ("t_q2.out", "t_q3.in"), ("t_q3.out", "t_q4.in")]
    assert frag["outputs"] == {"out": "t_q4.out"}
    # a whole-string "{service}" keeps the distribution as a dict
    assert frag["entities"][0]["params"]["service"] == {"dist": "exp", "mean": 1.0}


def test_cached_db_computes_miss_ratio(lib):
    frag = lib.instantiate("web/cached-db", {"hit_ratio": 0.9}, prefix="c")
    routes = frag["entities"][0]["params"]["routes"]
    assert routes["hit"] == 0.9 and routes["miss"] == pytest.approx(0.1)


@pytest.mark.parametrize(
    "args, message",
    [
        ({"n": "many"}, "must be int"),
        ({"n": 0}, ">= 1"),
        ({"bogus": 1}, "unknown argument"),
    ],
)
def test_bad_template_arguments(lib, args, message):
    with pytest.raises(LibraryError, match=message):
        lib.instantiate("web/lb-pool", args)


def test_template_expressions_are_restricted(tmp_path):
    lib = make_lib(
        tmp_path,
        {},
        {"evil": {"params": {}, "entities": [{"id": "{__import__('os')}", "type": "sink"}]}},
    )
    with pytest.raises(LibraryError, match="unsupported expression"):
        lib.instantiate("t/evil")
