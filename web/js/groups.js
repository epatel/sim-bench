// Group membership edits on a model: put an entity in a group, or take it out, and keep
// each touched group's GroupSpec in step (see cards/composites.md).
//
// A group's boundary is the set of member ports that links cross. After a change it is
// worked out again from the links. Boundary names that still map to a member port are
// kept, so a stand-in fitted to them stays valid. Ports no link touches yet (e.g. those
// a template declared) are kept too. Ports now wired only inside the group are dropped.
// A group left with no members loses its spec.

import { splitRef } from "./canvas.js";

export function groupSpec(model, id) {
  return (model.groups || []).find((g) => g.id === id);
}

export function removeGroupSpec(model, id) {
  model.groups = (model.groups || []).filter((g) => g.id !== id);
}

// Rewrite "group.port" references to the inner ports they map to.
export function inlineBoundaryRefs(model, g) {
  for (const c of model.connections) {
    const [fe, fp] = splitRef(c.from);
    if (fe === g.id && g.outputs?.[fp]) c.from = g.outputs[fp];
    const [te, tp] = splitRef(c.to);
    if (te === g.id && g.inputs?.[tp]) c.to = g.inputs[tp];
  }
}

// Why `gid` can't be a group name, or "" when it can.
export function groupNameProblem(model, gid) {
  if (gid.includes(".")) return "a group name can't contain '.'";
  if (model.entities.some((e) => e.id === gid)) return `${gid} is already an entity id`;
  return "";
}

// Move entity `e` into group `gid` ("" takes it out). Returns an error message, or "".
export function setEntityGroup(model, e, gid) {
  const old = e.group || "";
  if (gid === old) return "";
  const problem = gid ? groupNameProblem(model, gid) : "";
  if (problem) return problem;
  if (gid) e.group = gid;
  else delete e.group;
  for (const id of [old, gid]) if (id) syncGroup(model, id);
  return "";
}

// Bring a group's spec in line with its members and the links that cross into it.
function syncGroup(model, gid) {
  const members = new Set(model.entities.filter((e) => e.group === gid).map((e) => e.id));
  let g = groupSpec(model, gid);
  if (g) inlineBoundaryRefs(model, g);
  if (!members.size) return removeGroupSpec(model, gid);
  if (!g) {
    g = { id: gid, inputs: {}, outputs: {} };
    model.groups = [...(model.groups || []), g];
  }
  const inside = (ref) => members.has(splitRef(ref)[0]);
  const links = model.connections;
  const internal = new Set(links.filter((c) => inside(c.from) && inside(c.to)).flatMap((c) => [c.from, c.to]));
  const keep = (ref) => inside(ref) && !internal.has(ref); // still a member's port, not wired inside
  g.inputs = boundary(g.inputs, keep, links.filter((c) => inside(c.to) && !inside(c.from)).map((c) => c.to));
  g.outputs = boundary(g.outputs, keep, links.filter((c) => inside(c.from) && !inside(c.to)).map((c) => c.from));
}

// One side of a boundary: the old names `keep` accepts, plus a name for each
// newly crossed port (its own port name, or entity_port when that is taken).
function boundary(old, keep, crossed) {
  const side = Object.fromEntries(Object.entries(old || {}).filter(([, ref]) => keep(ref)));
  const mapped = new Set(Object.values(side));
  for (const ref of crossed) {
    if (mapped.has(ref)) continue;
    const [ent, port] = splitRef(ref);
    let name = side[port] ? `${ent}_${port}` : port;
    for (let n = 2; side[name]; n++) name = `${ent}_${port}${n}`;
    side[name] = ref;
    mapped.add(ref);
  }
  return side;
}
