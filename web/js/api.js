// REST helpers and the live-run WebSocket client.

async function request(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${method} ${url}: ${res.status}`);
  return data;
}

export const api = {
  types: () => request("GET", "/api/types"),
  models: () => request("GET", "/api/models"),
  getModel: (path) => request("GET", `/api/models/${path}`),
  saveModel: (path, model) => request("PUT", `/api/models/${path}`, model),
  validate: (model) => request("POST", "/api/validate", model),
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
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${proto}://${location.host}/ws/run`);
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
}
