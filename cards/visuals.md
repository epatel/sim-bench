# Visuals (the Visual canvas)

Read this before changing `visuals` in the model, `server/kernel/visuals.py`, the run's `visuals` command, or `web/js/vis.js` and `web/js/visual-view.js`.

## Model

```yaml
visuals:
  - id: db_load              # unique, no '.'
    entity: db               # optional: whose metrics the script sees by name
    widget: bar              # label (default) | bar | sparkline
    pos: [32, 48]            # on the Visual canvas (set when shown or dragged)
    code: |
      output = busy + queue  # required
      title = "db load"      # optional: title, unit, min, max
      max = 20
```

`ModelSpec.visuals` is left out of dumps when it's empty (`exclude_if`), so models and recorded agent cassettes without visuals serialize as before.

## Evaluation (server side, never in the browser)

- The script is `name = <expr>` lines in the `expr` language. It is parsed by `parse_visual` (cached) and checked in `Simulation.__init__` (`check_visuals`): ids are unique, entities exist, the script parses and sets `output`. Errors are `ModelError("visuals.<id>: ...")`.
- Running a script in the browser would let a model from the agent or a shared file run code in the page, which holds the access token. So scripts are interpreted by `expr` on the server.
- `RunSession.send_frame` adds `visuals: {id: {output, title?, unit?, min?, max?} | {error}}` to every frame that carries a summary: about 3 per second while playing, plus on load, pause, step and reset. Values that aren't numbers, strings, booleans or None are sent as text.
- Names a script sees, later ones winning:
  1. The globals' constants.
  2. `now` and `entities` (the raw per-entity summaries).
  3. With an entity: its machine vars, then each metric by name as its current number (a counter's value, a gauge's value, a sample set's mean), then `id` and `m` (its full summary).
  4. Earlier lines.
- Functions: the pure built-ins, `str`, and the model's global functions.
- A runtime error (say a metric that doesn't exist yet) is per visual, `{error}`, and shows on its widget.
- `{"cmd": "visuals", "visuals": [...]}` on `/ws/run` validates and swaps the visuals of the running model, then sends a frame. Editing a visual never restarts or invalidates the run.

## Browser

- `index.html` has Design / Visual buttons over the stage. Visual view hides the design canvas (`.stage.visual-view > canvas`; the sparkline's own canvas must stay visible) and shows `#vis`.
- `VisBoard` (`vis.js`) draws DOM widgets at `pos`. They are dragged on an 8px grid, and Delete removes the selected one. Sparklines keep the last 160 numeric outputs client-side. The history is cleared on load and whenever a visual is edited.
- The selection kind `visual` gets its own inspector: entity, widget, the script (debounced 400ms, then sent live), the live output or error, the entity's metric names, Show and Delete.
- The entity inspector's **Visuals** section lists that entity's visuals and has **Visualise**. That creates a label on the entity's most interesting metric (a gauge first, but not `in_<state>`).
