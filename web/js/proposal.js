// Comparing the agent's proposed model with the current one (pure functions).

const LISTS = ["entities", "connections", "groups", "scenarios"];

// Fill in the lists and params a model may omit.
export function normalize(model) {
  for (const key of LISTS) model[key] ??= [];
  for (const e of model.entities) e.params ??= {};
  return model;
}

// entity id -> position, for keeping things where the user saw them
export function positionsOf(model) {
  return new Map((model?.entities || []).map((e) => [e.id, e.pos]));
}

export function adoptPositions(model, positions, { overwrite = false } = {}) {
  const wanted = (e) => positions.has(e.id) && (overwrite || !e.pos);
  for (const e of model.entities.filter(wanted)) e.pos = positions.get(e.id);
}

// The proposal as a preview: its model (existing entities keep their place), each
// entity's status ("added" / "changed" / null) and the entities it would remove.
export function buildPreview(current, proposed) {
  const model = normalize(structuredClone(proposed));
  const before = new Map(current.entities.map((e) => [e.id, e]));
  adoptPositions(model, positionsOf(current), { overwrite: true });
  const status = new Map(model.entities.map((e) => [e.id, statusOf(before.get(e.id), e)]));
  const ids = new Set(model.entities.map((e) => e.id));
  const removed = current.entities.filter((e) => !ids.has(e.id));
  const count = (kind) => [...status.values()].filter((v) => v === kind).length;
  return { model, status, removed, counts: { added: count("added"), changed: count("changed"), removed: removed.length } };
}

function statusOf(old, e) {
  if (!old) return "added";
  const key = (x) => JSON.stringify([x.type, x.params || {}, x.group || null]);
  return key(old) === key(e) ? null : "changed";
}
