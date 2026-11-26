// Auto-layout for entities without a position: columns by distance from the sources.

import { NODE_H, NODE_W, splitRef } from "./canvas.js";

// Place entities that have no position: columns by distance from the sources.
export function layout(model) {
  if (model.entities.some((e) => !e.pos)) layoutFragment(model.entities, model.connections, null);
}

// Lay out `entities` lacking pos in columns, starting at `origin` (or 0,0).
export function layoutFragment(entities, connections, origin) {
  const ox = origin ? origin.x - NODE_W / 2 : 0;
  const oy = origin ? origin.y - NODE_H / 2 : 0;
  const depth = depths(entities, connections);
  const todo = entities.filter((e) => !e.pos);
  const column = (e) => depth.get(e.id) ?? 0;
  const perColumn = countBy(todo, column);
  const rows = new Map();
  for (const e of todo) {
    const d = column(e);
    const r = rows.get(d) || 0;
    rows.set(d, r + 1);
    const centered = r - (perColumn.get(d) - 1) / 2; // center each column on the origin row
    e.pos = [Math.round(ox + d * (NODE_W + 90)), Math.round(oy + centered * (NODE_H + 60))];
  }
}

// Entity id -> links followed from the nearest entity with no incoming link (breadth first).
function depths(entities, connections) {
  const next = new Map();
  for (const c of connections) {
    const a = splitRef(c.from)[0];
    next.set(a, [...(next.get(a) || []), splitRef(c.to)[0]]);
  }
  const incoming = new Set(connections.map((c) => splitRef(c.to)[0]));
  const queue = entities.filter((e) => !incoming.has(e.id)).map((e) => e.id);
  const depth = new Map(queue.map((id) => [id, 0]));
  while (queue.length) {
    const id = queue.shift();
    for (const n of (next.get(id) || []).filter((x) => !depth.has(x))) {
      depth.set(n, depth.get(id) + 1);
      queue.push(n);
    }
  }
  return depth;
}

function countBy(items, key) {
  const counts = new Map();
  for (const item of items) counts.set(key(item), (counts.get(key(item)) || 0) + 1);
  return counts;
}
