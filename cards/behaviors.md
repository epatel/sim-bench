# behaviors

How to give an entity custom logic: tier-2 state machines (`server/kernel/machine.py`) and tier-3 Python behaviors (`libraries/<lib>/behaviors/*.py`). Read this before writing or changing either, or the shared expression language (`server/kernel/expr.py`).

## Which tier

| Need | Use |
|---|---|
| Queueing, delay, routing by weight | a built-in (`server`, `delay`, `router`...) or a library type that extends one |
| States, timers, counters, retries, conditional routing, batching | **a state machine** (`extends: machine`). It is data, so it is safe in models, editable in the UI, and fine for the agent to write. |
| Math or data structures that the machine language can't express | **a Python behavior**. It is trusted repo code and needs a reviewed edit (see below). |

## State machines

A type file with `extends: machine` and a top-level `machine:` key (which becomes the `machine` param). The full syntax is in the `machine.py` docstring. In short:

```yaml
machine:
  inputs: [in, failure]
  outputs: [out, rejected]
  vars: {failures: 0}
  initial: closed
  on: {failure: []}            # fallback handlers for every state
  states:
    closed:
      enter: [{after: window, event: tick}]
      on:
        in: [{send: out}]
        failure: [{set: {failures: "failures + 1"}}, {if: "failures >= threshold", then: [{goto: open}]}]
```

- Actions: `send`, `emit`, `drop`, `set`, `goto`, `after`, `if`/`then`/`else`, `count`, `observe`, `push`, `pop`, `release`.
- `goto` ends the current action list. Then the old state's `exit` actions run, then the new state's `enter` actions. A single event allows at most 50 transitions, to catch goto loops.
- A timer belongs to the state that was current when it was set. Leaving that state cancels it, and a message it was holding is dropped as `timer <event> cancelled`. To time a state, start the timer in the state's `enter`. `keep: true` makes a timer survive state changes.
- A message that a handler neither sends, drops, pushes nor holds is **absorbed**. It is counted and dropped with reason `absorbed`, which the canvas doesn't flash. A message with no handler is `unhandled`.
- A runtime error drops the message, increments `errors` and sets `last_error`. The run continues.
- Metrics: `in_<state>` is a time-weighted gauge (its `avg` is the fraction of time spent in that state). There are also `transitions`, `absorbed`, `unhandled` and `errors`. `summary_extra` adds `state`, `states` and `vars`.
- The type's own params (`threshold`...) are expression names. `/api/types` exposes them as editable fields.
- Shipped examples: `web/circuit-breaker` and `queueing/batcher`. `projects/examples/resilience.yaml` shows them working together. `ecology/rabbits` and `ecology/foxes` are population machines.
- **Gillespie pattern** (the ecology types): in the state's `enter`, draw the time to the next event as `sample({'dist': 'exp', 'mean': 1 / total_rate})`; when it fires, pick which event happened with `random()`, update the counts, and `goto` the same state. The self-`goto` cancels the old timer (timers belong to a state *generation*), so any message that changes a rate can also just `goto` the state to redraw. Use `keep: true` for timers that must survive this, such as a periodic report.
- The canvas badge of a machine shows its state and its **first** variable, so put the main quantity first in `vars`.

## Expression language (`expr.py`)

Python syntax that is parsed with `ast` and interpreted, never passed to `eval`. Allowed: literals, names, `a.b` and `a["b"]` lookups into dicts and lists, arithmetic, comparisons, `and`/`or`/`not`, `x if c else y`, and calls to functions the caller supplies. Templates get no functions. Machines get `random`, `uniform`, `sample`, `len`, `min`, `max`, `abs`, `round`, `int` and `float`. Inside a machine, `msg` is the message payload plus `_age`, `_hops` and `_size`. Expressions are parsed once and cached.

## YAML booleans

All model and library YAML is read with `yamlio.load_yaml`, which uses YAML 1.2 booleans (only `true`/`false`). PyYAML's default would turn a bare `on:` into `True`. `check_machine` also repairs a `True` key, in case a machine comes from another loader.

## Python behaviors

```python
from server.kernel import Behavior

class TokenBucket(Behavior):          # registered as "<lib>/token-bucket"
    kind = "token-bucket"
    params_schema = {"rate": {"type": "float", "default": 10.0}}
    @classmethod
    def ports(cls, params): return ["in"], ["out", "rejected"]
    def on_message(self, port, msg): ...
```

- The module is imported when the library loads, and reloaded when the file's mtime changes. Each `Behavior` subclass defined in the file that has a `kind` is registered as `<lib>/<kind>`, and YAML types can extend it.
- Use only `self.ctx` (`now`, `params`, `rng`, `send`, `drop`, `schedule`, `message`, `counter`/`samples`/`gauge`, `connected`). Use `self.ctx.rng` for randomness, so runs stay repeatable. Keep state in picklable attributes, so snapshots and forks keep working.
- **These files run with full Python privileges in the server.** They are loaded only from `libraries/`, never from model files or the UI. Edits to them need the user's approval (a `permissions.ask` rule in `.claude/settings.json`). An agent that wants one must propose the file for review.
