// Canvas editor and live view: entities, ports, links, messages in flight.
import { glyph, ICON_FONT } from "./icons.js";
import { borderPoint, layoutPorts, portKey, settle } from "./ports.js";

export const NODE_W = 136;
export const NODE_H = 60;
const PORT_R = 5;
const GROUP_PAD = 18;
const GROUP_HEADER = 22;
const MAX_FLIGHTS = 3000;
const MIN_FLIGHT_MS = 350; // zero-latency hops stay visible this long
const STEP_HOP_MS = 450; // a stepped delivery travels its link this long, then pulses
const FLASH_MS = 450;
const DOT_R = 4;
const BOX = { w: NODE_W, h: NODE_H }; // for port placement
const PERIMETER = 2 * (BOX.w + BOX.h);

const C = {
  grid: "#1a2029", node: "#18202a", nodeLine: "#2c3644", text: "#d7dde6", muted: "#8591a3",
  link: "#3b4757", linkDown: "#ff6b6b", accent: "#f5b13d", select: "#4cc2ff",
  port: "#556274", portHot: "#4cc2ff", bad: "#ff6b6b", ok: "#5fd39a",
};

export function linkKey(conn) {
  return conn.id || `${conn.from}->${conn.to}`;
}

export class Editor extends EventTarget {
  constructor(canvas, app) {
    super();
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.app = app; // { model, portsOf(id), summary, addEntity, connect, deleteSelection }
    this.view = { x: 0, y: 0, scale: 1 };
    this.selection = null; // {kind: "entity"|"link"|"group", id} or {kind: "multi", id: [entity ids]}
    this.drag = null;
    this.hoverPort = null;
    this.flights = [];
    this.flashes = [];
    this.clock = { now: 0, wall: performance.now(), playing: false, speed: 1 };
    this.portAt = new Map(); // portKey -> current position along the node border
    this.lastFrame = performance.now();
    this.bindEvents();
    new ResizeObserver(() => this.resize()).observe(canvas);
    this.resize();
    requestAnimationFrame((t) => this.frame(t));
  }

  // --- coordinates ------------------------------------------------------

  resize() {
    const r = this.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.max(1, r.width * dpr);
    this.canvas.height = Math.max(1, r.height * dpr);
    this.dpr = dpr;
  }

  toWorld(ev) {
    const r = this.canvas.getBoundingClientRect();
    return {
      x: (ev.clientX - r.left - this.view.x) / this.view.scale,
      y: (ev.clientY - r.top - this.view.y) / this.view.scale,
    };
  }

  fit() {
    const ents = this.nodes();
    const r = this.canvas.getBoundingClientRect();
    if (!ents.length) { this.view = { x: r.width / 2, y: r.height / 2, scale: 1 }; return; }
    const xs = ents.map((e) => e.pos[0]), ys = ents.map((e) => e.pos[1]);
    const x0 = Math.min(...xs) - 60, x1 = Math.max(...xs) + NODE_W + 60;
    const y0 = Math.min(...ys) - 60, y1 = Math.max(...ys) + NODE_H + 80;
    const scale = Math.min(1.4, r.width / (x1 - x0), r.height / (y1 - y0));
    this.view = {
      scale,
      x: (r.width - (x1 - x0) * scale) / 2 - x0 * scale,
      y: (r.height - (y1 - y0) * scale) / 2 - y0 * scale,
    };
  }

  // --- geometry ---------------------------------------------------------

  // A port's point on its node's border, with the outward normal (nx, ny).
  portPos(entity, port, isOut) {
    // until the next frame places a new port: the middle of its classic side
    const s = this.portAt.get(portKey(entity.id, port, isOut)) ?? (isOut ? BOX.w + BOX.h / 2 : -BOX.h / 2);
    const b = borderPoint(s, BOX.w, BOX.h);
    return { x: entity.pos[0] + b.x, y: entity.pos[1] + b.y, nx: b.nx, ny: b.ny };
  }

  // Move every port towards the spot facing what it links to (see ports.js).
  placePorts(dt) {
    const links = [];
    for (const c of this.app.model.connections) {
      const from = this.endpoint(c.from, true), to = this.endpoint(c.to, false);
      if (from && to) links.push({ from, to });
    }
    const targets = layoutPorts({ nodes: this.nodes(), links, portsOf: (id) => this.app.portsOf(id), box: BOX });
    settle(this.portAt, targets, dt, PERIMETER);
  }

  // --- view layer: abstract groups are drawn as one collapsed node -------

