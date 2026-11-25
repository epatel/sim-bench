// App wiring: toolbar, palette, inspector, validation and the live run.
import { api, RunClient } from "./api.js";
import { Editor, NODE_H, NODE_W, fmt, linkKey, splitRef } from "./canvas.js";
import { loadIconFont } from "./icons.js";

const $ = (id) => document.getElementById(id);

const LINK_FIELDS = {
  latency: { type: "dist", default: 0, help: "delay per message" },
  bandwidth: { type: "float", default: null, nullable: true, help: "size units / time (empty = ∞)" },
  loss: { type: "float", default: 0, help: "drop probability" },
  ordered: { type: "bool", default: true, help: "never overtake" },
  up: { type: "bool", default: true, help: "link is up" },
};

const state = {
  model: emptyModel(),
  path: "untitled.yaml",
  types: {},
  ports: {},
  summary: null,
  runLoaded: false,
  runStale: false,
  playing: false,
  speed: 1,
};

function emptyModel() {
  return { name: "untitled", description: "", entities: [], connections: [] };
}

// --- the object the canvas talks to ------------------------------------------

const app = {
  get model() { return state.model; },
  get types() { return state.types; },
  get summary() { return state.summary; },

  portsOf(id) {
    if (state.ports[id]) return state.ports[id];
    const e = state.model.entities.find((x) => x.id === id);
    const t = e && state.types[e.type];
    return t ? { inputs: t.inputs, outputs: t.outputs } : { inputs: [], outputs: [] };
  },

  addEntity(kind, pos) {
    const id = uniqueId(kind);
    state.model.entities.push({ id, type: kind, params: {}, pos: pos.map((v) => Math.round(v)) });
    editor.select({ kind: "entity", id });
    changed();
  },

  connect(from, to) {
    const conns = state.model.connections.filter((c) => c.from !== from); // one link per output
    conns.push({ from, to });
    state.model.connections = conns;
    editor.select({ kind: "link", id: `${from}->${to}` });
    changed();
  },

  deleteSelection() {
    const sel = editor.selection;
    if (!sel) return;
    if (sel.kind === "entity") {
      state.model.entities = state.model.entities.filter((e) => e.id !== sel.id);
      state.model.connections = state.model.connections.filter(
        (c) => splitRef(c.from)[0] !== sel.id && splitRef(c.to)[0] !== sel.id,
      );
    } else {
      state.model.connections = state.model.connections.filter((c) => linkKey(c) !== sel.id);
    }
    editor.select(null);
    changed();
  },
};

const editor = new Editor($("canvas"), app);
const run = new RunClient();

// --- model edits and validation --------------------------------------------------

let validateTimer = null;

function changed({ structural = true } = {}) {
  if (state.runLoaded) {
    state.runStale = true;
    setStatus("model changed · reset to apply", "warn");
  }
  if (structural) renderInspector();
  clearTimeout(validateTimer);
  validateTimer = setTimeout(validateNow, 150);
}

async function validateNow() {
  try {
    const res = await api.validate(state.model);
    if (res.ok) {
      state.ports = res.ports;
      hideBanner();
    } else {
      showBanner(res.error);
    }
  } catch (e) {
    showBanner(e.message);
  }
}

function uniqueId(base) {
  const ids = new Set(state.model.entities.map((e) => e.id));
  for (let i = 1; ; i++) if (!ids.has(`${base}${i}`)) return `${base}${i}`;
}

function renameEntity(oldId, newId) {
  if (!newId || newId.includes(".") || state.model.entities.some((e) => e.id === newId)) return false;
  const e = state.model.entities.find((x) => x.id === oldId);
  e.id = newId;
  const fix = (ref) => {
    const [ent, port] = splitRef(ref);
    return ent === oldId ? `${newId}.${port}` : ref;
  };
  for (const c of state.model.connections) {
    c.from = fix(c.from);
    c.to = fix(c.to);
  }
  delete state.ports[oldId];
  editor.selection = { kind: "entity", id: newId };
  changed();
  return true;
}

// Place entities that have no position: columns by distance from the sources.
function layout(model) {
  const missing = model.entities.filter((e) => !e.pos);
  if (!missing.length) return;
  const next = new Map();
  for (const c of model.connections) {
    const a = splitRef(c.from)[0], b = splitRef(c.to)[0];
    if (!next.has(a)) next.set(a, []);
    next.get(a).push(b);
  }
  const incoming = new Set(model.connections.map((c) => splitRef(c.to)[0]));
  const depth = new Map();
  const queue = model.entities.filter((e) => !incoming.has(e.id)).map((e) => e.id);
  queue.forEach((id) => depth.set(id, 0));
  while (queue.length) {
    const id = queue.shift();
    for (const n of next.get(id) || []) {
      if (!depth.has(n)) {
        depth.set(n, depth.get(id) + 1);
        queue.push(n);
      }
    }
  }
  const rows = new Map();
  for (const e of model.entities) {
    if (e.pos) continue;
    const d = depth.get(e.id) ?? 0;
    const r = rows.get(d) || 0;
    rows.set(d, r + 1);
    e.pos = [d * (NODE_W + 90), r * (NODE_H + 60)];
  }
}

