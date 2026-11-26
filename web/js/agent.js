// The agent chat: talks to /ws/agent; the agent edits the model through tools and
// the server pushes each new model back here, so the canvas updates as it works.

import { authFetch, openSocket } from "./auth.js";
import { renderDiagrams, renderMarkdown } from "./markdown.js";

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
    this.sourcesBtn = h("button", { title: "Projects the agent may read" });
    this.sourcesBtn.onclick = () => this.sourcesPanel.classList.toggle("hidden");
    const buttons = h("span", { class: "ag-head-buttons" });
    buttons.append(this.sourcesBtn, reset);
    head.append(this.statusLine, buttons);
    this.sourcesPanel = h("div", { class: "ag-sources hidden" });
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
    this.root.append(head, this.sourcesPanel, this.log, this.composer);
    this.showSuggestions();
    this.loadSources();
  }

  async loadStatus() {
    try {
      const s = await (await authFetch("api/agent/status")).json();
      this.statusLine.textContent = `model: ${s.model}`;
      this.statusLine.title = `Claude Agent SDK ${s.sdk} · ${s.tools.length} workbench tools`;
    } catch {
      this.statusLine.textContent = "agent unavailable";
    }
  }

  // --- sources: other projects the agent may read ----------------------------------

  async loadSources() {
    const list = await (await authFetch("api/sources")).json();
    this.sourcesBtn.innerHTML = "";
    this.sourcesBtn.append(icon("fa-folder-tree"), ` ${list.length}`);
    this.sourcesPanel.innerHTML = "";
    this.sourcesPanel.append(h("p", { class: "sub" }, "The agent may read these projects (never run them). It proposes models from them for you to review."));
    for (const s of list) {
      const row = h("div", { class: "ag-source" });
      const del = h("button", { title: `Stop sharing ${s.name}` });
      del.append(icon("fa-xmark"));
      del.onclick = async () => {
        await authFetch(`api/sources/${encodeURIComponent(s.name)}`, { method: "DELETE" });
        this.loadSources();
      };
      row.append(h("b", {}, s.name), h("span", {}, s.path), del);
      this.sourcesPanel.append(row);
    }
    const form = h("form", { class: "ag-source-add" });
    const name = h("input", { placeholder: "name", spellcheck: "false" });
    const path = h("input", { placeholder: "/absolute/path/to/project", spellcheck: "false" });
    const add = h("button", { type: "submit" }, "Add");
    this.sourceError = h("div", { class: "ag-error" });
    form.onsubmit = (ev) => {
      ev.preventDefault();
      this.addSource(name.value.trim(), path.value.trim());
    };
    form.append(name, path, add);
    this.sourcesPanel.append(form, this.sourceError);
  }

  async addSource(name, path) {
    const res = await authFetch("api/sources", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, path }),
    });
    if (!res.ok) {
      this.sourceError.textContent = (await res.json()).detail;
      return;
    }
    this.loadSources();
  }

  connect() {
    this.ws = openSocket("ws/agent");
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
        div.append(renderMarkdown(e.text));
        this.add(div);
        renderDiagrams(div);
      },
      tool: () => this.toolCall(e),
      tool_result: () => this.toolResult(e),
      model: () => this.hooks.applyModel(e.model),
      batch: () => this.hooks.onBatch(e.id),
      error: () => this.add(h("div", { class: "ag-error" }, e.message)),
      proposal: () => this.proposal(e.proposal),
      proposal_resolved: () => this.resolved(e.accepted),
      done: () => this.done(e),
    }[e.type];
    if (handler) handler();
  }

  // --- proposals: changes the agent wants you to review ---------------------------------

  proposal(p) {
    const diff = this.hooks.onProposal(p);
    const card = h("div", { class: "ag-proposal" });
    card.append(h("div", { class: "ag-proposal-head" }, `Proposal: ${p.summary}`));
    const counts = `+${diff.added} new · ~${diff.changed} changed · −${diff.removed} removed` +
      (p.types.length ? ` · ${p.types.length} new type${p.types.length === 1 ? "" : "s"}` : "");
    card.append(h("div", { class: "sub" }, counts));
    for (const t of p.types) card.append(h("div", { class: "ag-type" }, `${t.library}/${t.name} extends ${t.extends}`));
    if (p.notes) {
      const notes = h("div", { class: "ag-msg ag-bot" });
      notes.append(renderMarkdown(p.notes));
      card.append(notes);
      renderDiagrams(notes);
    }
    const actions = h("div", { class: "row-actions" });
    const accept = h("button", { class: "primary" });
    accept.append(icon("fa-check"), " Accept");
    const reject = h("button");
    reject.append(icon("fa-xmark"), " Reject");
    accept.onclick = () => this.ws.send(JSON.stringify({ cmd: "accept" }));
    reject.onclick = () => this.ws.send(JSON.stringify({ cmd: "reject" }));
    actions.append(accept, reject);
    card.append(actions);
    this.pending = card;
    this.add(card);
  }

  resolved(accepted) {
    this.hooks.onProposalResolved(accepted);
    if (!this.pending) return;
    this.pending.querySelector(".row-actions").replaceWith(h("div", { class: "sub" }, accepted ? "✓ Accepted" : "✕ Rejected"));
    this.pending = null;
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