  groupSpecs() {
    return new Map((this.app.model.groups || []).map((g) => [g.id, g]));
  }

  // Visible nodes: entities outside abstract groups, plus one node per abstract group.
  abstractSpecOf(entity) {
    const g = entity.group && this.groupSpecs().get(entity.group);
    return g?.mode === "abstract" ? g : null;
  }

  nodes() {
    const visible = this.app.model.entities.filter((e) => !this.abstractSpecOf(e));
    const collapsed = [...this.groupSpecs().values()]
      .filter((g) => g.mode === "abstract")
      .map((g) => this.collapsedNode(g));
    return visible.concat(collapsed);
  }

  collapsedNode(g) {
    const members = this.app.model.entities.filter((e) => e.group === g.id);
    if (!g.pos) g.pos = centroid(members);
    return {
      id: g.id, name: g.name, type: g.abstract?.type || "?", pos: g.pos,
      collapsed: true, spec: g, count: members.length,
    };
  }

  // Map a connection end to the visible [node id, port], or null if it is hidden.
  endpoint(ref, isOut) {
    const [ent, port] = splitRef(ref);
    const g = this.groupSpecs().get(ent);
    if (g) return this.groupEndpoint(g, port, isOut);
    const e = this.app.model.entities.find((x) => x.id === ent);
    const owner = e && this.abstractSpecOf(e);
    return owner ? this.boundaryEndpoint(owner, ref, isOut) : [ent, port];
  }

  // "group.port": the collapsed node's port, or the inner port it maps to.
  groupEndpoint(g, port, isOut) {
    if (g.mode === "abstract") return [g.id, port];
    const inner = (isOut ? g.outputs : g.inputs)?.[port];
    return inner ? splitRef(inner) : null;
  }

  // An inner port of a collapsed group: its boundary port, or null (an internal link).
  boundaryEndpoint(g, ref, isOut) {
    const mapping = (isOut ? g.outputs : g.inputs) || {};
    const name = Object.keys(mapping).find((k) => mapping[k] === ref);
    return name ? [g.id, name] : null;
  }

  linkEnds(conn) {
    const a = this.endpoint(conn.from, true), b = this.endpoint(conn.to, false);
    const na = a && this.entity(a[0]), nb = b && this.entity(b[0]);
    if (!na || !nb) return null;
    return [this.portPos(na, a[1], true), this.portPos(nb, b[1], false)];
  }

  entity(id) {
    return this.nodes().find((e) => e.id === id);
  }

  hitEntity(p) {
    const ents = this.nodes();
    for (let i = ents.length - 1; i >= 0; i--) {
      const e = ents[i];
      if (p.x >= e.pos[0] && p.x <= e.pos[0] + NODE_W && p.y >= e.pos[1] && p.y <= e.pos[1] + NODE_H) return e;
    }
    return null;
  }

  // group name -> {x, y, w, h, members} framing its entities (header strip on top)
  groups() {
    const out = new Map();
    const specs = this.groupSpecs();
    for (const e of this.app.model.entities) {
      if (!e.group || specs.get(e.group)?.mode === "abstract") continue;
      const g = out.get(e.group) || { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity, members: [] };
      g.x0 = Math.min(g.x0, e.pos[0]);
      g.y0 = Math.min(g.y0, e.pos[1]);
      g.x1 = Math.max(g.x1, e.pos[0] + NODE_W);
      g.y1 = Math.max(g.y1, e.pos[1] + NODE_H);
      g.members.push(e);
      out.set(e.group, g);
    }
    for (const g of out.values()) {
      g.x = g.x0 - GROUP_PAD;
      g.y = g.y0 - GROUP_PAD - GROUP_HEADER;
      g.w = g.x1 - g.x0 + 2 * GROUP_PAD;
      g.h = g.y1 - g.y0 + 2 * GROUP_PAD + GROUP_HEADER + 14; // room for badges
    }
    return out;
  }

  hitGroupHeader(p) {
    for (const [name, g] of this.groups()) {
      if (p.x >= g.x && p.x <= g.x + g.w && p.y >= g.y && p.y <= g.y + GROUP_HEADER) return { name, ...g };
    }
    return null;
  }

  hitPort(p) {
    const r = (PORT_R + 5) / Math.min(1, this.view.scale);
    for (const e of this.nodes()) {
      const ports = this.app.portsOf(e.id);
      for (const [list, isOut] of [[ports.outputs, true], [ports.inputs, false]]) {
        for (const port of list) {
          const q = this.portPos(e, port, isOut);
          if (Math.hypot(q.x - p.x, q.y - p.y) <= r) return { entity: e.id, port, isOut, ...q };
        }
      }
    }
    return null;
  }

