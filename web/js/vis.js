// The Visual canvas: widgets showing values computed from the run by each visual's
// script (server/kernel/visuals.py; the browser never runs those scripts). Also the
// inspector for one visual, and the "Visuals" section of the entity inspector.

import { el } from "./dom.js";
import { fmt } from "./canvas.js";

const WIDGETS = { label: "fa-font", bar: "fa-chart-bar", sparkline: "fa-chart-line" };
const HISTORY = 160; // sparkline points
const GRID = 8;
const HELP = "Set output = <expr>. Optional: title, unit, min, max. Names: the entity's metrics by name "
  + "(current number), m.<metric>.<field> (e.g. m.residence.p95), its machine vars, now, "
  + "entities.<id>.<metric>…, the model's globals, and earlier lines. Functions: min max abs round int float len str.";

// deps: {model(), selected(), select(id), moved(), remove(id)}
export class VisBoard {
  constructor(root, deps) {
    this.root = root;
    this.deps = deps;
    this.values = {};
    this.history = {};
    this.root.tabIndex = 0;
    this.root.addEventListener("mousedown", (ev) => {
      if (ev.target === this.root) deps.select(null);
    });
    this.root.addEventListener("keydown", (ev) => {
      const id = deps.selected();
      const deleting = ev.key === "Delete" || ev.key === "Backspace";
      if (id && deleting) deps.remove(id);
    });
  }

  reset() {
    this.values = {};
    this.history = {};
    this.render();
  }

  // A script changed: its old values no longer belong on the same sparkline.
  forget() {
    this.history = {};
  }

  update(values) {
    this.values = values;
    for (const [id, v] of Object.entries(values)) {
      if (typeof v.output !== "number") continue;
      const h = (this.history[id] ||= []);
      h.push(v.output);
      if (h.length > HISTORY) h.shift();
    }
    this.render();
  }

  render() {
    const visuals = this.deps.model().visuals || [];
    const keep = new Set(visuals.map((v) => v.id));
    for (const w of [...this.root.querySelectorAll(".vis-w")]) if (!keep.has(w.dataset.id)) w.remove();
    visuals.forEach((v, i) => this.draw(v, i));
    this.root.classList.toggle("empty", !visuals.length);
  }

  draw(v, i) {
    v.pos ||= [GRID * 4 + (i % 4) * 232, GRID * 4 + Math.floor(i / 4) * 136];
    let w = this.root.querySelector(`.vis-w[data-id="${CSS.escape(v.id)}"]`);
    if (!w || w.dataset.widget !== v.widget) {
      w?.remove();
      w = this.create(v);
    }
    w.style.left = `${v.pos[0]}px`;
    w.style.top = `${v.pos[1]}px`;
    w.classList.toggle("selected", this.deps.selected() === v.id);
    fill(w, v, this.values[v.id], this.history[v.id] || []);
  }

  create(v) {
    const w = el("div", { class: `vis-w vis-${v.widget}`, "data-id": v.id, "data-widget": v.widget });
    w.append(el("div", { class: "vis-title" }), el("div", { class: "vis-value" }));
    if (v.widget === "bar") {
      const track = el("div", { class: "vis-track" });
      track.append(el("div", { class: "vis-fill" }));
      w.append(track);
    }
    if (v.widget === "sparkline") w.append(el("canvas", { class: "vis-spark", width: "200", height: "48" }));
    w.addEventListener("mousedown", (ev) => this.startDrag(ev, v, w));
    this.root.append(w);
    return w;
  }

  startDrag(ev, v, w) {
    ev.preventDefault();
    this.root.focus({ preventScroll: true });
    this.deps.select(v.id);
    const start = [ev.clientX, ev.clientY], origin = [...v.pos];
    let moved = false;
    const move = (e) => {
      const dx = e.clientX - start[0], dy = e.clientY - start[1];
      v.pos = [snap(Math.max(0, origin[0] + dx)), snap(Math.max(0, origin[1] + dy))];
      w.style.left = `${v.pos[0]}px`;
      w.style.top = `${v.pos[1]}px`;
      moved = true;
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      if (moved) this.deps.moved();
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  }
}

function snap(x) {
  return Math.round(x / GRID) * GRID;
}

function show(x) {
  return typeof x === "number" ? fmt(x) : String(x ?? "–");
}

// Put a visual's latest value into its widget.
function fill(w, v, value, history) {
  const title = w.querySelector(".vis-title");
  const text = w.querySelector(".vis-value");
  title.textContent = value?.title ?? v.id;
  w.classList.toggle("failed", Boolean(value?.error));
  if (value?.error) {
    text.textContent = value.error;
    return;
  }
  text.textContent = value ? `${show(value.output)}${value.unit ? ` ${value.unit}` : ""}` : "–";
  if (v.widget === "bar") fillBar(w, value);
  if (v.widget === "sparkline") drawSpark(w.querySelector("canvas"), history, value);
}

function fillBar(w, value) {
  const lo = value?.min ?? 0, hi = value?.max ?? 1;
  const x = typeof value?.output === "number" && hi > lo ? (value.output - lo) / (hi - lo) : 0;
  w.querySelector(".vis-fill").style.width = `${Math.max(0, Math.min(1, x)) * 100}%`;
}

function drawSpark(cv, history, value) {
  const ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, cv.width, cv.height);
  if (history.length < 2) return;
  const lo = value?.min ?? Math.min(...history), hi = value?.max ?? Math.max(...history);
  const span = hi - lo || 1;
  ctx.strokeStyle = "#f5b13d";
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.beginPath();
  history.forEach((y, i) => {
    const px = (i / (HISTORY - 1)) * cv.width;
    const py = cv.height - 3 - ((y - lo) / span) * (cv.height - 6);
    if (i) ctx.lineTo(px, py);
    else ctx.moveTo(px, py);
  });
  ctx.stroke();
}

