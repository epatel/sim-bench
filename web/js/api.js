// REST helpers and the live-run WebSocket client.

import { authFetch, openSocket } from "./auth.js";

async function request(method, url, body) {
  const res = await authFetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${method} ${url}: ${res.status}`);
  return data;
}

export const api = {
  types: () => request("GET", "api/types"),
  libraries: () => request("GET", "api/libraries"),
  templates: () => request("GET", "api/templates"),
  instantiate: (id, args, prefix) => request("POST", `api/templates/${id}/instantiate`, { args, prefix }),
  models: () => request("GET", "api/models"),
  getModel: (path) => request("GET", `api/models/${path}`),
  saveModel: (path, model) => request("PUT", `api/models/${path}`, model),
  validate: (model) => request("POST", "api/validate", model),
  // opts: { until, seed, kind }
  calibrate: (model, group, opts) => request("POST", "api/calibrate", { model, group, ...opts }),
  startBatch: (body) => request("POST", "api/batches", body),
  batches: () => request("GET", "api/batches"),
  batch: (id, metric) => request("GET", `api/batches/${id}${metric ? `?metric=${encodeURIComponent(metric)}` : ""}`),
};

// One live simulation on the server. Emits "loaded", "frame", "error", "closed".
export class RunClient extends EventTarget {
  constructor() {
    super();
    this.ws = null;
    this.queue = [];
    this.connect();
  }

  connect() {
    this.ws = openSocket("ws/run");
    this.ws.onopen = () => {
      for (const m of this.queue.splice(0)) this.ws.send(JSON.stringify(m));
    };
    this.ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      this.dispatchEvent(new CustomEvent(msg.type, { detail: msg }));
    };
    this.ws.onclose = () => {
      this.dispatchEvent(new CustomEvent("closed"));
      setTimeout(() => this.connect(), 1500);
    };
  }

  send(msg) {
    if (this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
    else this.queue.push(msg);
  }

  load(model, seed) { this.send({ cmd: "load", model, seed }); }
  play() { this.send({ cmd: "play" }); }
  pause() { this.send({ cmd: "pause" }); }
  step() { this.send({ cmd: "step" }); }
  reset() { this.send({ cmd: "reset" }); }
  speed(value) { this.send({ cmd: "speed", value }); }
  whatif(scenario, horizon) { this.send({ cmd: "whatif", scenario, horizon }); }
}