  hitLink(p) {
    const tol = 6 / this.view.scale;
    for (const conn of this.app.model.connections) {
      const ends = this.linkEnds(conn);
      if (!ends) continue;
      for (let t = 0; t <= 1; t += 0.04) {
        const q = bezier(ends[0], ends[1], t);
        if (Math.hypot(q.x - p.x, q.y - p.y) < tol) return conn;
      }
    }
    return null;
  }

  // --- input ------------------------------------------------------------

  bindEvents() {
    const cv = this.canvas;
    cv.addEventListener("mousedown", (ev) => this.onDown(ev));
    window.addEventListener("mousemove", (ev) => this.onMove(ev));
    window.addEventListener("mouseup", (ev) => this.onUp(ev));
    cv.addEventListener("wheel", (ev) => this.onWheel(ev), { passive: false });
    cv.addEventListener("keydown", (ev) => this.onKey(ev));
    cv.addEventListener("dblclick", (ev) => {
      const p = this.toWorld(ev);
      const header = this.hitGroupHeader(p);
      const ent = header ? null : this.hitEntity(p);
      if (header) this.app.toggleGroup(header.name);
      else if (ent?.collapsed) this.app.toggleGroup(ent.id);
    });
    cv.addEventListener("dragover", (ev) => ev.preventDefault());
    cv.addEventListener("drop", (ev) => {
      ev.preventDefault();
      const p = this.toWorld(ev);
      const pos = [p.x - NODE_W / 2, p.y - NODE_H / 2];
      const kind = ev.dataTransfer.getData("text/x-sim-type");
      const template = ev.dataTransfer.getData("text/x-sim-template");
      if (kind) this.app.addEntity(kind, pos);
      else if (template) this.app.insertTemplate(template, pos);
    });
  }

  onDown(ev) {
    this.canvas.focus({ preventScroll: true });
    const p = this.toWorld(ev);
    const port = this.hitPort(p);
    if (port && port.isOut) {
      this.drag = { kind: "link", from: port, to: p };
      return;
    }
    const header = this.hitGroupHeader(p);
    if (header) {
      this.select({ kind: "group", id: header.name });
      this.drag = {
        kind: "move-group",
        members: header.members,
        start: p,
        origins: header.members.map((e) => [...e.pos]),
      };
      return;
    }
    const ent = this.hitEntity(p);
    if (ent) return this.downOnEntity(ent, p, ev);
    const conn = this.hitLink(p);
    if (conn) {
      this.select({ kind: "link", id: linkKey(conn) });
      return;
    }
    if (ev.shiftKey) {
      this.drag = { kind: "marquee", start: p, to: p, base: this.selectedIds() };
      return;
    }
    this.select(null);
    this.drag = { kind: "pan", sx: ev.clientX, sy: ev.clientY, vx: this.view.x, vy: this.view.y };
  }

  // Shift/Cmd/Ctrl-click toggles an entity in the selection; dragging one of several
  // selected entities moves them all.
  downOnEntity(ent, p, ev) {
    const ids = this.selectedIds();
    const toggling = ev.shiftKey || ev.metaKey || ev.ctrlKey;
    if (toggling && !ent.collapsed) {
      this.selectEntities(ids.includes(ent.id) ? ids.filter((x) => x !== ent.id) : [...ids, ent.id]);
      return;
    }
    if (ids.length > 1 && ids.includes(ent.id)) {
      const members = this.app.model.entities.filter((e) => ids.includes(e.id));
      this.drag = { kind: "move-group", members, start: p, origins: members.map((e) => [...e.pos]) };
      return;
    }
    this.select({ kind: ent.collapsed ? "group" : "entity", id: ent.id });
    this.drag = { kind: "move", entity: ent, dx: p.x - ent.pos[0], dy: p.y - ent.pos[1], moved: false };
  }

  onKey(ev) {
    const command = ev.metaKey || ev.ctrlKey ? `mod+${ev.key}` : ev.key;
    const action = CANVAS_KEYS[command];
    if (!action) return;
    ev.preventDefault();
    action.call(this);
  }

  // Ids of the selected (plain) entities: none, one, or several.
  selectedIds() {
    const sel = this.selection;
    if (sel?.kind === "multi") return sel.id;
    return sel?.kind === "entity" ? [sel.id] : [];
  }

  selectEntities(ids) {
    const unique = [...new Set(ids)];
    if (unique.length > 1) this.select({ kind: "multi", id: unique });
    else this.select(unique.length ? { kind: "entity", id: unique[0] } : null);
  }

