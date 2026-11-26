// App wiring: toolbar, palette, inspector, validation and the live run.
import { api, RunClient } from "./api.js";
import { Editor, NODE_H, NODE_W, fmt, linkKey, splitRef } from "./canvas.js";
import { loadIconFont } from "./icons.js";
import { AgentChat } from "./agent.js";
import { el } from "./dom.js";
import { adoptPositions, buildPreview, normalize, positionsOf } from "./proposal.js";
import { sourceSection } from "./provenance.js";
import { Results } from "./results.js";
import { wireRunControls } from "./runbar.js";

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
  finished: false, // the last run has nothing left to do; play restarts it
  speed: 1,
  speedAuto: true, // follow the server's suggested speed until the slider is moved
  suggestedSpeed: null,
};

function emptyModel() {
  return { name: "untitled", description: "", entities: [], connections: [] };
}

// --- the object the canvas talks to ------------------------------------------

const app = {
  get model() { return state.proposal ? state.proposal.model : state.model; },
  diffOf(id) { return state.proposal?.status.get(id); },
  get ghosts() { return state.proposal?.removed || []; },
  get types() { return state.types; },
  get summary() { return state.summary; },

  portsOf(id) {
    const g = groupSpec(id); // a collapsed group shows its boundary ports
    if (g) return { inputs: Object.keys(g.inputs || {}), outputs: Object.keys(g.outputs || {}) };
    if (state.ports[id]) return state.ports[id];
    const e = state.model.entities.find((x) => x.id === id);
    const t = e && state.types[e.type];
    return t ? { inputs: t.inputs, outputs: t.outputs } : { inputs: [], outputs: [] };
  },

  addEntity(kind, pos) {
    if (state.proposal) return showBanner("Accept or reject the agent's proposal first.");
    const id = uniqueId(kind);
    state.model.entities.push({ id, type: kind, params: {}, pos: pos.map((v) => Math.round(v)) });
    editor.select({ kind: "entity", id });
    changed();
  },

  toggleGroup(id) {
    const g = groupSpec(id);
    if (!g) return showBanner(`Group ${id} has no boundary yet. Select it and make it a composite first.`);
    if (g.mode === "abstract") g.mode = "expanded";
    else if (!g.abstract) return showBanner(`Calibrate or set an abstract behavior for ${id} first.`);
    else g.mode = "abstract";
    editor.select({ kind: "group", id });
    changed();
  },

  insertTemplate(id, pos) {
    if (state.proposal) return showBanner("Accept or reject the agent's proposal first.");
    insertTemplate(id, {}, null, { x: pos[0] + NODE_W / 2, y: pos[1] + NODE_H / 2 });
  },

  connect(from, to) {
    if (state.proposal) return showBanner("Accept or reject the agent's proposal first.");
    const conns = state.model.connections.filter((c) => c.from !== from); // one link per output
    conns.push({ from, to });
    state.model.connections = conns;
    editor.select({ kind: "link", id: `${from}->${to}` });
    changed();
  },

  deleteSelection() {
    if (state.proposal) return showBanner("Accept or reject the agent's proposal first.");
    const remove = REMOVERS[editor.selection?.kind];
    if (!remove) return;
    remove(editor.selection.id);
    editor.select(null);
    changed();
  },
};

// How to delete each kind of selection from the model.
const REMOVERS = {
  entity: (id) => removeEntities(new Set([id])),
  group: (id) => {
    const members = state.model.entities.filter((e) => e.group === id).map((e) => e.id);
    removeGroupSpec(id);
    removeEntities(new Set([...members, id])); // the id also drops links to "group.port"
  },
  link: (id) => {
    state.model.connections = state.model.connections.filter((c) => linkKey(c) !== id);
  },
};

function removeEntities(gone) {
  state.model.entities = state.model.entities.filter((e) => !gone.has(e.id));
  state.model.connections = state.model.connections.filter(
    (c) => !gone.has(splitRef(c.from)[0]) && !gone.has(splitRef(c.to)[0]),
  );
}

const editor = new Editor($("canvas"), app);
const results = new Results($("results"));
const chat = new AgentChat($("agent"), {
  getModel: () => state.model,
  applyModel: applyAgentModel,
  onBatch: (id) => results.show(id),
  onProposal: previewProposal,
  onProposalResolved: () => endPreview(),
});

// --- proposals: preview the agent's suggested model as a diff ---------------------

