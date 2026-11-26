// Port placement: each port sits on its node's border, facing what it is linked to.
//
// A position is a distance `s` along the border, clockwise from the top-left corner,
// so ports can glide around corners. A linked port aims at the centre of the node(s)
// at the other end of its links (never at their ports, so placement can't feed back
// on itself). An unlinked port, or any port of a node without `dynamic_ports`, keeps
// the classic spot: inputs left, outputs right.
// Ports of one node are then spread apart, so none sit on top of each other.

const CORNER = 14; // keep ports this far from a corner
const SPACING = 18; // minimum distance between two ports of one node, along the border
const SETTLE_MS = 90; // time constant of the glide towards the target

export const portKey = (id, port, isOut) => `${id}\u0000${isOut ? "o" : "i"}\u0000${port}`;

// The point at distance s along the border of a w x h box, with its outward normal.
export function borderPoint(s, w, h) {
  const p = 2 * (w + h);
  s = ((s % p) + p) % p;
  if (s < w) return { x: s, y: 0, nx: 0, ny: -1 };
  if (s < w + h) return { x: w, y: s - w, nx: 1, ny: 0 };
  if (s < 2 * w + h) return { x: w - (s - w - h), y: h, nx: 0, ny: 1 };
  return { x: 0, y: h - (s - 2 * w - h), nx: -1, ny: 0 };
}

// Where a ray from the box centre in direction (dx, dy) leaves the box, as s.
function borderParam(dx, dy, w, h) {
  const t = Math.min(dx ? w / 2 / Math.abs(dx) : Infinity, dy ? h / 2 / Math.abs(dy) : Infinity);
  const x = w / 2 + dx * t, y = h / 2 + dy * t;
  const edges = [
    [Math.abs(y), () => clampEdge(x, w)], // top
    [Math.abs(x - w), () => w + clampEdge(y, h)], // right
    [Math.abs(y - h), () => w + h + (w - clampEdge(x, w))], // bottom
    [Math.abs(x), () => 2 * w + h + (h - clampEdge(y, h))], // left
  ];
  return edges.reduce((best, e) => (e[0] < best[0] ? e : best))[1]();
}

function clampEdge(v, len) {
  return Math.min(len - CORNER, Math.max(CORNER, v));
}

// The classic spot: inputs spread down the left side, outputs down the right side.
function defaultParam(i, n, isOut, { w, h }) {
  const y = (h * (i + 1)) / (n + 1);
  return isOut ? w + y : 2 * w + h + (h - y);
}

// nodes: [{id, pos, dynamic_ports}] (others keep the classic spots); links: [{from: [nodeId, port], to: [nodeId, port]}] between them;
// portsOf(id) -> {inputs, outputs}; box: {w, h}. Returns Map(portKey -> target s).
export function layoutPorts({ nodes, links, portsOf, box }) {
  const aims = aimsOf(nodes, links);
  const targets = new Map();
  for (const node of nodes) {
    for (const { key, s } of nodeTargets(node, portsOf(node.id), aims, box)) targets.set(key, s);
  }
  return targets;
}

// portKey -> summed unit directions towards the nodes at the other ends of its links.
function aimsOf(nodes, links) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const aims = new Map();
  const aim = (node, port, isOut, other) => {
    const dx = other.pos[0] - node.pos[0], dy = other.pos[1] - node.pos[1];
    const len = Math.hypot(dx, dy);
    if (!len) return;
    const key = portKey(node.id, port, isOut);
    const sum = aims.get(key) || { x: 0, y: 0 };
    aims.set(key, { x: sum.x + dx / len, y: sum.y + dy / len });
  };
  for (const { from, to } of links) {
    const a = byId.get(from[0]), b = byId.get(to[0]);
    if (a && b) {
      aim(a, from[1], true, b);
      aim(b, to[1], false, a);
    }
  }
  return aims;
}

function nodeTargets(node, ports, aims, box) {
  if (!node.dynamic_ports) aims = new Map(); // nothing pulls its ports
  const mine = [];
  for (const [list, isOut] of [[ports.inputs, false], [ports.outputs, true]]) {
    list.forEach((port, i) => {
      const key = portKey(node.id, port, isOut);
      const a = aims.get(key);
      const aimed = a && Math.hypot(a.x, a.y) > 1e-6; // links pulling opposite ways cancel
      const s = aimed ? borderParam(a.x, a.y, box.w, box.h) : defaultParam(i, list.length, isOut, box);
      mine.push({ key, s });
    });
  }
  return spread(mine, 2 * (box.w + box.h));
}

// Push ports apart until neighbours are at least SPACING apart (around the loop).
function spread(ports, perimeter) {
  const gap = Math.min(SPACING, perimeter / Math.max(1, ports.length));
  const list = ports.map((p) => ({ ...p })).sort((a, b) => a.s - b.s);
  for (let pass = 0; pass < 8 && list.length > 1; pass++) {
    for (let i = 0; i < list.length; i++) pushApart(list[i], list[(i + 1) % list.length], gap, perimeter);
  }
  return list;
}

function pushApart(a, b, gap, perimeter) {
  const d = wrap(b.s - a.s, perimeter);
  if (d >= gap) return;
  a.s -= (gap - d) / 2;
  b.s += (gap - d) / 2;
}

function wrap(d, perimeter) {
  return ((d % perimeter) + perimeter) % perimeter;
}

// Glide each port's current s towards its target, the short way round the border.
// Returns whether any port is still moving.
export function settle(current, targets, dt, perimeter) {
  const k = 1 - Math.exp(-dt / SETTLE_MS);
  let moving = false;
  for (const [key, target] of targets) {
    const s = current.get(key);
    const next = s === undefined ? target : glide(s, target, k, perimeter); // new ports appear in place
    current.set(key, next);
    moving ||= next !== target;
  }
  for (const key of [...current.keys()]) if (!targets.has(key)) current.delete(key);
  return moving;
}

function glide(s, target, k, perimeter) {
  let d = wrap(target - s, perimeter);
  if (d > perimeter / 2) d -= perimeter;
  return Math.abs(d) < 0.3 ? target : s + d * k;
}