  // Plain entities whose card overlaps the rectangle between points a and b.
  entitiesIn(a, b) {
    const x0 = Math.min(a.x, b.x), x1 = Math.max(a.x, b.x), y0 = Math.min(a.y, b.y), y1 = Math.max(a.y, b.y);
    return this.nodes().filter((e) => !e.collapsed && e.pos[0] < x1 && e.pos[0] + NODE_W > x0
      && e.pos[1] < y1 && e.pos[1] + NODE_H > y0).map((e) => e.id);
  }

  onMove(ev) {
    if (this.drag) {
      DRAGS[this.drag.kind].call(this, this.drag, this.toWorld(ev), ev);
    } else if (ev.target === this.canvas) {
      this.hoverPort = this.hitPort(this.toWorld(ev));
      this.canvas.style.cursor = this.hoverPort?.isOut ? "crosshair" : "default";
    }
  }

  onUp(ev) {
    const d = this.drag;
    this.drag = null;
    if (!d) return;
    if (d.kind === "move" && d.moved) this.dispatchEvent(new CustomEvent("moved"));
    if (d.kind === "marquee") this.selectEntities([...d.base, ...this.entitiesIn(d.start, d.to)]);
    if (d.kind === "link") {
      const target = this.hitPort(this.toWorld(ev));
      if (target && !target.isOut) this.app.connect(`${d.from.entity}.${d.from.port}`, `${target.entity}.${target.port}`);
    }
  }

  onWheel(ev) {
    ev.preventDefault();
    const r = this.canvas.getBoundingClientRect();
    const mx = ev.clientX - r.left, my = ev.clientY - r.top;
    const k = Math.exp(-ev.deltaY * 0.0015);
    const scale = Math.min(3, Math.max(0.2, this.view.scale * k));
    const f = scale / this.view.scale;
    this.view.x = mx - (mx - this.view.x) * f;
    this.view.y = my - (my - this.view.y) * f;
    this.view.scale = scale;
  }

  select(sel) {
    this.selection = sel;
    this.dispatchEvent(new CustomEvent("select", { detail: sel }));
  }

  // --- live data --------------------------------------------------------

  setClock(now, playing, speed) {
    this.clock = { now, wall: performance.now(), playing, speed };
  }

  displayNow() {
    const c = this.clock;
    if (!c.playing) return c.now;
    const ahead = (c.speed * (performance.now() - c.wall)) / 1000;
    return c.now + Math.min(ahead, c.speed * 0.25); // don't run far past the server
  }

  // stepping: the frame came from a step, so deliveries are animated in wall time
  addRecords(records, { stepping = false } = {}) {
    const wall = performance.now();
    for (const r of records) {
      if (r.type === "deliver") this.addDelivery(r, wall, stepping);
      else if (r.type === "send") this.flights.push({ link: r.link, t0: r.t, t1: r.arrive, wall });
      else if (flashesEntity(r)) this.flashes.push({ entity: r.entity, wall, rgb: BAD_RGB });
      else if (r.type === "lost") this.flashes.push({ link: r.link, wall, rgb: BAD_RGB });
    }
    if (this.flights.length > MAX_FLIGHTS) this.flights.splice(0, this.flights.length - MAX_FLIGHTS);
  }

  addDelivery(r, wall, stepping) {
    let at = wall;
    if (stepping && r.link) {
      this.flights.push({ link: r.link, wall, hop: true });
      at = wall + STEP_HOP_MS; // pulse when the dot arrives
    }
    const busy = this.flashes.some((f) => f.entity === r.entity && f.rgb === ACCENT_RGB && at - f.wall < FLASH_MS / 2);
    if (!busy) this.flashes.push({ entity: r.entity, wall: at, rgb: ACCENT_RGB });
  }

  clearLive() {
    this.flights = [];
    this.flashes = [];
  }

  // --- drawing ----------------------------------------------------------

  frame(t = performance.now()) {
    this.placePorts(Math.min(100, t - this.lastFrame));
    this.lastFrame = t;
    this.draw();
    requestAnimationFrame((t) => this.frame(t));
  }

