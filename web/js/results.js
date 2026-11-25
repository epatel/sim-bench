// Results drawer: compare scenarios of a batch (or a what-if) for one metric.
//  - dot + 95% CI per scenario, with a baseline reference line
//  - the metric over time (mean over seeds), one 2px line per scenario
//  - a table view with the change versus baseline
// Colors: categorical slots in fixed order (validated against the panel surface,
// see the dataviz palette); a scenario keeps its slot by position in the batch.
import { api } from "./api.js";
import { fmt } from "./canvas.js";

const SLOTS = 8;
const SVG = "http://www.w3.org/2000/svg";

function h(tag, attrs = {}, text) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (text !== undefined) n.textContent = text;
  return n;
}

function s(tag, attrs = {}, text) {
  const n = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (text !== undefined) n.textContent = text;
  return n;
}

const slotOf = (i) => `var(--series-${(i % SLOTS) + 1})`;

// Prefer an end-to-end latency, then any latency, then the first metric.
function defaultMetric(metrics) {
  return (
    metrics.find((m) => /^entities\.[^.]+\.latency\.mean$/.test(m)) ||
    metrics.find((m) => m.endsWith(".mean")) ||
    metrics[0]
  );
}

export class Results {
  constructor(root) {
    this.root = root;
    this.batch = null;
    this.metric = null;
    this.view = "chart";
    this.poll = null;
    this.build();
  }

  build() {
    this.root.innerHTML = "";
    const head = h("div", { class: "rs-head" });
    const title = h("button", { class: "rs-toggle", title: "Show or hide results" });
    title.innerHTML = '<i class="fa-solid fa-chart-line"></i> Results';
    title.onclick = () => this.toggle();
    this.batchSelect = h("select", { class: "rs-batch", title: "Earlier runs" });
    this.batchSelect.onchange = () => this.show(Number(this.batchSelect.value));
    this.metricInput = h("input", { class: "rs-metric", list: "rs-metrics", spellcheck: "false", title: "Metric to compare" });
    this.metricList = h("datalist", { id: "rs-metrics" });
    this.metricInput.onchange = () => this.pickMetric(this.metricInput.value.trim());
    this.viewBtn = h("button", { title: "Chart or table" });
    this.viewBtn.onclick = () => {
      this.view = this.view === "chart" ? "table" : "chart";
      this.render();
    };
    this.status = h("span", { class: "rs-status" });
    head.append(title, this.batchSelect, this.metricInput, this.metricList, this.viewBtn, this.status);
    this.body = h("div", { class: "rs-body" });
    this.tip = h("div", { class: "rs-tip hidden" });
    this.root.append(head, this.body, this.tip);
    this.render();
  }

  toggle(open = !this.root.classList.contains("open")) {
    this.root.classList.toggle("open", open);
    if (open) this.refreshList();
  }

  async refreshList() {
    const list = await api.batches();
    this.batchSelect.innerHTML = "";
    for (const b of list) {
      const what = b.kind === "whatif" ? `what-if @t=${fmt(b.params.from)}` : `${b.params.seeds.length} seeds × ${fmt(b.params.until)}`;
      const label = `#${b.id} ${b.model_name} · ${b.params.scenarios.join(", ")} · ${what}${b.status === "done" ? "" : ` (${b.status})`}`;
      this.batchSelect.append(h("option", { value: b.id }, label));
    }
    if (this.batch) this.batchSelect.value = this.batch.id;
  }

  // Load a batch; keeps polling while it runs.
  async show(id) {
    clearTimeout(this.poll);
    this.toggle(true);
    const first = await api.batch(id);
    const metric = this.metric && first.metrics.includes(this.metric) ? this.metric : defaultMetric(first.metrics);
    this.batch = metric ? await api.batch(id, metric) : first;
    this.metric = metric;
    this.metricList.innerHTML = "";
    for (const m of this.batch.metrics) this.metricList.append(h("option", { value: m }));
    this.metricInput.value = metric || "";
    await this.refreshList();
    this.render();
    if (this.batch.status === "running") this.poll = setTimeout(() => this.show(id), 700);
  }

  async pickMetric(metric) {
    if (!this.batch || !this.batch.metrics.includes(metric)) return;
    this.metric = metric;
    this.batch = await api.batch(this.batch.id, metric);
    this.render();
  }

  // --- rendering ---------------------------------------------------------------

