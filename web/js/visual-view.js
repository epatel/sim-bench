// Wires the Visual canvas into the app: the Design / Visual switch, visual selection,
// live values from run frames, and visual edits sent to the running simulation (the run
// goes on; visuals only read it).

import { VisBoard, inspectVisual, visualSection } from "./vis.js";
import { fmt } from "./canvas.js";

const $ = (id) => document.getElementById(id);

// ctx: {state, editor, run, validate}
export function wireVisuals({ state, editor, run, validate }) {
  const visuals = () => (state.model.visuals ||= []);
  const selected = () => (editor.selection?.kind === "visual" ? editor.selection.id : null);
  const select = (id) => editor.select(id ? { kind: "visual", id } : null);
  let values = {};

  function sync(rerender) {
    board.forget();
    validate();
    if (state.runLoaded) run.visuals(visuals());
    board.render();
    if (rerender) editor.select(editor.selection); // redraws the inspector
  }

  function remove(id) {
    state.model.visuals = visuals().filter((v) => v.id !== id);
    if (selected() === id) select(null);
    sync(false);
  }

  const board = new VisBoard($("vis"), { model: () => state.model, selected, select, remove, moved: () => {} });

  function setView(view) {
    for (const b of document.querySelectorAll(".view-tabs button")) b.classList.toggle("on", b.dataset.view === view);
    $("stage").classList.toggle("visual-view", view === "visual");
    if (view === "visual") board.render();
  }
  for (const b of document.querySelectorAll(".view-tabs button")) b.onclick = () => setView(b.dataset.view);

  run.addEventListener("loaded", () => {
    values = {};
    board.reset();
  });
  run.addEventListener("frame", (ev) => {
    if (!ev.detail.visuals) return;
    values = ev.detail.visuals;
    board.update(values);
    const status = document.querySelector("#inspector .vis-status");
    const v = values[selected()];
    if (status && v) status.textContent = v.error ? `error: ${v.error}` : `output: ${typeof v.output === "number" ? fmt(v.output) : v.output}`;
  });
  editor.addEventListener("select", () => board.render());

  return {
    setView,
    reset: () => board.reset(),
    exists: (id) => visuals().some((v) => v.id === id),
    remove,
    inspect(root, id) {
      const v = visuals().find((x) => x.id === id);
      if (!v) return false;
      inspectVisual(root, v, {
        model: state.model, summary: state.summary, values,
        changed: sync, remove, showVisual: () => setView("visual"),
      });
      return true;
    },
    section(e) {
      return visualSection(e, {
        model: state.model, summary: state.summary, select,
        add(v) {
          visuals().push(v);
          sync(false);
          select(v.id);
        },
      });
    },
  };
}