function previewProposal(p) {
  const preview = buildPreview(state.model, p.model);
  layoutFragment(preview.model.entities, preview.model.connections, viewCenter());
  state.proposal = preview;
  showBanner("Reviewing the agent's proposal: accept or reject it in the chat.", "info");
  api.validate(preview.model).then((res) => res.ok && Object.assign(state.ports, res.ports));
  if (!state.model.entities.length) requestAnimationFrame(() => editor.fit());
  return preview.counts;
}

function endPreview() {
  const positions = positionsOf(state.proposal?.model);
  state.proposal = null;
  adoptPositions(state.model, positions, { overwrite: true });
  editor.select(null);
  hideBanner();
}

// The agent changed the model: adopt it (the file path stays), place new entities.
function applyAgentModel(model) {
  const wasEmpty = state.model.entities.length === 0;
  normalize(model);
  adoptPositions(model, positionsOf(state.proposal?.model)); // where the preview showed them
  layout(model);
  state.model = model;
  if (editor.selection && !selectionExists(editor.selection)) editor.select(null);
  changed();
  if (wasEmpty) requestAnimationFrame(() => editor.fit());
}

function selectionExists(sel) {
  if (sel.kind === "entity") return state.model.entities.some((e) => e.id === sel.id);
  if (sel.kind === "link") return state.model.connections.some((c) => linkKey(c) === sel.id);
  if (sel.kind === "group") return state.model.groups.some((g) => g.id === sel.id);
  return true;
}

for (const tab of document.querySelectorAll(".tabs button")) {
  tab.onclick = () => {
    for (const t of document.querySelectorAll(".tabs button")) t.classList.toggle("on", t === tab);
    $("inspector").classList.toggle("hidden", tab.dataset.tab !== "inspector");
    $("agent").classList.toggle("hidden", tab.dataset.tab !== "agent");
  };
}
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

// --- groups (composites) ----------------------------------------------------------

function groupSpec(id) {
  return (state.model.groups || []).find((g) => g.id === id);
}

function removeGroupSpec(id) {
  state.model.groups = (state.model.groups || []).filter((g) => g.id !== id);
}

// Rewrite "group.port" references to the inner ports they map to.
function inlineBoundaryRefs(g) {
  for (const c of state.model.connections) {
    const [fe, fp] = splitRef(c.from);
    if (fe === g.id && g.outputs?.[fp]) c.from = g.outputs[fp];
    const [te, tp] = splitRef(c.to);
    if (te === g.id && g.inputs?.[tp]) c.to = g.inputs[tp];
  }
}

function uniqueId(base) {
  const ids = new Set(state.model.entities.map((e) => e.id));
  for (let i = 1; ; i++) if (!ids.has(`${base}${i}`)) return `${base}${i}`;
}

function isFreeId(id) {
  return Boolean(id) && !id.includes(".") && !state.model.entities.some((e) => e.id === id);
}

// Apply `fix` to every "entity.port" reference in the model.
function rewriteRefs(fix) {
  for (const c of state.model.connections) {
    c.from = fix(c.from);
    c.to = fix(c.to);
  }
  const sides = (state.model.groups || []).flatMap((g) => [g.inputs || {}, g.outputs || {}]);
  for (const side of sides) for (const k of Object.keys(side)) side[k] = fix(side[k]);
}

function renameEntity(oldId, newId) {
  if (!isFreeId(newId)) return false;
  state.model.entities.find((x) => x.id === oldId).id = newId;
  rewriteRefs((ref) => {
    const [ent, port] = splitRef(ref);
    return ent === oldId ? `${newId}.${port}` : ref;
  });
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
  normalize(model);
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
  state.model.groups.push({ id: frag.group, template: id, inputs: frag.inputs, outputs: frag.outputs });
  editor.select({ kind: "group", id: frag.group });
  changed();
}

// --- inspector -----------------------------------------------------------------

// One inspector per selection kind; each returns false when its target is gone.
const INSPECTORS = {
  entity(root, id) {
    const e = state.model.entities.find((x) => x.id === id);
    return e && inspectEntity(root, e) !== false;
  },
  link(root, id) {
    const c = state.model.connections.find((x) => linkKey(x) === id);
    return c && inspectLink(root, c) !== false;
  },
  template(root, id) {
    const t = state.templates[id];
    return t && inspectTemplate(root, t) !== false;
  },
  group(root, id) {
    const members = state.model.entities.filter((e) => e.group === id);
    if (!members.length && !groupSpec(id)) return false;
    inspectGroup(root, id, members);
    return true;
  },
};