  draw() {
    const { ctx, view, dpr } = this;
    const W = this.canvas.width / dpr, H = this.canvas.height / dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    this.drawGrid(W, H);
    ctx.setTransform(dpr * view.scale, 0, 0, dpr * view.scale, dpr * view.x, dpr * view.y);

    this.drawGroups();
    const conns = new Map(this.app.model.connections.map((c) => [linkKey(c), c]));
    for (const conn of conns.values()) this.drawLink(conn);
    this.drawFlights(conns);
    for (const e of this.app.ghosts || []) this.drawGhost(e);
    for (const e of this.nodes()) this.drawEntity(e);
    this.drawFlashes(conns);

    if (this.drag?.kind === "link") {
      ctx.strokeStyle = C.select;
      ctx.setLineDash([5, 4]);
      ctx.lineWidth = 1.5;
      this.strokeBezier(this.drag.from, this.drag.to);
      ctx.setLineDash([]);
    }
    if (this.drag?.kind === "marquee") this.drawMarquee(this.drag.start, this.drag.to);
  }

  drawMarquee(a, b) {
    const { ctx } = this;
    ctx.fillStyle = "rgba(76,194,255,0.08)";
    ctx.strokeStyle = C.select;
    ctx.lineWidth = 1 / this.view.scale;
    ctx.setLineDash([4 / this.view.scale, 3 / this.view.scale]);
    ctx.fillRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
    ctx.strokeRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
    ctx.setLineDash([]);
  }

  drawGroups() {
    const { ctx } = this;
    for (const [name, g] of this.groups()) {
      const selected = this.selection?.kind === "group" && this.selection.id === name;
      ctx.fillStyle = "rgba(245,177,61,0.035)";
      ctx.strokeStyle = selected ? C.select : "#3a3222";
      ctx.lineWidth = selected ? 1.8 : 1;
      ctx.setLineDash(selected ? [] : [5, 4]);
      roundRect(ctx, g.x, g.y, g.w, g.h, 12);
      ctx.fill();
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = selected ? C.select : "#a3894f";
      ctx.font = '900 10px "Font Awesome 7 Free"';
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillText(glyph("fa-layer-group"), g.x + 10, g.y + GROUP_HEADER / 2 + 1);
      ctx.font = "600 11px -apple-system, Segoe UI, sans-serif";
      ctx.fillText(name, g.x + 26, g.y + GROUP_HEADER / 2 + 1);
    }
  }

  drawGrid(W, H) {
    const { ctx, view } = this;
    const step = 24 * view.scale;
    if (step < 8) return;
    ctx.fillStyle = C.grid;
    const ox = ((view.x % step) + step) % step, oy = ((view.y % step) + step) % step;
    for (let x = ox; x < W; x += step) for (let y = oy; y < H; y += step) ctx.fillRect(x, y, 1.5, 1.5);
  }

  strokeBezier(a, b) {
    const ctx = this.ctx;
    const [p1, p2] = controls(a, b);
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.bezierCurveTo(p1.x, p1.y, p2.x, p2.y, b.x, b.y);
    ctx.stroke();
  }

  drawLink(conn) {
    const ends = this.linkEnds(conn);
    if (!ends) return;
    const { ctx } = this;
    const key = linkKey(conn);
    const selected = this.selection?.kind === "link" && this.selection.id === key;
    const down = conn.up === false;
    ctx.strokeStyle = selected ? C.select : down ? C.linkDown : C.link;
    ctx.lineWidth = selected ? 2.5 : 1.6;
    if (down) ctx.setLineDash([6, 5]);
    this.strokeBezier(ends[0], ends[1]);
    ctx.setLineDash([]);

    const m = this.app.summary?.links?.[key];
    const inFlight = m?.in_flight?.value || 0;
    const lost = m?.lost?.value || 0;
    if (inFlight > 0 || lost > 0) {
      const mid = bezier(ends[0], ends[1], 0.5);
      const label = lost ? `${inFlight} · ${lost} lost` : `${inFlight}`;
      ctx.font = "10px ui-monospace, Menlo, monospace";
      const w = ctx.measureText(label).width + 10;
      ctx.fillStyle = "#11161dcc";
      roundRect(ctx, mid.x - w / 2, mid.y - 16, w, 14, 7);
      ctx.fill();
      ctx.fillStyle = lost ? C.bad : C.muted;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(label, mid.x, mid.y - 9);
    }
  }

  drawFlights(conns) {
    const { ctx } = this;
    const now = this.displayNow();
    const wall = performance.now();
    const keep = [];
    ctx.fillStyle = C.accent;
    for (const f of this.flights) {
      const progress = flightProgress(f, now, wall);
      if (progress > 1) continue;
      keep.push(f);
      if (progress < 0) continue;
      const conn = conns.get(f.link);
      const ends = conn && this.linkEnds(conn);
      if (!ends) continue;
      const q = bezier(ends[0], ends[1], progress);
      ctx.beginPath();
      ctx.arc(q.x, q.y, DOT_R, 0, Math.PI * 2);
      ctx.fill();
    }
    this.flights = keep;
  }

