# behaviors

How to give an entity custom logic: tier-2 state machines (`server/kernel/machine.py`) and tier-3 Python behaviors (`libraries/<lib>/behaviors/*.py`). Read this before writing or changing either, or the shared expression language (`server/kernel/expr.py`).

## Which tier

| Need | Use |
|---|---|
| Queueing, delay, routing by weight | a built-in (`server`, `delay`, `router`...) or a library type that extends one |
| States, timers, counters, retries, conditional routing, batching | **a state machine** (`extends: machine`), written as a script. It is data, so it is safe in models, editable in the UI, and fine for the agent to write. |
| Math or data structures that the machine language can't express | **a Python behavior**. It is trusted repo code and needs a reviewed edit (see below). |

## State machines

A type file with `extends: machine` and a top-level `machine:` key (which becomes the `machine` param). Write it as a **script** (`machine_script.py`, full grammar in its docstring), which reads like code:

```yaml
machine: |
  inputs in, failure
  outputs out, rejected
  var failures = 0

  on failure: pass             # a fallback handler for every state

  state closed:                # the first state is the initial one
    enter: after window -> tick
    on in: send out
    on failure:
      failures += 1
      if failures >= threshold:
        goto open
```

- `parse_script` turns a script into the same mapping as the older YAML form (`machine.py` docstring), and `check_machine` accepts either. Nothing in the runtime changed, so a script is still data: no `eval`, safe in models and in agent output. Script errors name the line. `format_script` turns a mapping back into a script; `parse(format(m))` runs exactly like `m`.
- Script semantics that differ from the mapping form: each line is one action, so consecutive assignments are sequential, as in code. `a, b = e1, e2` assigns from the old values at once (the mapping form's multi-key `set`). `x += e` is `x = x + (e)`. `elif` is an `if` nested in `else`.
- `var` values are literals (numbers, strings, lists). A var that starts from a param needs a first state that sets it and moves on (see `ecology/rabbits`).
- The inspector edits a machine as a script in a code box (Tab indents). A machine still written as a mapping shows as JSON and is saved as a mapping.
- Templates substitute `{expr}` in strings, so a machine script inside a template must not use `{...}` payloads. Keep machines in types.
- The agent's `define_type` tool still takes a mapping. Changing its schema or description changes recorded tool listings, so it needs the cassettes re-recorded.

- Actions: `send`, `emit`, `drop`, `set`, `goto`, `after`, `if`/`then`/`else`, `count`, `observe`, `push`, `pop`, `release`.
- `goto` ends the current action list. Then the old state's `exit` actions run, then the new state's `enter` actions. A single event allows at most 50 transitions, to catch goto loops.
- A timer belongs to the state that was current when it was set. Leaving that state cancels it, and a message it was holding is dropped as `timer <event> cancelled`. To time a state, start the timer in the state's `enter`. `keep: true` makes a timer survive state changes.
- A message that a handler neither sends, drops, pushes nor holds is **absorbed**. It is counted and dropped with reason `absorbed`, which the canvas doesn't flash. A message with no handler is `unhandled`.
- A runtime error drops the message, increments `errors` and sets `last_error`. The run continues.
- Metrics: `in_<state>` is a time-weighted gauge (its `avg` is the fraction of time spent in that state). There are also `transitions`, `absorbed`, `unhandled` and `errors`. `summary_extra` adds `state`, `states` and `vars`.
- The type's own params (`threshold`...) are expression names. `/api/types` exposes them as editable fields.
- Shipped examples: `web/circuit-breaker` and `queueing/batcher`. `projects/examples/resilience.yaml` shows them working together. `ecology/rabbits` and `ecology/foxes` are population machines.
- **Gillespie pattern** (the ecology types): in the state's `enter`, draw the time to the next event as `exponential(1 / total_rate)`; when it fires, pick which event happened with `random()`, update the counts, and `goto` the same state. The self-`goto` cancels the old timer (timers belong to a state *generation*), so any message that changes a rate can also just `goto` the state to redraw. Use `keep: true` for timers that must survive this, such as a periodic report.
- The canvas badge of a machine shows its state and its **first** variable, so put the main quantity first in `vars`.

## Model globals (`model_globals.py`)

A model's `globals:` is a script of constants and functions that every machine in the model can use. It is edited in the inspector when nothing is selected.

```yaml
globals: |
  meadow = 2000
  def logistic(n, cap):
    return n * (1 - n / cap)
```

- Constants are computed once, when the model loads. They can use earlier constants, any function, and the pure built-ins (`min`, `max`, `abs`, `round`, `int`, `float`, `len`), but not `random()`.
- A function body is one `return <expr>`. It sees its arguments and the constants, not a machine's variables (pass them in). It can call other functions, recursion included (`... if n > 1 else ...` is lazy), up to 50 calls deep. It can also use every function the calling machine has: `random()` inside a function draws from the calling entity's stream, so runs repeat and forks stay exact.
- In a machine, names resolve as variables, then params, then globals. A type param such as `capacity` therefore hides a constant of the same name.
- `Globals` holds only data (strings and values), so snapshots pickle. `bind` builds the callables for each evaluation.
- Globals are not visible in entity params, links or templates (those are values, not expressions). A type that calls a global function only works in models that define it.

## Expression language (`expr.py`)

Python syntax that is parsed with `ast` and interpreted, never passed to `eval`. Allowed: literals, names, `a.b` and `a["b"]` lookups into dicts and lists, arithmetic, comparisons, `and`/`or`/`not`, `x if c else y`, and calls to functions the caller supplies. Templates get no functions. Machines get `random`, `uniform`, `exponential`, `sample`, `len`, `min`, `max`, `abs`, `round`, `int` and `float`, plus the model's global functions. Inside a machine, `msg` is the message payload plus `_age`, `_hops` and `_size`. Expressions are parsed once and cached.

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
