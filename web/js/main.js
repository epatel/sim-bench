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
  templates: {},
  libraries: [],
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

  insertTemplate(id, pos) {
    insertTemplate(id, {}, null, { x: pos[0] + NODE_W / 2, y: pos[1] + NODE_H / 2 });
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
    if (!sel || sel.kind === "template") return;
    if (sel.kind === "entity" || sel.kind === "group") {
      const gone = new Set(
        state.model.entities
          .filter((e) => (sel.kind === "entity" ? e.id === sel.id : e.group === sel.id))
          .map((e) => e.id),
      );
      state.model.entities = state.model.entities.filter((e) => !gone.has(e.id));
      state.model.connections = state.model.connections.filter(
        (c) => !gone.has(splitRef(c.from)[0]) && !gone.has(splitRef(c.to)[0]),
      );
    } else if (sel.kind === "link") {
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

function changedModel(opts) {
  changed(opts);
}

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
  if (model.entities.some((e) => !e.pos)) layoutFragment(model.entities, model.connections, null);
}

// Lay out `entities` lacking pos in columns, starting at `origin` (or 0,0).
function layoutFragment(entities, connections, origin) {
  const model = { entities, connections };
  const ox = origin ? origin.x - NODE_W / 2 : 0;
  const oy = origin ? origin.y - NODE_H / 2 : 0;
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
  const todo = model.entities.filter((e) => !e.pos);
  const perColumn = new Map();
  for (const e of todo) {
    const d = depth.get(e.id) ?? 0;
    perColumn.set(d, (perColumn.get(d) || 0) + 1);
  }
  const rows = new Map();
  for (const e of todo) {
    const d = depth.get(e.id) ?? 0;
    const r = rows.get(d) || 0;
    rows.set(d, r + 1);
    const centered = r - (perColumn.get(d) - 1) / 2; // center each column on the origin row
    e.pos = [Math.round(ox + d * (NODE_W + 90)), Math.round(oy + centered * (NODE_H + 60))];
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
  const sections = [{ name: "", title: "Built-in", description: "Generic behaviors every library builds on." }];
  for (const lib of state.libraries) sections.push({ name: lib.name, title: lib.name, description: lib.description });
  for (const sec of sections) {
    const types = Object.values(state.types).filter((t) => t.library === sec.name);
    const templates = Object.values(state.templates).filter((t) => t.library === sec.name);
    if (!types.length && !templates.length) continue;
    const details = el("details", { class: "lib", open: "" });
    const summary = el("summary", { title: sec.description }, sec.title);
    details.append(summary);
    for (const t of types) details.append(typeCard(t));
    for (const t of templates) details.append(templateCard(t));
    root.append(details);
  }
}

function shortName(id) {
  return id.includes("/") ? id.slice(id.indexOf("/") + 1) : id;
}

function typeCard(t) {
  const card = el("div", { class: "type-card", draggable: "true", title: `${t.kind}\n${t.chain.join(" → ")}` });
  card.append(el("i", { class: `fa-solid ${t.icon}` }));
  const txt = el("div");
  txt.append(el("div", { class: "k" }, shortName(t.kind)), el("div", { class: "d" }, t.description));
  card.append(txt);
  card.addEventListener("dragstart", (ev) => ev.dataTransfer.setData("text/x-sim-type", t.kind));
  card.addEventListener("click", () => {
    const p = viewCenter();
    const n = state.model.entities.length;
    app.addEntity(t.kind, [p.x - NODE_W / 2 + (n % 5) * 16, p.y - NODE_H / 2 + (n % 5) * 16]);
  });
  return card;
}

function templateCard(t) {
  const card = el("div", { class: "type-card template", draggable: "true", title: `${t.id} (template)` });
  card.append(el("i", { class: `fa-solid ${t.icon}` }));
  const txt = el("div");
  const k = el("div", { class: "k" }, shortName(t.id));
  k.append(el("span", { class: "tag" }, "template"));
  txt.append(k, el("div", { class: "d" }, t.description));
  card.append(txt);
  card.addEventListener("dragstart", (ev) => ev.dataTransfer.setData("text/x-sim-template", t.id));
  card.addEventListener("click", () => editor.select({ kind: "template", id: t.id }));
  return card;
}

function viewCenter() {
  const r = $("canvas").getBoundingClientRect();
  return editor.toWorld({ clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 });
}

// Expand a template on the server and add its entities around `origin`.
async function insertTemplate(id, args = {}, prefix = null, origin = viewCenter()) {
  const base = prefix || shortName(id).replace(/-/g, "_");
  const used = new Set(state.model.entities.map((e) => e.group).filter(Boolean));
  const ids = new Set(state.model.entities.map((e) => e.id));
  let p = base;
  for (let n = 2; used.has(p) || [...ids].some((x) => x.startsWith(`${p}_`)); n++) p = `${base}${n}`;
  let frag;
  try {
    frag = await api.instantiate(id, args, p);
  } catch (e) {
    showBanner(e.message);
    return;
  }
  layoutFragment(frag.entities, frag.connections, origin);
  state.model.entities.push(...frag.entities);
  state.model.connections.push(...frag.connections);
  editor.select({ kind: "group", id: frag.group });
  changed();
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
  if (sel?.kind === "template" && state.templates[sel.id]) return inspectTemplate(root, state.templates[sel.id]);
  if (sel?.kind === "group") {
    const members = state.model.entities.filter((e) => e.group === sel.id);
    if (members.length) return inspectGroup(root, sel.id, members);
  }
  inspectModel(root);
}

function inspectTemplate(root, t) {
  root.append(el("h2", {}, "Template"));
  const title = el("h3");
  title.append(el("i", { class: `fa-solid ${t.icon}` }), ` ${t.id}`);
  root.append(title, el("p", { class: "sub" }, t.description));
  const args = {};
  if (Object.keys(t.params).length) root.append(el("h2", {}, "Arguments"));
  for (const [name, meta] of Object.entries(t.params)) {
    const fieldMeta = meta.type === "string" ? { ...meta, type: "string" } : meta;
    root.append(paramField(args, name, fieldMeta, { silent: true }));
  }
  const prefix = { value: "" };
  root.append(textField("prefix (group name)", "", (v) => { prefix.value = v; }, { silent: true }));
  const ports = [
    ...Object.entries(t.inputs).map(([k, v]) => `in ${k} → ${v}`),
    ...Object.entries(t.outputs).map(([k, v]) => `out ${k} ← ${v}`),
  ];
  if (ports.length) root.append(el("p", { class: "sub" }, `Boundary: ${ports.join(" · ")}`));
  const actions = el("div", { class: "row-actions" });
  const add = el("button", { class: "primary" });
  add.append(el("i", { class: "fa-solid fa-plus" }), " Insert");
  add.onclick = () => insertTemplate(t.id, args, prefix.value || null);
  actions.append(add);
  root.append(actions);
}

function inspectGroup(root, group, members) {
  root.append(el("h2", {}, "Group"));
  const title = el("h3");
  title.append(el("i", { class: "fa-solid fa-layer-group" }), ` ${group}`);
  root.append(title);
  root.append(el("p", { class: "sub" }, `${members.length} entities. Drag the frame header to move them together.`));
  const table = el("table", { class: "metrics" });
  for (const e of members) table.append(row(e.id, e.type));
  root.append(table);
  const actions = el("div", { class: "row-actions" });
  const ungroup = el("button", { title: "Keep the entities, drop the frame" });
  ungroup.append(el("i", { class: "fa-solid fa-object-ungroup" }), " Ungroup");
  ungroup.onclick = () => {
    for (const e of members) delete e.group;
    editor.select(null);
    changed();
  };
  const del = el("button", { class: "danger" });
  del.append(el("i", { class: "fa-solid fa-trash" }), " Delete all");
  del.onclick = () => app.deleteSelection();
  actions.append(ungroup, del);
  root.append(actions);
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
  if (type && type.chain.length > 1) {
    const chain = el("p", { class: "sub chain" });
    chain.append(el("i", { class: "fa-solid fa-sitemap" }), ` ${type.chain.join(" → ")}`);
    root.append(chain);
  }
  if (e.group) root.append(el("p", { class: "sub" }, `In group ${e.group}`));

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

// `silent` fields edit a scratch object (e.g. template arguments), not the model.
function textField(label, value, onChange, { silent = false } = {}) {
  const f = el("div", { class: "field" });
  f.append(el("label", {}, label));
  const input = el("input", { value, spellcheck: "false" });
  input.addEventListener("change", () => {
    input.classList.remove("invalid");
    onChange(input.value.trim(), input);
    if (!silent) changed({ structural: false });
  });
  f.append(input);
  return f;
}

// Edit obj[name] according to its schema entry; values are typed as JSON.
function paramField(obj, name, meta, { silent = false } = {}) {
  const changed = silent ? () => {} : changedModel;
  const f = el("div", { class: meta.type === "bool" ? "field check" : "field" });
  const has = Object.prototype.hasOwnProperty.call(obj, name);
  if (meta.type === "string") {
    const label = el("label");
    label.append(el("span", {}, name), el("span", { class: "help" }, meta.help || ""));
    const input = el("input", { spellcheck: "false", placeholder: meta.default ?? "" });
    input.value = has ? obj[name] : "";
    input.addEventListener("change", () => {
      if (input.value.trim()) obj[name] = input.value.trim();
      else delete obj[name];
      changed({ structural: false });
    });
    f.append(label, input);
    return f;
  }
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
  const [types, libraries, templates] = await Promise.all([api.types(), api.libraries(), api.templates()]);
  state.types = Object.fromEntries(types.map((t) => [t.kind, t]));
  state.libraries = libraries;
  state.templates = Object.fromEntries(templates.map((t) => [t.id, t]));
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