  drawFlashes(conns) {
    const { ctx } = this;
    const wall = performance.now();
    this.flashes = this.flashes.filter((f) => wall - f.wall < FLASH_MS);
    for (const f of this.flashes) {
      if (wall < f.wall) continue; // scheduled: a stepped hop is still on its way
      const a = 1 - (wall - f.wall) / FLASH_MS;
      ctx.strokeStyle = `rgba(${f.rgb},${a})`;
      ctx.lineWidth = 2;
      if (f.entity) {
        const e = this.entity(f.entity);
        if (!e) continue;
        roundRect(ctx, e.pos[0] - 4 - a * 4, e.pos[1] - 4 - a * 4, NODE_W + 8 + a * 8, NODE_H + 8 + a * 8, 12);
        ctx.stroke();
      } else if (f.link) {
        const conn = conns.get(f.link);
        const ends = conn && this.linkEnds(conn);
        if (!ends) continue;
        const m = bezier(ends[0], ends[1], 0.15);
        ctx.beginPath();
        ctx.arc(m.x, m.y, 6 + (1 - a) * 6, 0, Math.PI * 2);
        ctx.stroke();
      }
    }
  }

  isSelected(e) {
    if (this.selection?.kind === "multi") return !e.collapsed && this.selection.id.includes(e.id);
    const kind = e.collapsed ? "group" : "entity";
    return this.selection?.kind === kind && this.selection.id === e.id;
  }

  drawEntity(e) {
    const selected = this.isSelected(e);
    if (e.collapsed) this.drawStackedBack(e, selected);
    this.drawCard(e, selected);
    const diff = this.app.diffOf?.(e.id);
    if (diff) this.drawDiffMark(e, diff);
    this.drawIcon(e);
    this.drawLabels(e);
    const stats = this.app.summary?.entities?.[e.id];
    if (stats) this.drawBadges(e, stats);
    this.drawPorts(e);
  }

