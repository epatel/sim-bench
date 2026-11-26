// Inspector fields: one editor per parameter type (string, bool, enum, machine script),
// and JSON for numbers, distributions and objects.
import { el } from "./dom.js";

// Fields whose edits change what the inspector shows (ports, scenario list): redraw it.
const PORT_PARAMS = new Set(["routes", "machine", "error_port", "scenarios"]);

// Edit obj[name] according to its schema entry. One builder per field type;
// anything without its own builder is edited as JSON.
// paramFields(changedModel) returns paramField(obj, name, meta, {silent}).
export function paramFields(changedModel) {
  return function paramField(obj, name, meta, { silent = false } = {}) {
    const field = {
      obj, name, meta,
      has: Object.prototype.hasOwnProperty.call(obj, name),
      done: silent ? () => {} : () => changedModel({ structural: PORT_PARAMS.has(name) }),
    };
    return (FIELDS[meta.type] || jsonField)(field);
  };
}

const FIELDS = { string: stringField, bool: boolField, enum: enumField, machine: machineField, code: codeField };

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

// A state machine: a script (see the behaviors card). A machine still written as a
// mapping shows as JSON, and saves as a mapping again if the text stays JSON.
function machineField(field) {
  return codeArea(field, (text) => {
    if (!text.trim().startsWith("{")) return { value: text };
    try {
      return { value: JSON.parse(text) };
    } catch {
      return { error: "not JSON" };
    }
  });
}

// A script (e.g. the model's globals). Empty text removes it.
function codeField(field) {
  return codeArea(field, (text) => (text.trim() ? { value: text } : { empty: true }), 6);
}

// A monospace editor; parse(text) -> {value} | {empty} | {error}.
function codeArea({ obj, name, meta, has, done }, parse, minRows = 2) {
  const f = el("div", { class: "field" });
  const area = el("textarea", { spellcheck: "false", class: "code", wrap: "off" });
  const value = has && obj[name] !== null ? obj[name] : meta.default;
  area.value = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  area.placeholder = meta.placeholder || "";
  area.rows = Math.max(minRows, Math.min(24, area.value.split("\n").length + 1));
  area.addEventListener("keydown", (ev) => indentKeys(ev, area));
  area.addEventListener("change", () => store({ obj, name, done }, area, parse(area.value)));
  f.append(fieldLabel(name, meta.help), area);
  return f;
}

// Tab indents by two spaces (and shift-tab removes them) instead of leaving the editor.
function indentKeys(ev, area) {
  if (ev.key !== "Tab") return;
  ev.preventDefault();
  const { selectionStart: start, value } = area;
  const lineStart = value.lastIndexOf("\n", start - 1) + 1;
  if (ev.shiftKey) {
    if (value.startsWith("  ", lineStart)) {
      area.setRangeText("", lineStart, lineStart + 2, "end");
    }
    return;
  }
  area.setRangeText("  ", start, area.selectionEnd, "end");
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

function applyJson(field, input) {
  const text = input.value.trim();
  store(field, input, text === "" ? { empty: true } : parseTyped(text, field.meta.type));
}

// Save a parsed edit ({value} | {empty} | {error}) into obj[name], marking errors.
function store({ obj, name, done }, input, parsed) {
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