function setModel(model, path) {
  model.entities ??= [];
  model.connections ??= [];
  for (const e of model.entities) e.params ??= {};
  layout(model);
  state.model = model;
  state.path = path;
  state.ports = {};
  state.summary = null;
  state.runLoaded = false;
  state.runStale = false;
  $("model-path").value = path;
  editor.clearLive();
  editor.select(null);
  requestAnimationFrame(() => editor.fit()); // after layout has settled
  setStatus("");
  validateNow();
  renderInspector();
}

// --- palette -----------------------------------------------------------------

function renderPalette() {
  const root = $("palette");
  root.innerHTML = "";
  for (const t of Object.values(state.types)) {
    const card = el("div", { class: "type-card", draggable: "true", title: t.description });
    card.append(el("i", { class: `fa-solid ${t.icon}` }));
    const txt = el("div");
    txt.append(el("div", { class: "k" }, t.kind), el("div", { class: "d" }, t.description));
    card.append(txt);
    card.addEventListener("dragstart", (ev) => ev.dataTransfer.setData("text/x-sim-type", t.kind));
    card.addEventListener("click", () => {
      const r = $("canvas").getBoundingClientRect();
      const p = editor.toWorld({ clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 });
      const n = state.model.entities.length;
      app.addEntity(t.kind, [p.x - NODE_W / 2 + (n % 5) * 16, p.y - NODE_H / 2 + (n % 5) * 16]);
    });
    root.append(card);
  }
}

// --- inspector -----------------------------------------------------------------

function renderInspector() {
  const root = $("inspector");
  root.innerHTML = "";
  const sel = editor.selection;
  if (sel?.kind === "entity") {
    const e = state.model.entities.find((x) => x.id === sel.id);
    if (e) return inspectEntity(root, e);
  }
  if (sel?.kind === "link") {
    const c = state.model.connections.find((x) => linkKey(x) === sel.id);
    if (c) return inspectLink(root, c);
  }
  inspectModel(root);
}

function inspectModel(root) {
  root.append(el("h2", {}, "Model"));
  root.append(textField("name", state.model.name, (v) => { state.model.name = v; }));
  root.append(textField("description", state.model.description || "", (v) => { state.model.description = v; }));
  root.append(
    el("p", { class: "sub" },
      `${state.model.entities.length} entities · ${state.model.connections.length} connections`),
  );
  root.append(el("h2", {}, "Run summary"));
  root.append(el("div", { id: "metrics" }));
  renderMetrics();
}

function inspectEntity(root, e) {
  const type = state.types[e.type];
  root.append(el("h2", {}, "Entity"));
  const title = el("h3");
  title.append(el("i", { class: `fa-solid ${type?.icon || "fa-circle-question"}` }), ` ${e.id}`);
  root.append(title);
  root.append(el("p", { class: "sub" }, type ? type.description : `unknown type ${e.type}`));

  const idField = textField("id", e.id, (v, input) => {
    if (!renameEntity(e.id, v)) input.classList.add("invalid");
  });
  root.append(idField);
  root.append(textField("label", e.name || "", (v) => { if (v) e.name = v; else delete e.name; }));

  if (type && Object.keys(type.params).length) {
    root.append(el("h2", {}, "Parameters"));
    for (const [name, meta] of Object.entries(type.params)) {
      root.append(paramField(e.params, name, meta));
    }
  }
  const actions = el("div", { class: "row-actions" });
  const del = el("button", { class: "danger", title: "Delete (Del)" });
  del.append(el("i", { class: "fa-solid fa-trash" }), " Delete");
  del.onclick = () => app.deleteSelection();
  actions.append(del);
  root.append(actions);

  root.append(el("h2", {}, "Metrics"));
  root.append(el("div", { id: "metrics" }));
  renderMetrics();
}