  render() {
    this.body.innerHTML = "";
    this.viewBtn.innerHTML = this.view === "chart" ? '<i class="fa-solid fa-table"></i>' : '<i class="fa-solid fa-chart-simple"></i>';
    const b = this.batch;
    if (!b) {
      this.status.textContent = "";
      this.body.append(h("p", { class: "empty" }, "Run a batch or a what-if to compare scenarios."));
      return;
    }
    this.renderStatus(b);
    const scenarios = b.params.scenarios.filter((sid) => b.aggregate[sid]);
    if (!scenarios.length || !this.metric) {
      this.body.append(h("p", { class: "empty" }, b.status === "failed" ? b.error : "Waiting for the first runs…"));
      return;
    }
    const rows = scenarios.map((sid, i) => ({ sid, color: slotOf(i), stats: b.aggregate[sid][this.metric] }));
    this.body.append(this.legend(rows));
    if (this.view === "table") return this.body.append(this.table(rows));
    const charts = h("div", { class: "rs-charts" });
    charts.append(this.dotPlot(rows), this.lineChart(rows));
    this.body.append(charts);
  }

  renderStatus(b) {
    const total = b.kind === "whatif" ? 2 : b.params.scenarios.length * b.params.seeds.length;
    const parts = [b.kind === "whatif" ? `what-if from t=${fmt(b.params.from)}, +${fmt(b.params.horizon)}` : `${b.params.seeds.length} seeds, until ${fmt(b.params.until)}`];
    if (b.status === "running") parts.push(`running ${b.runs}/${total}`);
    if (b.status === "failed") parts.push(`failed: ${b.error}`);
    if (b.truncated) parts.push("⚠ some runs hit the event cap");
    this.status.textContent = parts.join(" · ");
  }

  legend(rows) {
    const lg = h("div", { class: "rs-legend" });
    for (const r of rows) {
      const item = h("span", { class: "rs-key" });
      item.append(h("i", { style: `background:${r.color}` }), document.createTextNode(r.sid));
      lg.append(item);
    }
    return lg;
  }

  table(rows) {
    const base = rows[0].stats;
    const t = h("table", { class: "metrics rs-table" });
    const head = h("tr");
    for (const c of ["scenario", "runs", "mean", "± 95% CI", "vs " + rows[0].sid]) head.append(h("th", {}, c));
    t.append(head);
    for (const r of rows) {
      const tr = h("tr");
      const name = h("td");
      name.append(h("i", { class: "rs-dot", style: `background:${r.color}` }), document.createTextNode(r.sid));
      tr.append(name, h("td", {}, fmt(r.stats?.n)), h("td", {}, fmt(r.stats?.mean)), h("td", {}, fmt(r.stats?.ci95)), h("td", {}, delta(r.stats, base)));
      t.append(tr);
    }
    return t;
  }