function renderInspector() {
  const root = $("inspector");
  root.innerHTML = "";
  const sel = editor.selection;
  const shown = sel && INSPECTORS[sel.kind]?.(root, sel.id);
  if (!shown) inspectModel(root);
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

function inspectGroup(root, gid, members) {
  const g = groupSpec(gid);
  root.append(el("h2", {}, g ? "Composite" : "Group"));
  const title = el("h3");
  title.append(el("i", { class: "fa-solid fa-layer-group" }), ` ${g?.name || gid}`);
  root.append(title);
  if (!g) {
    root.append(el("p", { class: "sub" }, `A visual group of ${members.length} entities. Make it a composite to give it boundary ports and an abstract mode.`));
    root.append(groupActions(gid, members, [button("fa-cubes", "Make composite", () => {
      state.model.groups.push({ id: gid, inputs: {}, outputs: {} });
      changed();
    })]));
    return root.append(membersTable(members));
  }
  if (g.template) root.append(el("p", { class: "sub" }, `From template ${g.template}. Double-click it on the canvas to collapse or expand it.`));
  root.append(modeSwitch(g));
  root.append(el("h2", {}, "Boundary"));
  root.append(paramField(g, "inputs", { type: "object", default: {}, help: "boundary port → inner entity.port" }));
  root.append(paramField(g, "outputs", { type: "object", default: {}, help: "boundary port → inner entity.port" }));
  root.append(abstractEditor(g));
  root.append(calibrationPanel(g));
  root.append(el("h2", {}, "Metrics"), el("div", { id: "metrics" }));
  renderMetrics();
  root.append(el("h2", {}, "Members"), membersTable(members));
  root.append(groupActions(gid, members, [button("fa-crosshairs", "Focus", () => focusGroup(gid), "Expand this group and collapse every other group that has an abstract behavior")]));
}

function button(icon, label, onclick, title = "") {
  const b = el("button", { title });
  b.append(el("i", { class: `fa-solid ${icon}` }), ` ${label}`);
  b.onclick = onclick;
  return b;
}

function membersTable(members) {
  const table = el("table", { class: "metrics" });
  for (const e of members) table.append(row(e.id, e.type));
  return table;
}

function groupActions(gid, members, extra = []) {
  const actions = el("div", { class: "row-actions" });
  const ungroup = button("fa-object-ungroup", "Ungroup", () => {
    const g = groupSpec(gid);
    if (g) {
      inlineBoundaryRefs(g);
      removeGroupSpec(gid);
    }
    for (const e of members) delete e.group;
    editor.select(null);
    changed();
  }, "Keep the entities, drop the group");
  const del = button("fa-trash", "Delete all", () => app.deleteSelection());
  del.classList.add("danger");
  actions.append(...extra, ungroup, del);
  return actions;
}

function modeSwitch(g) {
  const wrap = el("div", { class: "segmented" });
  for (const mode of ["expanded", "abstract"]) {
    const current = g.mode || "expanded";
    const b = el("button", { class: current === mode ? "on" : "" }, mode);
    b.disabled = mode === "abstract" && !g.abstract;
    b.title = b.disabled ? "Calibrate or set an abstract behavior first" : "";
    b.onclick = () => {
      g.mode = mode;
      changed();
    };
    wrap.append(b);
  }
  return wrap;
}

function abstractEditor(g) {
  const box = el("div");
  box.append(el("h2", {}, "Abstract behavior"));
  if (!g.abstract) {
    box.append(el("p", { class: "sub" }, "None yet. Calibrate from an expanded run, or pick a type."));
  }
  const f = el("div", { class: "field" });
  f.append(el("label", {}, "stand-in type"));
  const s = el("select");
  s.append(el("option", { value: "" }, "—"));
  for (const id of Object.keys(state.types)) s.append(el("option", { value: id }, id));
  s.value = g.abstract?.type || "";
  s.onchange = () => {
    if (!s.value) delete g.abstract;
    else g.abstract = { type: s.value, params: {} };
    if (!g.abstract && g.mode === "abstract") g.mode = "expanded";
    changed();
  };
  f.append(s);
  box.append(f);
  if (g.abstract) {
    box.append(paramField(g.abstract, "params", { type: "object", default: {}, help: "stand-in parameters" }));
    const cal = g.abstract.calibration;
    if (cal) {
      const t = el("table", { class: "metrics" });
      t.append(...calibrationRows(cal));
      box.append(t, el("p", { class: "sub" }, cal.note));
    }
  }
  return box;
}

function calibrationRows(cal) {
  const rows = [
    row("calibrated at", `t=${fmt(cal.measured_until)}, seed ${cal.seed}`),
    row("samples", fmt(cal.samples)),
    row("arrival rate", fmt(cal.arrival_rate)),
    row("residence mean", fmt(cal.residence_mean)),
    row("residence p95", fmt(cal.residence_p95)),
    row("loss", fmt(cal.loss)),
  ];
  if (cal.capacity != null) {
    rows.push(
      row("capacity", `${fmt(cal.capacity)}/s`),
      row("bottleneck", `${cal.bottleneck} (${fmt(cal.bottleneck_utilization * 100)}% busy)`),
    );
    if (cal.predicted_residence != null) rows.push(row("fitted residence", fmt(cal.predicted_residence)));
  }
  return rows;
}

const STAND_IN_HELP = "server: saturates at the group's capacity; delay: latency at this load only";

function calibrationPanel(g) {
  const box = el("div");
  box.append(el("h2", {}, "Calibrate"));
  const opts = { until: 2000, kind: "auto" };
  box.append(paramField(opts, "until", { type: "float", default: 2000, help: "simulated time to measure" }, { silent: true }));
  box.append(paramField(opts, "kind", { type: "enum", options: ["auto", "server", "delay"], default: "auto", help: STAND_IN_HELP }, { silent: true }));
  const out = el("p", { class: "sub" });
  const go = button("fa-ruler-combined", "Calibrate from expanded run", () => runCalibration(g, opts, go, out));
  const actions = el("div", { class: "row-actions" });
  actions.append(go);
  box.append(actions, out);
  return box;
}

function hasBoundary(g) {
  return Object.keys(g.inputs || {}).length > 0 && Object.keys(g.outputs || {}).length > 0;
}

async function runCalibration(g, opts, go, out) {
  if (!hasBoundary(g)) {
    out.textContent = "Set boundary inputs and outputs first.";
    return;
  }
  go.disabled = true;
  out.textContent = "Running…";
  try {
    const res = await api.calibrate(state.model, g.id, {
      until: opts.until ?? 2000, seed: Number($("seed").value) || 0, kind: opts.kind,
    });
    g.abstract = res.abstract;
    changed();
    const early = res.truncated ? " (stopped early: event cap)" : "";
    const cap = res.abstract.calibration.capacity;
    const limit = cap != null ? `, capacity ${fmt(cap)}/s` : ", no capacity limit";
    showBanner(`Calibrated ${g.id} as ${res.abstract.type}: mean residence ${fmt(res.probe.residence.mean)} over ${fmt(res.probe.exited)} messages${limit}${early}`, "info");
  } catch (e) {
    out.textContent = e.message;
    go.disabled = false;
  }
}

function focusGroup(gid) {
  for (const g of state.model.groups) {
    if (g.id === gid) g.mode = "expanded";
    else if (g.abstract) g.mode = "abstract";
  }
  editor.select({ kind: "group", id: gid });
  changed();
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
  root.append(scenariosPanel());
}

// --- scenarios: what-if variants of the model -------------------------------------

function scenariosPanel() {
  const box = el("div");
  box.append(el("h2", {}, "Scenarios"));
  const list = state.model.scenarios || [];
  if (!list.length) box.append(el("p", { class: "sub" }, "None yet. Add one as JSON below."));
  for (const sc of list) {
    const item = el("div", { class: "scenario" });
    item.append(el("div", { class: "k" }, sc.name ? `${sc.id} · ${sc.name}` : sc.id));
    for (const p of sc.patches || []) item.append(el("div", { class: "d" }, describePatch(p)));
    box.append(item);
  }
  box.append(paramField(state.model, "scenarios", {
    type: "object", default: [], help: "patches: {entity, param, value} · {link, field, value} · {group, mode}; add at: t to time one",
  }));
  box.append(batchControls(list), whatifControls(list));
  return box;
}

function describePatch(p) {
  const when = p.at !== undefined && p.at !== null ? `at t=${p.at}: ` : "";
  if (p.entity) return `${when}${p.entity}.${p.param} = ${JSON.stringify(p.value)}`;
  if (p.link) return `${when}link ${p.link} ${p.field} = ${JSON.stringify(p.value)}`;
  return `group ${p.group} → ${p.mode}`;
}

function batchControls(list) {
  const box = el("div", { class: "batch" });
  const opts = { seeds: 5, until: 1000 };
  box.append(el("h2", {}, "Compare over seeds"));
  box.append(paramField(opts, "seeds", { type: "int", default: 5, help: "runs per scenario" }, { silent: true }));
  box.append(paramField(opts, "until", { type: "float", default: 1000, help: "simulated time per run" }, { silent: true }));
  const go = button("fa-layer-group", `Run baseline + ${list.length} scenario${list.length === 1 ? "" : "s"}`, async () => {
    try {
      const scenarios = ["baseline", ...list.map((s) => s.id)];
      const res = await api.startBatch({ model: state.model, scenarios, seeds: opts.seeds ?? 5, until: opts.until ?? 1000 });
      results.show(res.id);
    } catch (e) {
      showBanner(e.message);
    }
  });
  const actions = el("div", { class: "row-actions" });
  actions.append(go);
  box.append(actions);
  return box;
}

function whatifControls(list) {
  const box = el("div");
  box.append(el("h2", {}, "What if, from here"));
  if (!list.length) return box;
  box.append(el("p", { class: "sub" }, "Pause a run, then fork it: the same state and random numbers, with and without the scenario."));
  const f = el("div", { class: "field" });
  f.append(el("label", {}, "scenario"));
  const pick = el("select");
  for (const sc of list) pick.append(el("option", { value: sc.id }, sc.name || sc.id));
  f.append(pick);
  const opts = { horizon: 100 };
  box.append(f, paramField(opts, "horizon", { type: "float", default: 100, help: "how far to run both forks" }, { silent: true }));
  const go = button("fa-code-fork", "Fork and compare", () => {
    if (!state.runLoaded || state.runStale) return showBanner("Start (and pause) a run of the current model first.");
    run.whatif(pick.value, opts.horizon ?? 100);
    showBanner(`What-if ${pick.value}: running both forks…`, "info");
  });
  const actions = el("div", { class: "row-actions" });
  actions.append(go);
  box.append(actions);
  return box;
}

function inspectEntity(root, e) {
  const type = state.types[e.type];
  entityHeader(root, e, type);

  const idField = textField("id", e.id, (v, input) => {
    if (!renameEntity(e.id, v)) input.classList.add("invalid");
  });
  root.append(idField);
  root.append(textField("label", e.name || "", (v) => { if (v) e.name = v; else delete e.name; }));

  if (type && Object.keys(type.params).length) root.append(el("h2", {}, "Parameters"));
  for (const [name, meta] of Object.entries(type?.params || {})) root.append(paramField(e.params, name, meta));
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

function entityHeader(root, e, type) {
  root.append(el("h2", {}, "Entity"));
  const title = el("h3");
  title.append(el("i", { class: `fa-solid ${type?.icon || "fa-circle-question"}` }), ` ${e.id}`);
  root.append(title, el("p", { class: "sub" }, type ? type.description : `unknown type ${e.type}`));
  if (type?.chain.length > 1) {
    const chain = el("p", { class: "sub chain" });
    chain.append(el("i", { class: "fa-solid fa-sitemap" }), ` ${type.chain.join(" → ")}`);
    root.append(chain);
  }
  if (e.group) root.append(el("p", { class: "sub" }, `In group ${e.group}`));
  const refs = [...(e.source || []), ...(type?.source || [])];
  if (refs.length) root.append(sourceSection(refs));
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

// Fields whose edits change what the inspector shows (ports, scenario list): redraw it.
const PORT_PARAMS = new Set(["routes", "machine", "error_port", "scenarios"]);

// Edit obj[name] according to its schema entry. One builder per field type;
// anything without its own builder is edited as JSON.
function paramField(obj, name, meta, { silent = false } = {}) {
  const field = {
    obj, name, meta,
    has: Object.prototype.hasOwnProperty.call(obj, name),
    done: silent ? () => {} : () => changedModel({ structural: PORT_PARAMS.has(name) }),
  };
  return (FIELDS[meta.type] || jsonField)(field);
}

const FIELDS = { string: stringField, bool: boolField, enum: enumField };

function fieldLabel(name, help) {
  const label = el("label");
  label.append(el("span", {}, name), el("span", { class: "help" }, help || ""));
  return label;
}

function stringField({ obj, name, meta, has, done }) {
  const f = el("div", { class: "field" });
  const input = el("input", { spellcheck: "false", placeholder: meta.default ?? "" });
  input.value = has ? obj[name] : "";
  input.addEventListener("change", () => {
    if (input.value.trim()) obj[name] = input.value.trim();
    else delete obj[name];
    done();
  });
  f.append(fieldLabel(name, meta.help), input);
  return f;
}

function boolField({ obj, name, meta, has, done }) {
  const f = el("div", { class: "field check" });
  const label = el("label");
  const box = el("input", { type: "checkbox" });
  box.checked = has ? !!obj[name] : !!meta.default;
  box.addEventListener("change", () => {
    if (box.checked === meta.default) delete obj[name];
    else obj[name] = box.checked;
    done();
  });
  label.append(box, `${name}`, el("span", { class: "help" }, meta.help || ""));
  f.append(label);
  return f;
}

function enumField({ obj, name, meta, has, done }) {
  const f = el("div", { class: "field" });
  const s = el("select");
  for (const o of meta.options) s.append(el("option", { value: o }, o));
  s.value = has ? obj[name] : meta.default;
  s.addEventListener("change", () => {
    obj[name] = s.value;
    done();
  });
  f.append(fieldLabel(name, meta.help), s);
  return f;
}

const DIST_HINT = 'A number, or e.g. {"dist":"exp","mean":1}, {"dist":"uniform","low":0,"high":2}';

// Numbers, distributions and objects, typed as JSON. Objects (e.g. a state machine)
// get a multi-line editor that starts from the default.
function jsonField(field) {
  const f = el("div", { class: "field" });
  const input = jsonInput(field);
  input.addEventListener("change", () => applyJson(field, input));
  f.append(fieldLabel(field.name, field.meta.help), input);
  return f;
}

function jsonInput({ obj, name, meta, has }) {
  if (meta.type === "object") {
    const area = el("textarea", { spellcheck: "false" });
    area.value = JSON.stringify(has && obj[name] !== null ? obj[name] : meta.default, null, 2) ?? "";
    area.rows = Math.min(18, area.value.split("\n").length + 1);
    return area;
  }
  const input = el("input", { spellcheck: "false" });
  input.placeholder = meta.default === null ? "empty" : JSON.stringify(meta.default);
  if (meta.type === "dist") input.title = DIST_HINT;
  input.value = has && obj[name] !== null ? JSON.stringify(obj[name]) : "";
  return input;
}

function applyJson({ obj, name, meta, done }, input) {
  const text = input.value.trim();
  const parsed = text === "" ? { empty: true } : parseTyped(text, meta.type);
  input.classList.toggle("invalid", Boolean(parsed.error));
  if (parsed.error) return;
  if (parsed.empty) delete obj[name];
  else obj[name] = parsed.value;
  done();
}

// JSON text -> {value} or {error}, checking numbers for int/float fields.
function parseTyped(text, type) {
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    return { error: "not JSON" };
  }
  const numeric = type === "int" || type === "float";
  if (numeric && typeof value !== "number") return { error: "not a number" };
  if (type === "int" && !Number.isInteger(value)) return { error: "not an integer" };
  return { value };
}

// The slice of a run summary that belongs to the current selection.
const METRICS = {
  entity: (s, id) => s.entities[id],
  link: (s, id) => s.links[id],
  // an abstract group's stand-in has entity metrics; an expanded one has a boundary probe
  group: (s, id) => (groupSpec(id)?.mode === "abstract" ? s.entities[id] : s.groups?.[id]),
};

function metricsFor(sel, s) {
  const pick = sel && METRICS[sel.kind];
  return pick ? pick(s, sel.id) : { time: s.time, events: s.events, pending: s.pending, seed: s.seed };
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
  const data = metricsFor(sel, s);
  if (!data) {
    root.append(el("p", { class: "empty" }, "Not in the running model. Press stop to include it."));
    return;
  }
  if (data.states) root.append(stateChips(data.states, data.state));
  const table = el("table", { class: "metrics" });
  for (const [name, m] of Object.entries(data)) {
    if (name === "type" || name === "states") continue;
    table.append(row(name, describe(m)));
  }
  root.append(table);
}

// A machine's states, with the current one lit.
function stateChips(states, current) {
  const wrap = el("div", { class: "chips" });
  for (const s of states) wrap.append(el("span", { class: s === current ? "chip on" : "chip" }, s));
  return wrap;
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

// --- run controls (runbar.js) ---------------------------------------------------

$("btn-fit").onclick = () => editor.fit();
wireRunControls({ state, editor, run, results, renderMetrics, showBanner, setStatus });

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