// --- inspector ------------------------------------------------------------------------

function select(options, value, onChange) {
  const s = el("select");
  for (const [v, label] of options) s.append(el("option", { value: v }, label));
  s.value = value;
  s.onchange = () => onChange(s.value);
  return s;
}

function field(label, input) {
  const f = el("div", { class: "field" });
  f.append(el("label", {}, label), input);
  return f;
}

// deps: {model, summary, values, changed(), remove(id), showVisual()}
export function inspectVisual(root, v, deps) {
  root.append(el("h2", {}, "Visual"));
  const title = el("h3");
  title.append(el("i", { class: `fa-solid ${WIDGETS[v.widget]}` }), ` ${v.id}`);
  root.append(title);
  const entities = [["", "(none: model-wide)"], ...deps.model.entities.map((e) => [e.id, e.id])];
  root.append(field("entity", select(entities, v.entity || "", (x) => {
    if (x) v.entity = x;
    else delete v.entity;
    deps.changed(true);
  })));
  root.append(field("widget", select(Object.keys(WIDGETS).map((k) => [k, k]), v.widget, (x) => {
    v.widget = x;
    deps.changed(true);
  })));
  root.append(codeField(v, deps), el("p", { class: "sub vis-help" }, HELP));
  const names = metricNames(deps.summary, v.entity);
  if (names.length) root.append(el("p", { class: "sub" }, `${v.entity} has: ${names.join(", ")}`));
  root.append(visualActions(v, deps));
}

function codeField(v, deps) {
  const area = el("textarea", { class: "code", spellcheck: "false", wrap: "off", rows: "6" });
  area.value = v.code;
  const status = el("div", { class: "sub vis-status" });
  const value = deps.values[v.id];
  status.textContent = value?.error ? `error: ${value.error}` : value ? `output: ${show(value.output)}` : "Run the model to see the value.";
  let timer = null;
  area.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      v.code = area.value;
      deps.changed(false); // keep the text area (and its cursor) as it is
    }, 400);
  });
  const f = field("script", area);
  f.append(status);
  return f;
}

function visualActions(v, deps) {
  const row = el("div", { class: "row-actions" });
  const show = el("button", { title: "Switch to the Visual canvas" });
  show.append(el("i", { class: "fa-solid fa-display" }), " Show");
  show.onclick = deps.showVisual;
  const del = el("button", { class: "danger" });
  del.append(el("i", { class: "fa-solid fa-trash" }), " Delete");
  del.onclick = () => deps.remove(v.id);
  row.append(show, del);
  return row;
}

// The names a script sees for an entity, most interesting first: gauges (levels that
// rise and fall, but not the in_<state> markers), then samples, then counters, then vars.
const RANK = { gauge: 0, samples: 1, counter: 2 };

function metricNames(summary, entity) {
  const data = summary?.entities?.[entity];
  if (!data) return [];
  const rank = (k) => (RANK[data[k].kind] ?? 3) + (k.startsWith("in_") || k === "errors" ? 3 : 0);
  const metrics = Object.keys(data).filter((k) => data[k]?.kind).sort((a, b) => rank(a) - rank(b));
  return [...new Set([...metrics, ...Object.keys(data.vars || {})])];
}

// The "Visuals" part of the entity inspector: the entity's visuals, and a button for a new one.
// deps: {model, summary, select(id), add(visual)}
export function visualSection(e, deps) {
  const box = el("div", { class: "vis-section" });
  box.append(el("h2", {}, "Visuals"));
  const mine = (deps.model.visuals || []).filter((v) => v.entity === e.id);
  for (const v of mine) {
    const b = el("button", { class: "vis-link" });
    b.append(el("i", { class: `fa-solid ${WIDGETS[v.widget]}` }), ` ${v.id}`);
    b.onclick = () => deps.select(v.id);
    box.append(b);
  }
  const add = el("button", { title: "Show a value of this entity on the Visual canvas" });
  add.append(el("i", { class: "fa-solid fa-plus" }), " Visualise");
  add.onclick = () => deps.add(newVisual(e, deps));
  box.append(add);
  return box;
}

function newVisual(e, deps) {
  const ids = new Set((deps.model.visuals || []).map((v) => v.id));
  let id = `${e.id}_vis`;
  for (let n = 2; ids.has(id); n++) id = `${e.id}_vis${n}`;
  const first = metricNames(deps.summary, e.id)[0] || null;
  const output = first ? `output = ${first}` : "output = now";
  const title = first && first !== e.id ? `${e.id} ${first}` : e.id;
  return { id, entity: e.id, widget: "label", code: `${output}\ntitle = "${title}"\n` };
}
