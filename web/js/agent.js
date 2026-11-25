// The agent chat: talks to /ws/agent; the agent edits the model through tools and
// the server pushes each new model back here, so the canvas updates as it works.

const SUGGESTIONS = [
  "Explain this model and what limits its throughput.",
  "Put a cache in front of the database and compare latency over 5 seeds.",
  "Add a scenario where the database gets twice as slow at t=200, then compare.",
];

function h(tag, attrs = {}, text) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (text !== undefined) n.textContent = text;
  return n;
}

function icon(name) {
  return h("i", { class: `fa-solid ${name}` });
}

// Minimal, safe markdown: escape first, then code, bold and bullet lists.
function renderMarkdown(text) {
  const esc = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const blocks = esc.split(/```[a-z]*\n?/);
  return blocks
    .map((b, i) => (i % 2 ? `<pre>${b}</pre>` : inline(b)))
    .join("");
}

function inline(text) {
  const lines = text.split("\n").map((line) => {
    const item = line.match(/^\s*[-*] (.*)/);
    const body = (item ? item[1] : line)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/^#{1,4} (.*)/, "<strong>$1</strong>");
    return item ? `<li>${body}</li>` : `${body}<br>`;
  });
  return lines.join("").replace(/(<li>.*?<\/li>)+/g, (m) => `<ul>${m}</ul>`);
}

export class AgentChat {
  // hooks: { getModel(), applyModel(model), onBatch(id) }
  constructor(root, hooks) {
    this.root = root;
    this.hooks = hooks;
    this.busy = false;
    this.tools = new Map(); // tool_use id -> row element
    this.build();
    this.connect();
    this.loadStatus();
  }

  build() {
    this.root.innerHTML = "";
    const head = h("div", { class: "ag-head" });
    this.statusLine = h("span", { class: "ag-status" }, "connecting…");
    const reset = h("button", { title: "Start a new conversation" });
    reset.append(icon("fa-rotate-right"));
    reset.onclick = () => this.reset();
    head.append(this.statusLine, reset);
    this.log = h("div", { class: "ag-log" });
    this.composer = h("form", { class: "ag-compose" });
    this.input = h("textarea", { rows: "3", placeholder: "Ask the agent to build, change, run or explain the model…" });
    this.input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        this.submit();
      }
    });
    this.sendBtn = h("button", { type: "submit", class: "primary", title: "Send (Enter)" });
    this.sendBtn.append(icon("fa-paper-plane"));
    this.composer.onsubmit = (ev) => {
      ev.preventDefault();
      this.busy ? this.stop() : this.submit();
    };
    this.composer.append(this.input, this.sendBtn);
    this.root.append(head, this.log, this.composer);
    this.showSuggestions();
  }

  async loadStatus() {
    try {
      const s = await (await fetch("/api/agent/status")).json();
      this.statusLine.textContent = `model: ${s.model}`;
      this.statusLine.title = `Claude Agent SDK ${s.sdk} · ${s.tools.length} workbench tools`;
    } catch {
      this.statusLine.textContent = "agent unavailable";
    }
  }

  connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${proto}://${location.host}/ws/agent`);
    this.ws.onmessage = (ev) => this.onEvent(JSON.parse(ev.data));
    this.ws.onclose = () => {
      this.setBusy(false);
      setTimeout(() => this.connect(), 2000);
    };
  }

  showSuggestions() {
    const box = h("div", { class: "ag-suggest" });
    box.append(h("p", { class: "sub" }, "The agent edits this model with workbench tools; you see each change on the canvas."));
    for (const text of SUGGESTIONS) {
      const chip = h("button", { type: "button" }, text);
      chip.onclick = () => {
        this.input.value = text;
        this.submit();
      };
      box.append(chip);
    }
    this.log.append(box);
  }

  submit() {
    const text = this.input.value.trim();
    if (!text || !this.ready()) return;
    this.log.querySelector(".ag-suggest")?.remove();
    this.add(h("div", { class: "ag-msg ag-user" }, text));
    this.input.value = "";
    this.ws.send(JSON.stringify({ cmd: "prompt", text, model: this.hooks.getModel() }));
    this.setBusy(true);
    this.thinking = this.add(h("div", { class: "ag-thinking" }, "Working…"));
  }

  ready() {
    return !this.busy && this.ws.readyState === WebSocket.OPEN;
  }

  stop() {
    this.ws.send(JSON.stringify({ cmd: "interrupt" }));
  }

  reset() {
    this.ws.send(JSON.stringify({ cmd: "reset" }));
    this.log.innerHTML = "";
    this.tools.clear();
    this.setBusy(false);
    this.showSuggestions();
  }

  setBusy(busy) {
    this.busy = busy;
    this.sendBtn.innerHTML = "";
    this.sendBtn.append(icon(busy ? "fa-stop" : "fa-paper-plane"));
    this.sendBtn.title = busy ? "Stop" : "Send (Enter)";
    this.input.disabled = busy;
  }

  add(node) {
    const stick = this.log.scrollTop + this.log.clientHeight >= this.log.scrollHeight - 30;
    this.log.append(node);
    if (stick) this.log.scrollTop = this.log.scrollHeight;
    return node;
  }

  onEvent(e) {
    const handler = {
      text: () => {
        const div = h("div", { class: "ag-msg ag-bot" });
        div.innerHTML = renderMarkdown(e.text);
        this.add(div);
      },
      tool: () => this.toolCall(e),
      tool_result: () => this.toolResult(e),
      model: () => this.hooks.applyModel(e.model),
      batch: () => this.hooks.onBatch(e.id),
      error: () => this.add(h("div", { class: "ag-error" }, e.message)),
      done: () => this.done(e),
    }[e.type];
    if (handler) handler();
  }

  toolCall(e) {
    const row = h("details", { class: "ag-tool running" });
    const summary = h("summary");
    const args = JSON.stringify(e.input);
    summary.append(icon("fa-spinner fa-spin"), h("b", {}, ` ${e.name}`), h("span", {}, ` ${args.length > 90 ? `${args.slice(0, 90)}…` : args}`));
    row.append(summary, h("pre", {}, JSON.stringify(e.input, null, 2)));
    this.tools.set(e.id, row);
    this.add(row);
  }

  toolResult(e) {
    const row = this.tools.get(e.id);
    if (!row) return;
    row.classList.remove("running");
    row.classList.toggle("failed", e.error);
    row.querySelector("summary i").className = `fa-solid ${e.error ? "fa-xmark" : "fa-check"}`;
    row.append(h("pre", { class: "ag-result" }, e.text));
  }

  done(e) {
    this.thinking?.remove();
    this.setBusy(false);
    const parts = [];
    if (e.turns) parts.push(`${e.turns} turn${e.turns === 1 ? "" : "s"}`);
    if (e.duration_ms) parts.push(`${(e.duration_ms / 1000).toFixed(1)} s`);
    if (e.subtype && e.subtype !== "success") parts.push(e.subtype.replace(/_/g, " "));
    if (parts.length) this.add(h("div", { class: "ag-done" }, parts.join(" · ")));
  }
}
