# libraries

How item-type libraries and templates work (`libraries/`, `server/kernel/library.py`). Read this before adding or editing a library, a type or a template, or before changing how types resolve.

## Layout

```
libraries/<lib>/library.yaml       name, description
libraries/<lib>/types/<name>.yaml  item type    -> referenced as "<lib>/<name>"
libraries/<lib>/templates/*.yaml   template     -> referenced as "<lib>/<name>"
libraries/<lib>/behaviors/*.py     Python behaviors -> "<lib>/<kind>" (trusted code, see the behaviors card)
```

Shipped libraries:
- `web`: client, network, load-balancer, app-server, cache, database, response, circuit-breaker (a machine), and token-bucket (Python). Templates: `lb-pool`, `cached-db`.
- `queueing`: poisson-source, mm-server, batcher (a machine). Template: `tandem`.

## Item types

```yaml
name: app-server
extends: server            # built-in kind, "<lib>/<type>", or a bare name in the same library
description: ...
icon: fa-microchip         # any Font Awesome 7 solid icon name
params: {servers: 4, service: {dist: lognormal, mean: 0.12, std: 0.05}}
```

- `Library.resolve(id)` walks `extends` down to a built-in behavior. Defaults merge in this order: the behavior's `params_schema` defaults, then each ancestor's params, then the type's own params. The entity's own `params` win last. This merge happens in `Simulation.__init__`, and the model itself is never changed.
- A type may add params of its own. They are merged like the others, and `/api/types` exposes them with a guessed type. They only matter if the behavior reads them, which a state machine does through its expressions. A top-level `machine:` key becomes the `machine` param.
- Broken chains (a cycle, an unknown parent, a missing `extends`) fail when the library loads (`Library.check`). The API then returns a 500 error naming the file.
- A router-based type gets its ports from `routes`, so a type like `web/cache` has the outputs `hit` and `miss`.

## Templates

```yaml
params: {n: {type: int, default: 3, min: 1}, server: {type: string, default: web/app-server}}
entities:
  - {id: lb, type: web/load-balancer, params: {routes: {$each: n, key: "s{i}", value: 1}}}
  - {id: "s{i}", repeat: n, type: "{server}"}
connections:
  - {from: "lb.s{i}", to: "s{i}.in", repeat: n}
inputs: {in: lb.in}
outputs: {out: join.out}
```

- `{expr}` inside a string is substituted. An expression may use params, the loop index `i`, numbers, `+ - * //` and unary minus. It is parsed with `ast`, and anything else is rejected, so templates can never run code.
- A string that is exactly `{expr}` is replaced by the raw value. This keeps a distribution dict as a dict, and `{1 - hit_ratio}` as a number.
- `repeat: <expr>` emits the entry for i = 1..N. `{$each: <expr>, key, value}` builds a mapping.
- `instantiate(id, args, prefix)` prefixes every entity id with `<prefix>_`, sets `group: <prefix>`, and returns `{entities, connections, inputs, outputs, group}`. The UI picks a prefix that is not yet used and lays the fragment out at the drop point.
- The UI also adds a `GroupSpec` built from the template's `inputs` and `outputs`, so an inserted template is a composite that can be collapsed or calibrated.

## API and UI

- `GET /api/types` returns built-in and library types, with `library`, `base`, `chain`, and params whose `default` is the merged value. `GET /api/libraries` and `GET /api/templates` list the rest, and `POST /api/templates/{id}/instantiate` with `{args, prefix}` expands a template.
- The server re-reads `libraries/` on every request, so edits (by hand or by the agent) show up after a page reload. A run keeps the library it was loaded with.
- The palette has one collapsible section per library. Clicking a template opens its argument form in the inspector. Dragging a template inserts it with default arguments. Dragging a group frame's header moves the whole group.