  // One row per scenario: mean as a dot, the 95% CI as a whisker.
  dotPlot(rows) {
    const W = 360, rowH = 30, top = 22, left = 112, right = 18;
    const H = top + rows.length * rowH + 22;
    const lo = Math.min(...rows.map((r) => (r.stats ? r.stats.mean - r.stats.ci95 : Infinity)));
    const hi = Math.max(...rows.map((r) => (r.stats ? r.stats.mean + r.stats.ci95 : -Infinity)));
    const x = scale(lo, hi, left, W - right);
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "rs-svg", role: "img", "aria-label": `${this.metric} by scenario, mean and 95% interval` });
    svg.append(s("text", { x: 0, y: 12, class: "rs-title" }, "mean ± 95% CI"));
    for (const t of x.ticks) {
      svg.append(s("line", { x1: x(t), x2: x(t), y1: top - 4, y2: H - 18, class: "rs-grid" }));
      svg.append(s("text", { x: x(t), y: H - 4, class: "rs-tick", "text-anchor": "middle" }, fmt(t)));
    }
    const base = rows[0].stats;
    if (base) svg.append(s("line", { x1: x(base.mean), x2: x(base.mean), y1: top - 6, y2: H - 18, class: "rs-ref" }));
    rows.forEach((r, i) => {
      const y = top + i * rowH + rowH / 2;
      svg.append(s("text", { x: left - 10, y: y + 4, class: "rs-label", "text-anchor": "end" }, clipText(r.sid, 15)));
      if (!r.stats) return;
      const { mean, ci95 } = r.stats;
      svg.append(s("line", { x1: x(mean - ci95), x2: x(mean + ci95), y1: y, y2: y, stroke: r.color, "stroke-width": 2, "stroke-linecap": "round" }));
      const dot = s("circle", { cx: x(mean), cy: y, r: 5, fill: r.color, class: "rs-mark" });
      const hit = s("rect", { x: left - 100, y: y - rowH / 2, width: W - left + 100, height: rowH, fill: "transparent" });
      hit.addEventListener("mousemove", (ev) => this.showTip(ev, [[r, `${fmt(mean)} ± ${fmt(ci95)}  (${delta(r.stats, base)}, n=${r.stats.n})`]]));
      hit.addEventListener("mouseleave", () => this.hideTip());
      svg.append(dot, hit);
    });
    return wrap(svg, "rs-dots");
  }

  // The metric over time, mean over seeds, one line per scenario.
  lineChart(rows) {
    const series = this.batch.series || {};
    const W = 520, H = 200, left = 52, right = 16, top = 22, bottom = 22;
    const lines = rows.map((r) => ({ ...r, pts: series[r.sid] || [] })).filter((l) => l.pts.length);
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "rs-svg", role: "img", "aria-label": `${this.metric} over time` });
    svg.append(s("text", { x: 0, y: 12, class: "rs-title" }, "over time (mean over seeds)"));
    if (!lines.length) return wrap(svg, "rs-lines");
    const all = lines.flatMap((l) => l.pts);
    const x = scale(Math.min(...all.map((p) => p[0])), Math.max(...all.map((p) => p[0])), left, W - right);
    // lines compare shape and level, so the y-axis fits the data (no forced zero)
    const y = scale(Math.min(...all.map((p) => p[1])), Math.max(...all.map((p) => p[1])), H - bottom, top);
    for (const t of y.ticks) {
      svg.append(s("line", { x1: left, x2: W - right, y1: y(t), y2: y(t), class: "rs-grid" }));
      svg.append(s("text", { x: left - 6, y: y(t) + 3, class: "rs-tick", "text-anchor": "end" }, fmt(t)));
    }
    for (const t of x.ticks) svg.append(s("text", { x: x(t), y: H - 6, class: "rs-tick", "text-anchor": "middle" }, fmt(t)));
    for (const l of lines) {
      const d = l.pts.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
      svg.append(s("path", { d, fill: "none", stroke: l.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
      const end = l.pts[l.pts.length - 1];
      svg.append(s("circle", { cx: x(end[0]), cy: y(end[1]), r: 4, fill: l.color, class: "rs-mark" }));
    }
    // crosshair + tooltip
    const cross = s("line", { y1: top, y2: H - bottom, class: "rs-cross hidden" });
    const hit = s("rect", { x: left, y: top, width: W - left - right, height: H - top - bottom, fill: "transparent" });
    hit.addEventListener("mousemove", (ev) => {
      const box = svg.getBoundingClientRect();
      const px = ((ev.clientX - box.left) / box.width) * W;
      const t = x.invert(px);
      const items = lines.map((l) => [l, nearest(l.pts, t)]).filter(([, p]) => p);
      if (!items.length) return;
      const tx = items[0][1][0];
      cross.setAttribute("x1", x(tx));
      cross.setAttribute("x2", x(tx));
      cross.classList.remove("hidden");
      this.showTip(ev, items.map(([l, p]) => [l, fmt(p[1])]), `t = ${fmt(tx)}`);
    });
    hit.addEventListener("mouseleave", () => {
      cross.classList.add("hidden");
      this.hideTip();
    });
    svg.append(cross, hit);
    return wrap(svg, "rs-lines");
  }

  showTip(ev, items, heading) {
    this.tip.innerHTML = "";
    if (heading) this.tip.append(h("div", { class: "rs-tip-head" }, heading));
    for (const [r, text] of items) {
      const line = h("div");
      line.append(h("i", { class: "rs-dot", style: `background:${r.color}` }), document.createTextNode(`${r.sid}  ${text}`));
      this.tip.append(line);
    }
    const box = this.root.getBoundingClientRect();
    this.tip.style.left = `${Math.min(ev.clientX - box.left + 14, box.width - 260)}px`;
    this.tip.style.top = `${ev.clientY - box.top + 10}px`;
    this.tip.classList.remove("hidden");
  }

  hideTip() {
    this.tip.classList.add("hidden");
  }
}

// --- helpers ------------------------------------------------------------------------

function wrap(svg, cls) {
  const d = h("div", { class: cls });
  d.append(svg);
  return d;
}

function delta(stats, base) {
  const comparable = stats && base && base.mean !== 0;
  if (!comparable) return "–";
  if (stats === base) return "baseline";
  const pct = ((stats.mean - base.mean) / Math.abs(base.mean)) * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%`;
}

function clipText(text, max) {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function nearest(pts, t) {
  let best = null;
  for (const p of pts) if (!best || Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
  return best;
}

// Linear scale with clean ticks; a zero-width domain is padded.
function scale(d0, d1, r0, r1) {
  if (!Number.isFinite(d0) || !Number.isFinite(d1)) [d0, d1] = [0, 1];
  if (d0 === d1) [d0, d1] = [d0 - Math.abs(d0 || 1) * 0.1, d1 + Math.abs(d1 || 1) * 0.1];
  const step = niceStep((d1 - d0) / 4);
  const lo = Math.floor(d0 / step) * step, hi = Math.ceil(d1 / step) * step;
  const f = (v) => r0 + ((v - lo) / (hi - lo)) * (r1 - r0);
  f.invert = (p) => lo + ((p - r0) / (r1 - r0)) * (hi - lo);
  f.ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) f.ticks.push(Number(v.toPrecision(12)));
  return f;
}

function niceStep(raw) {
  const p = 10 ** Math.floor(Math.log10(raw));
  const m = raw / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
}