function inspectLink(root, c) {
  root.append(el("h2", {}, "Connection"));
  root.append(el("h3", {}, `${c.from} → ${c.to}`));
  root.append(el("p", { class: "sub" }, "Changes apply to the run after reset."));
  for (const [name, meta] of Object.entries(LINK_FIELDS)) root.append(paramField(c, name, meta));
  const actions = el("div", { class: "row-actions" });
  const del = el("button", { class: "danger" });
  del.append(el("i", { class: "fa-solid fa-trash" }), " Delete");
  del.onclick = () => app.deleteSelection();
  actions.append(del);
  root.append(actions);
  root.append(el("h2", {}, "Metrics"));
  root.append(el("div", { id: "metrics" }));
  renderMetrics();
}

function textField(label, value, onChange) {
  const f = el("div", { class: "field" });
  f.append(el("label", {}, label));
  const input = el("input", { value, spellcheck: "false" });
  input.addEventListener("change", () => {
    input.classList.remove("invalid");
    onChange(input.value.trim(), input);
    changed({ structural: false });
  });
  f.append(input);
  return f;
}

// Edit obj[name] according to its schema entry; values are typed as JSON.
function paramField(obj, name, meta) {
  const f = el("div", { class: meta.type === "bool" ? "field check" : "field" });
  const has = Object.prototype.hasOwnProperty.call(obj, name);
  if (meta.type === "bool") {
    const label = el("label");
    const box = el("input", { type: "checkbox" });
    box.checked = has ? !!obj[name] : !!meta.default;
    box.addEventListener("change", () => {
      if (box.checked === meta.default) delete obj[name];
      else obj[name] = box.checked;
      changed({ structural: false });
    });
    label.append(box, `${name}`, el("span", { class: "help" }, meta.help || ""));
    f.append(label);
    return f;
  }
  const label = el("label");
  label.append(el("span", {}, name), el("span", { class: "help" }, meta.help || ""));
  f.append(label);
  if (meta.type === "enum") {
    const s = el("select");
    for (const o of meta.options) s.append(el("option", { value: o }, o));
    s.value = has ? obj[name] : meta.default;
    s.addEventListener("change", () => {
      obj[name] = s.value;
      changed();
    });
    f.append(s);
    return f;
  }
  const placeholder = meta.default === null ? "empty" : JSON.stringify(meta.default);
  const input = el("input", { spellcheck: "false", placeholder });
  input.value = has && obj[name] !== null ? JSON.stringify(obj[name]) : "";
  if (meta.type === "dist") input.title = 'A number, or e.g. {"dist":"exp","mean":1}, {"dist":"uniform","low":0,"high":2}';
  input.addEventListener("change", () => {
    const text = input.value.trim();
    input.classList.remove("invalid");
    if (text === "") {
      delete obj[name];
    } else {
      let v;
      try {
        v = JSON.parse(text);
      } catch {
        input.classList.add("invalid");
        return;
      }
      if ((meta.type === "int" || meta.type === "float") && typeof v !== "number") {
        input.classList.add("invalid");
        return;
      }
      if (meta.type === "int" && !Number.isInteger(v)) {
        input.classList.add("invalid");
        return;
      }
      obj[name] = v;
    }
    // Router routes change the ports, so redraw everything.
    changed({ structural: name === "routes" });
  });
  f.append(input);
  return f;
}

function renderMetrics() {
  const root = $("metrics");
  if (!root) return;
  const sel = editor.selection;
  const s = state.summary;
  root.innerHTML = "";
  if (!s) {
    root.append(el("p", { class: "empty" }, "Press play to run."));
    return;
  }
  let data;
  if (sel?.kind === "entity") data = s.entities[sel.id];
  else if (sel?.kind === "link") data = s.links[sel.id];
  else data = { time: s.time, events: s.events, pending: s.pending, seed: s.seed };
  if (!data) {
    root.append(el("p", { class: "empty" }, "Not in the running model. Reset to include it."));
    return;
  }
  const table = el("table", { class: "metrics" });
  for (const [name, m] of Object.entries(data)) {
    if (name === "type") continue;
    table.append(row(name, describe(m)));
  }
  root.append(table);
}

function describe(m) {
  if (typeof m !== "object" || m === null) return fmt(m);
  if (m.kind === "counter") return fmt(m.value);
  if (m.kind === "gauge") return `${fmt(m.value)} (avg ${fmt(m.avg)}, peak ${fmt(m.peak)})`;
  if (m.kind === "samples") {
    if (!m.count) return "–";
    return `n=${fmt(m.count)} mean=${fmt(m.mean)} p95=${fmt(m.p95)} max=${fmt(m.max)}`;
  }
  return JSON.stringify(m);
}

function row(a, b) {
  const tr = el("tr");
  tr.append(el("td", {}, a), el("td", {}, b));
  return tr;
}

