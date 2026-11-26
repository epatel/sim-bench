"""The ecology library: stochastic Lotka-Volterra from two state machines."""

from pathlib import Path

from server.kernel import Library, Simulation, load_model
from server.kernel.model import ModelSpec
from server.kernel.scenario import flat_metrics

ROOT = Path(__file__).resolve().parent.parent
LIB = Library.load(ROOT / "libraries")


def sim_of(entities, connections=(), seed=1):
    model = ModelSpec.model_validate({"entities": entities, "connections": list(connections)})
    return Simulation(model, seed=seed, library=LIB)


def populations(sim):
    s = sim.summary()["entities"]
    return s["rabbits"]["vars"]["rabbits"], s["foxes"]["vars"]["foxes"]


def test_rabbits_alone_fill_the_meadow():
    sim = sim_of([{"id": "rabbits", "type": "ecology/rabbits", "params": {"capacity": 300}}])
    sim.run(until=30)
    assert 255 <= sim.summary()["entities"]["rabbits"]["vars"]["rabbits"] <= 300


def test_foxes_without_prey_starve():
    sim = sim_of([{"id": "foxes", "type": "ecology/foxes"}])
    sim.run(until=60)
    s = sim.summary()["entities"]["foxes"]
    assert s["state"] == "extinct" and s["vars"]["foxes"] == 0


def test_the_example_cycles_with_foxes_lagging_rabbits():
    model = load_model(ROOT / "projects/examples/rabbits-and-foxes.yaml")
    sim = Simulation(model, seed=0, library=LIB)
    series = []
    for t in range(1, 61):
        sim.run(until=t)
        series.append(populations(sim))
    rabbits, foxes = zip(*series[10:], strict=True)
    assert min(rabbits) > 0 and min(foxes) > 0  # both survive this seed
    assert max(rabbits) > 3 * min(rabbits)  # boom and bust, not a steady state
    # foxes peak after rabbits: the first fox peak comes 1-4 time units after the rabbits'
    first = slice(0, 10)
    r_peak = max(range(10), key=lambda i: series[first][i][0])
    f_peak = max(range(10), key=lambda i: series[first][i][1])
    assert 1 <= f_peak - r_peak <= 4


def test_machine_variables_are_recorded_in_batch_metrics():
    model = load_model(ROOT / "projects/examples/rabbits-and-foxes.yaml")
    sim = Simulation(model, seed=0, library=LIB)
    sim.run(until=1)
    flat = flat_metrics(sim.summary())
    assert flat["entities.rabbits.vars.rabbits"] == populations(sim)[0]
    assert flat["entities.foxes.vars.foxes"] == populations(sim)[1]
