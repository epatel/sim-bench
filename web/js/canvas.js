// Canvas editor and live view: entities, ports, links, messages in flight.
import { glyph, ICON_FONT } from "./icons.js";

export const NODE_W = 136;
export const NODE_H = 60;
const PORT_R = 5;
const GROUP_PAD = 18;
const GROUP_HEADER = 22;
const MAX_FLIGHTS = 3000;
const MIN_FLIGHT_MS = 180; // zero-latency hops stay visible this long

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
    this.selection = null; // {kind: "entity"|"link", id}
    this.drag = null;
    this.hoverPort = null;
    this.flights = [];
    this.flashes = [];
    this.clock = { now: 0, wall: performance.now(), playing: false, speed: 1 };
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
    const ents = this.app.model.entities;
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

  portPos(entity, port, isOut) {
    const ports = this.app.portsOf(entity.id);
    const list = isOut ? ports.outputs : ports.inputs;
    const i = Math.max(0, list.indexOf(port));
    return {
      x: entity.pos[0] + (isOut ? NODE_W : 0),
      y: entity.pos[1] + (NODE_H * (i + 1)) / (list.length + 1),
    };
  }

  linkEnds(conn) {
    const [se, sp] = splitRef(conn.from), [de, dp] = splitRef(conn.to);
    const a = this.entity(se), b = this.entity(de);
    if (!a || !b) return null;
    return [this.portPos(a, sp, true), this.portPos(b, dp, false)];
  }

  entity(id) {
    return this.app.model.entities.find((e) => e.id === id);
  }

  hitEntity(p) {
    const ents = this.app.model.entities;
    for (let i = ents.length - 1; i >= 0; i--) {
      const e = ents[i];
      if (p.x >= e.pos[0] && p.x <= e.pos[0] + NODE_W && p.y >= e.pos[1] && p.y <= e.pos[1] + NODE_H) return e;
    }
    return null;
  }

  // group name -> {x, y, w, h, members} framing its entities (header strip on top)
  groups() {
    const out = new Map();
    for (const e of this.app.model.entities) {
      if (!e.group) continue;
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
    for (const e of this.app.model.entities) {
      const ports = this.app.portsOf(e.id);
      for (const [list, isOut] of [[ports.outputs, true], [ports.inputs, false]]) {
        for (const port of list) {
          const q = this.portPos(e, port, isOut);
          if (Math.hypot(q.x - p.x, q.y - p.y) <= r) return { entity: e.id, port, isOut, x: q.x, y: q.y };
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
    cv.addEventListener("keydown", (ev) => {
      if ((ev.key === "Delete" || ev.key === "Backspace") && this.selection) {
        ev.preventDefault();
        this.app.deleteSelection();
      }
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
    if (ent) {
      this.select({ kind: "entity", id: ent.id });
      this.drag = { kind: "move", entity: ent, dx: p.x - ent.pos[0], dy: p.y - ent.pos[1], moved: false };
      return;
    }
    const conn = this.hitLink(p);
    if (conn) {
      this.select({ kind: "link", id: linkKey(conn) });
      return;
    }
    this.select(null);
    this.drag = { kind: "pan", sx: ev.clientX, sy: ev.clientY, vx: this.view.x, vy: this.view.y };
  }

  onMove(ev) {
    if (!this.drag) {
      if (ev.target === this.canvas) {
        this.hoverPort = this.hitPort(this.toWorld(ev));
        this.canvas.style.cursor = this.hoverPort?.isOut ? "crosshair" : "default";
      }
      return;
    }
    const p = this.toWorld(ev);
    if (this.drag.kind === "pan") {
      this.view.x = this.drag.vx + ev.clientX - this.drag.sx;
      this.view.y = this.drag.vy + ev.clientY - this.drag.sy;
    } else if (this.drag.kind === "move") {
      this.drag.entity.pos = [snap(p.x - this.drag.dx), snap(p.y - this.drag.dy)];
      this.drag.moved = true;
    } else if (this.drag.kind === "move-group") {
      const dx = snap(p.x - this.drag.start.x), dy = snap(p.y - this.drag.start.y);
      this.drag.members.forEach((e, i) => {
        e.pos = [this.drag.origins[i][0] + dx, this.drag.origins[i][1] + dy];
      });
    } else if (this.drag.kind === "link") {
      this.drag.to = p;
      this.hoverPort = this.hitPort(p);
    }
  }

  onUp(ev) {
    const d = this.drag;
    this.drag = null;
    if (!d) return;
    if (d.kind === "move" && d.moved) this.dispatchEvent(new CustomEvent("moved"));
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

  addRecords(records) {
    const wall = performance.now();
    for (const r of records) {
      if (r.type === "send") {
        this.flights.push({ link: r.link, t0: r.t, t1: r.arrive, wall });
      } else if (r.type === "drop" || r.type === "unrouted") {
        this.flashes.push({ entity: r.entity, wall, color: C.bad });
      } else if (r.type === "lost") {
        this.flashes.push({ link: r.link, wall, color: C.bad });
      }
    }
    if (this.flights.length > MAX_FLIGHTS) this.flights.splice(0, this.flights.length - MAX_FLIGHTS);
  }

  clearLive() {
    this.flights = [];
    this.flashes = [];
  }

  // --- drawing ----------------------------------------------------------

  frame() {
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
    for (const e of this.app.model.entities) this.drawEntity(e);
    this.drawFlashes(conns);

    if (this.drag?.kind === "link") {
      ctx.strokeStyle = C.select;
      ctx.setLineDash([5, 4]);
      ctx.lineWidth = 1.5;
      this.strokeBezier(this.drag.from, this.drag.to);
      ctx.setLineDash([]);
    }
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
    const dx = Math.max(40, Math.abs(b.x - a.x) * 0.5);
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.bezierCurveTo(a.x + dx, a.y, b.x - dx, b.y, b.x, b.y);
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
      const span = f.t1 - f.t0;
      const visualSpan = span > 0 ? span : 0;
      let progress = visualSpan > 0 ? (now - f.t0) / visualSpan : (wall - f.wall) / MIN_FLIGHT_MS;
      if (visualSpan > 0 && progress >= 1 && wall - f.wall < MIN_FLIGHT_MS) progress = (wall - f.wall) / MIN_FLIGHT_MS;
      if (progress > 1) continue;
      keep.push(f);
      if (progress < 0) continue;
      const conn = conns.get(f.link);
      const ends = conn && this.linkEnds(conn);
      if (!ends) continue;
      const q = bezier(ends[0], ends[1], progress);
      ctx.beginPath();
      ctx.arc(q.x, q.y, 3.2, 0, Math.PI * 2);
      ctx.fill();
    }
    this.flights = keep;
  }

  drawFlashes(conns) {
    const { ctx } = this;
    const wall = performance.now();
    this.flashes = this.flashes.filter((f) => wall - f.wall < 450);
    for (const f of this.flashes) {
      const a = 1 - (wall - f.wall) / 450;
      ctx.strokeStyle = `rgba(255,107,107,${a})`;
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

  drawEntity(e) {
    const { ctx } = this;
    const [x, y] = e.pos;
    const type = this.app.types[e.type];
    const selected = this.selection?.kind === "entity" && this.selection.id === e.id;
    const stats = this.app.summary?.entities?.[e.id];

    ctx.fillStyle = C.node;
    ctx.strokeStyle = selected ? C.select : C.nodeLine;
    ctx.lineWidth = selected ? 2 : 1;
    roundRect(ctx, x, y, NODE_W, NODE_H, 9);
    ctx.fill();
    ctx.stroke();

    // icon
    ctx.fillStyle = "#231d10";
    ctx.beginPath();
    ctx.arc(x + 24, y + NODE_H / 2, 15, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = type ? C.accent : C.bad;
    ctx.font = ICON_FONT;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(glyph(type?.icon || "fa-circle-question"), x + 24, y + NODE_H / 2 + 1);

    // labels
    ctx.textAlign = "left";
    ctx.fillStyle = C.text;
    ctx.font = "600 12.5px -apple-system, Segoe UI, sans-serif";
    ctx.fillText(clip(ctx, e.name || e.id, NODE_W - 52), x + 46, y + 22);
    ctx.fillStyle = C.muted;
    ctx.font = "11px -apple-system, Segoe UI, sans-serif";
    ctx.fillText(e.type, x + 46, y + 39);

    if (stats) this.drawBadges(e, stats);
    this.drawPorts(e);
  }

  drawBadges(e, s) {
    const { ctx } = this;
    const [x, y] = e.pos;
    let text = "";
    if (s.type === "source") text = `${fmt(s.emitted?.value)} sent`;
    else if (s.type === "server") text = `q ${fmt(s.queue?.value)} · ${Math.round((s.utilization || 0) * 100)}% busy`;
    else if (s.type === "sink") text = `${fmt(s.received?.value)} · ${fmt(s.latency?.mean)} lat`;
    if (s.dropped?.value || s.errors?.value) text += `  ✕${fmt((s.dropped?.value || 0) + (s.errors?.value || 0))}`;
    if (s.unrouted?.value) text += `  ⚠ ${fmt(s.unrouted.value)} unrouted`;
    if (text) {
      ctx.font = "10.5px ui-monospace, Menlo, monospace";
      ctx.fillStyle = C.muted;
      ctx.textAlign = "left";
      ctx.fillText(text, x + 2, y + NODE_H + 13);
    }
    if (s.type === "server") {
      const u = Math.min(1, s.utilization || 0);
      ctx.fillStyle = "#222a35";
      ctx.fillRect(x + 8, y + NODE_H - 6, NODE_W - 16, 3);
      ctx.fillStyle = u > 0.9 ? C.bad : u > 0.7 ? C.accent : C.ok;
      ctx.fillRect(x + 8, y + NODE_H - 6, (NODE_W - 16) * u, 3);
    }
  }

  drawPorts(e) {
    const { ctx } = this;
    const ports = this.app.portsOf(e.id);
    for (const [list, isOut] of [[ports.inputs, false], [ports.outputs, true]]) {
      for (const port of list) {
        const q = this.portPos(e, port, isOut);
        const hot = this.hoverPort && this.hoverPort.entity === e.id && this.hoverPort.port === port && this.hoverPort.isOut === isOut;
        ctx.fillStyle = hot ? C.portHot : C.node;
        ctx.strokeStyle = hot ? C.portHot : C.port;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(q.x, q.y, PORT_R, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
        if (list.length > 1 || (port !== "in" && port !== "out")) {
          ctx.fillStyle = C.muted;
          ctx.font = "9.5px ui-monospace, Menlo, monospace";
          ctx.textAlign = isOut ? "left" : "right";
          ctx.textBaseline = "middle";
          ctx.fillText(port, q.x + (isOut ? 8 : -8), q.y);
        }
      }
    }
  }
}

// --- helpers --------------------------------------------------------------

export function splitRef(ref) {
  const i = ref.lastIndexOf(".");
  return [ref.slice(0, i), ref.slice(i + 1)];
}

function bezier(a, b, t) {
  const dx = Math.max(40, Math.abs(b.x - a.x) * 0.5);
  const p1 = { x: a.x + dx, y: a.y }, p2 = { x: b.x - dx, y: b.y };
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
  if (Number.isInteger(v)) return v.toLocaleString("en-US");
  const a = Math.abs(v);
  if (a >= 100) return v.toFixed(0);
  if (a >= 1) return v.toFixed(2);
  if (a >= 0.001) return v.toFixed(4);
  return v.toExponential(2);
}