// --- run controls ----------------------------------------------------------------

function ensureLoaded() {
  if (!state.runLoaded || state.runStale) {
    editor.clearLive();
    run.load(state.model, Number($("seed").value) || 0);
    run.speed(state.speed);
    state.runLoaded = true;
    state.runStale = false;
    setStatus("");
  }
}

$("btn-play").onclick = () => {
  if (state.playing) return run.pause();
  ensureLoaded();
  run.play();
};
$("btn-step").onclick = () => {
  ensureLoaded();
  run.step();
};
$("btn-reset").onclick = () => {
  state.runStale = true;
  state.summary = null;
  ensureLoaded();
  renderMetrics();
};
$("speed").oninput = () => {
  state.speed = 10 ** Number($("speed").value);
  $("speed-label").textContent = `${fmt(state.speed)}×`;
  run.speed(state.speed);
};
$("btn-fit").onclick = () => editor.fit();

run.addEventListener("frame", (ev) => {
  const f = ev.detail;
  state.playing = f.playing;
  editor.setClock(f.now, f.playing, state.speed);
  editor.addRecords(f.records);
  $("sim-time").textContent = fmt(f.now);
  $("sim-events").textContent = fmt(f.events);
  $("btn-play").innerHTML = `<i class="fa-solid ${f.playing ? "fa-pause" : "fa-play"}"></i>`;
  if (!state.runStale) {
    if (f.lagging) setStatus("lagging · lower speed", "warn");
    else if (!f.playing && f.pending === 0 && f.events > 0) setStatus("finished");
    else setStatus("");
  }
  if (f.summary) {
    state.summary = f.summary;
    renderMetrics();
  }
});
run.addEventListener("loaded", (ev) => {
  state.ports = ev.detail.ports;
});
run.addEventListener("error", (ev) => {
  state.runLoaded = false;
  setStatus("run error", "bad");
  showBanner(ev.detail.message);
});
run.addEventListener("closed", () => {
  state.runLoaded = false;
  state.playing = false;
  setStatus("disconnected", "bad");
});

// --- files -----------------------------------------------------------------------

async function refreshModelList(selected) {
  const list = await api.models();
  const s = $("model-select");
  s.innerHTML = "";
  s.append(el("option", { value: "" }, "Open model…"));
  for (const m of list) s.append(el("option", { value: m.path }, `${m.name} — ${m.path}`));
  s.value = selected || "";
}

$("model-select").onchange = async () => {
  const path = $("model-select").value;
  if (!path) return;
  try {
    setModel(await api.getModel(path), path);
  } catch (e) {
    showBanner(e.message);
  }
};
$("btn-new").onclick = () => {
  setModel(emptyModel(), "untitled.yaml");
  $("model-select").value = "";
};
async function save() {
  const path = $("model-path").value.trim();
  try {
    await api.saveModel(path, state.model);
    state.path = path;
    showBanner(`Saved projects/${path}`, "info");
    refreshModelList(path);
  } catch (e) {
    showBanner(e.message);
  }
}
$("btn-save").onclick = save;

window.addEventListener("keydown", (ev) => {
  if ((ev.metaKey || ev.ctrlKey) && ev.key === "s") {
    ev.preventDefault();
    save();
  } else if (ev.key === " " && !["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(ev.target.tagName)) {
    ev.preventDefault();
    $("btn-play").click();
  }
});

editor.addEventListener("select", () => renderInspector());
editor.addEventListener("moved", () => {});

// --- tiny DOM helper -------------------------------------------------------------

function el(tag, attrs = {}, text) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "value") n.value = v;
    else n.setAttribute(k, v);
  }
  if (text !== undefined) n.textContent = text;
  return n;
}

let bannerTimer = null;
function showBanner(text, kind = "error") {
  const b = $("banner");
  b.textContent = text;
  b.className = kind === "info" ? "banner info" : "banner";
  clearTimeout(bannerTimer);
  if (kind === "info") bannerTimer = setTimeout(hideBanner, 2000);
}
function hideBanner() {
  $("banner").className = "banner hidden";
}
function setStatus(text, kind = "") {
  const s = $("run-status");
  s.textContent = text;
  s.className = `status ${kind}`;
}

// --- start -----------------------------------------------------------------------

async function start() {
  await loadIconFont();
  const types = await api.types();
  state.types = Object.fromEntries(types.map((t) => [t.kind, t]));
  renderPalette();
  const first = "examples/web-backend.yaml";
  await refreshModelList(first);
  try {
    setModel(await api.getModel(first), first);
  } catch {
    setModel(emptyModel(), "untitled.yaml");
  }
}

start();