  // A second card behind a collapsed group: this node stands in for many.
  // Proposal preview: what the agent adds or changes, and what it would remove.
  drawDiffMark(e, diff) {
    const { ctx } = this;
    const color = diff === "added" ? C.ok : C.accent;
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 4]);
    roundRect(ctx, e.pos[0] - 4, e.pos[1] - 4, NODE_W + 8, NODE_H + 8, 12);
    ctx.stroke();
    ctx.setLineDash([]);
    const label = diff === "added" ? "NEW" : "CHANGED";
    ctx.font = "700 9px -apple-system, Segoe UI, sans-serif";
    const w = ctx.measureText(label).width + 10;
    ctx.fillStyle = color;
    roundRect(ctx, e.pos[0] + NODE_W - w, e.pos[1] - 12, w, 14, 7);
    ctx.fill();
    ctx.fillStyle = "#10141a";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(label, e.pos[0] + NODE_W - w / 2, e.pos[1] - 5);
  }

  drawGhost(e) {
    const { ctx } = this;
    ctx.save();
    ctx.globalAlpha = 0.35;
    ctx.strokeStyle = C.bad;
    ctx.setLineDash([4, 4]);
    roundRect(ctx, e.pos[0], e.pos[1], NODE_W, NODE_H, 9);
    ctx.stroke();
    ctx.fillStyle = C.bad;
    ctx.font = "600 12px -apple-system, Segoe UI, sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(`${e.id} (removed)`, e.pos[0] + 12, e.pos[1] + NODE_H / 2);
    ctx.restore();
  }

  drawStackedBack(e, selected) {
    const { ctx } = this;
    ctx.fillStyle = "#141a22";
    ctx.strokeStyle = selected ? C.select : "#4a3d22";
    ctx.lineWidth = 1;
    roundRect(ctx, e.pos[0] + 5, e.pos[1] - 5, NODE_W, NODE_H, 9);
    ctx.fill();
    ctx.stroke();
  }

  drawCard(e, selected) {
    const { ctx } = this;
    const idle = e.collapsed ? "#7a6330" : C.nodeLine;
    ctx.fillStyle = C.node;
    ctx.strokeStyle = selected ? C.select : idle;
    ctx.lineWidth = selected ? 2 : 1;
    roundRect(ctx, e.pos[0], e.pos[1], NODE_W, NODE_H, 9);
    ctx.fill();
    ctx.stroke();
  }

  drawIcon(e) {
    const { ctx } = this;
    const [x, y] = e.pos;
    const type = this.app.types[e.type];
    ctx.fillStyle = "#231d10";
    ctx.beginPath();
    ctx.arc(x + 24, y + NODE_H / 2, 15, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = type ? C.accent : C.bad;
    ctx.font = ICON_FONT;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(glyph(type?.icon || "fa-circle-question"), x + 24, y + NODE_H / 2 + 1);
  }

  drawLabels(e) {
    const { ctx } = this;
    const [x, y] = e.pos;
    const subtitle = e.collapsed ? `${e.count} items · abstract` : e.type;
    ctx.textAlign = "left";
    ctx.fillStyle = C.text;
    ctx.font = "600 12.5px -apple-system, Segoe UI, sans-serif";
    ctx.fillText(clip(ctx, e.name || e.id, NODE_W - 52), x + 46, y + 22);
    ctx.fillStyle = C.muted;
    ctx.font = "11px -apple-system, Segoe UI, sans-serif";
    ctx.fillText(clip(ctx, subtitle, NODE_W - 52), x + 46, y + 39);
  }

  drawBadges(e, s) {
    const { ctx } = this;
    const [x, y] = e.pos;
    const text = badgeText(e, s);
    if (text) {
      ctx.font = "10.5px ui-monospace, Menlo, monospace";
      ctx.fillStyle = C.muted;
      ctx.textAlign = "left";
      ctx.fillText(text, x + 2, y + NODE_H + 13);
    }
    if (s.type === "server") this.drawUtilization(x, y, Math.min(1, s.utilization || 0));
  }

  drawUtilization(x, y, u) {
    const { ctx } = this;
    ctx.fillStyle = "#222a35";
    ctx.fillRect(x + 8, y + NODE_H - 6, NODE_W - 16, 3);
    ctx.fillStyle = utilizationColor(u);
    ctx.fillRect(x + 8, y + NODE_H - 6, (NODE_W - 16) * u, 3);
  }

  drawPorts(e) {
    const ports = this.app.portsOf(e.id);
    for (const [list, isOut] of [[ports.inputs, false], [ports.outputs, true]]) {
      // a lone in/out needs no label
      const labelled = list.length > 1 || list.some((p) => p !== "in" && p !== "out");
      for (const port of list) this.drawPort(e, port, isOut, labelled);
    }
  }

  drawPort(e, port, isOut, labelled) {
    const { ctx } = this;
    const q = this.portPos(e, port, isOut);
    const h = this.hoverPort;
    const hot = h && h.entity === e.id && h.port === port && h.isOut === isOut;
    // outputs are filled, inputs hollow: with dynamic ports the side doesn't tell them apart
    ctx.fillStyle = hot ? C.portHot : isOut ? C.port : C.node;
    ctx.strokeStyle = hot ? C.portHot : C.port;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(q.x, q.y, PORT_R, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    if (!labelled) return;
    ctx.fillStyle = C.muted;
    ctx.font = "9.5px ui-monospace, Menlo, monospace";
    ctx.textAlign = ALIGN[Math.sign(q.nx) + 1];
    ctx.textBaseline = BASELINE[Math.sign(q.ny) + 1];
    ctx.fillText(port, q.x + q.nx * 8, q.y + q.ny * 8);
  }
}

// --- helpers --------------------------------------------------------------

// A port label's anchor, by the sign of the port's outward normal (-1, 0, 1).
const ALIGN = ["right", "center", "left"];
const BASELINE = ["bottom", "middle", "top"];

export function splitRef(ref) {
  const i = ref.lastIndexOf(".");
  return [ref.slice(0, i), ref.slice(i + 1)];
}

// A link leaves and enters along its ports' outward normals. An end without one (the
// pointer, while dragging a new link) gets no handle.
function controls(a, b) {
  const d = Math.max(36, Math.hypot(b.x - a.x, b.y - a.y) * 0.4);
  const handle = (p) => (p.nx === undefined ? { x: p.x, y: p.y } : { x: p.x + p.nx * d, y: p.y + p.ny * d });
  return [handle(a), handle(b)];
}

function bezier(a, b, t) {
  const [p1, p2] = controls(a, b);
  const u = 1 - t;
  return {
    x: u * u * u * a.x + 3 * u * u * t * p1.x + 3 * u * t * t * p2.x + t * t * t * b.x,
    y: u * u * u * a.y + 3 * u * u * t * p1.y + 3 * u * t * t * p2.y + t * t * t * b.y,
  };
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, r);
}

