// The inspector for several selected entities: set their group or dynamic ports, or
// delete them together. The canvas handles the selecting (shift-click, shift-drag, Cmd/Ctrl-A).

import { el } from "./dom.js";
import { setEntityGroup } from "./groups.js";

// deps: {model, changed, remove, showBanner}
export function inspectMulti(root, ids, deps) {
  const ents = deps.model.entities.filter((e) => ids.includes(e.id));
  if (ents.length < 2) return false;
  root.append(el("h2", {}, "Selection"), el("h3", {}, `${ents.length} entities`));
  root.append(el("p", { class: "sub" }, ents.map((e) => e.id).join(", ")));
  root.append(el("p", { class: "sub" }, "Shift-click to add or remove one, shift-drag to add an area. Drag any of them to move all."));
  root.append(groupField(ents, deps), portsField(ents, deps), actions(deps));
  return true;
}

function field(label, input) {
  const f = el("div", { class: "field" });
  f.append(el("label", {}, label), input);
  return f;
}

function groupField(ents, deps) {
  const groups = new Set(ents.map((e) => e.group || ""));
  const input = el("input", { value: groups.size === 1 ? [...groups][0] : "", spellcheck: "false" });
  input.placeholder = groups.size === 1 ? "none" : "mixed";
  input.addEventListener("change", () => {
    const problem = ents.map((e) => setEntityGroup(deps.model, e, input.value.trim())).find(Boolean);
    if (problem) {
      input.classList.add("invalid");
      deps.showBanner(problem);
    }
    deps.changed();
  });
  return field("group (all of them)", input);
}

function portsField(ents, deps) {
  const input = el("input", { type: "checkbox" });
  const on = ents.filter((e) => e.dynamic_ports).length;
  input.checked = on === ents.length;
  input.indeterminate = on > 0 && on < ents.length;
  input.addEventListener("change", () => {
    for (const e of ents) {
      if (input.checked) e.dynamic_ports = true;
      else delete e.dynamic_ports;
    }
    deps.changed({ structural: false });
  });
  return field("dynamic ports (all of them)", input);
}

function actions(deps) {
  const row = el("div", { class: "row-actions" });
  const del = el("button", { class: "danger", title: "Delete (Del)" });
  del.append(el("i", { class: "fa-solid fa-trash" }), " Delete all");
  del.onclick = deps.remove;
  row.append(del);
  return row;
}