// Keys on the focused canvas (called with `this` = the Editor).
const CANVAS_KEYS = {
  Delete() {
    if (this.selection) this.app.deleteSelection();
  },
  Backspace() {
    if (this.selection) this.app.deleteSelection();
  },
  Escape() {
    this.select(null);
  },
  "mod+a"() {
    this.selectEntities(this.nodes().filter((e) => !e.collapsed).map((e) => e.id));
  },
};

// What a mouse move does for each kind of drag (called with `this` = the Editor).
const DRAGS = {
  pan(d, p, ev) {
    this.view.x = d.vx + ev.clientX - d.sx;
    this.view.y = d.vy + ev.clientY - d.sy;
  },
  move(d, p) {
    d.entity.pos = [snap(p.x - d.dx), snap(p.y - d.dy)];
    if (d.entity.collapsed) d.entity.spec.pos = d.entity.pos;
    d.moved = true;
  },
  "move-group"(d, p) {
    const dx = snap(p.x - d.start.x), dy = snap(p.y - d.start.y);
    d.members.forEach((e, i) => {
      e.pos = [d.origins[i][0] + dx, d.origins[i][1] + dy];
    });
  },
  marquee(d, p) {
    d.to = p;
  },
  link(d, p) {
    d.to = p;
    this.hoverPort = this.hitPort(p);
  },
};

// Drops and unrouted messages flash the node red; a machine absorbing a message doesn't.
const BAD_RGB = "255,107,107";
const ACCENT_RGB = "245,177,61";

// How far along its link a message dot is (0..1; > 1 means it has arrived).
function flightProgress(f, now, wall) {
  if (f.hop) return (wall - f.wall) / STEP_HOP_MS;
  const span = f.t1 - f.t0;
  const sinceSent = (wall - f.wall) / MIN_FLIGHT_MS;
  if (span <= 0) return sinceSent;
  const progress = (now - f.t0) / span;
  return progress >= 1 && sinceSent < 1 ? sinceSent : progress; // keep fast hops visible
}

function flashesEntity(r) {
  if (r.type === "unrouted") return true;
  return r.type === "drop" && r.reason !== "absorbed";
}

// One-line live summary under a node, by behavior kind.
const BADGES = {
  source: (s) => `${fmt(s.emitted?.value)} sent`,
  server: (s) => `q ${fmt(s.queue?.value)} · ${Math.round((s.utilization || 0) * 100)}% busy`,
  sink: (s) => `${fmt(s.received?.value)} · ${fmt(s.latency?.mean)} lat`,
  delay: (s, e) => (e.collapsed ? `${fmt(s.passed?.value)} passed` : ""),
  machine: (s) => `● ${s.state}${firstVar(s.vars)}${s.last_error ? "  ⚠ error" : ""}`,
};

// A machine's first variable (its main quantity, by convention), e.g. " · foxes 42".
function firstVar(vars) {
  const [name, value] = Object.entries(vars || {})[0] || [];
  return typeof value === "number" ? ` · ${name} ${fmt(value)}` : "";
}

function badgeText(e, s) {
  let text = BADGES[s.type]?.(s, e) || "";
  const failed = (s.dropped?.value || 0) + (s.errors?.value || 0);
  if (failed) text += `  ✕${fmt(failed)}`;
  if (s.unrouted?.value) text += `  ⚠ ${fmt(s.unrouted.value)} unrouted`;
  return text;
}

function utilizationColor(u) {
  if (u > 0.9) return C.bad;
  return u > 0.7 ? C.accent : C.ok;
}

function centroid(entities) {
  if (!entities.length) return [0, 0];
  const x = entities.reduce((a, e) => a + e.pos[0], 0) / entities.length;
  const y = entities.reduce((a, e) => a + e.pos[1], 0) / entities.length;
  return [snap(x), snap(y)];
}

function snap(v) {
  return Math.round(v / 8) * 8;
}

function clip(ctx, text, max) {
  if (ctx.measureText(text).width <= max) return text;
  while (text.length > 1 && ctx.measureText(text + "…").width > max) text = text.slice(0, -1);
  return text + "…";
}

export function fmt(v) {
  if (v === undefined || v === null || Number.isNaN(v)) return "–";
  if (typeof v !== "number") return String(v); // e.g. item_type in a summary
  if (Number.isInteger(v)) return v.toLocaleString("en-US");
  const a = Math.abs(v);
  if (a >= 100) return v.toFixed(0);
  if (a >= 1) return v.toFixed(2);
  if (a >= 0.001) return v.toFixed(4);
  return v.toExponential(2);
}
