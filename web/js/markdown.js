// Markdown for agent messages, built as DOM nodes (text always goes in as textContent,
// so nothing the agent writes becomes HTML). Covers what chat replies use: headings,
// paragraphs, lists (nested, ordered or not), fenced code, block quotes, tables, rules,
// and inline code, bold, italic, strike-through and links. ```mermaid fences become
// diagrams when the vendored mermaid bundle is there (see renderDiagrams).

const FENCE = /^\s*(```|~~~)\s*([\w-]*)/;
const HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const QUOTE = /^\s*>\s?/;
const ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

function node(tag, attrs = {}, text) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (text !== undefined) n.textContent = text;
  return n;
}

export function renderMarkdown(text) {
  const frag = document.createDocumentFragment();
  const lines = String(text).replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    if (!lines[i].trim()) {
      i++;
      continue;
    }
    const block = BLOCKS.find((b) => b.starts(lines, i));
    const [el, next] = (block || PARAGRAPH).read(lines, i);
    frag.append(el);
    i = next;
  }
  return frag;
}

// Each block kind: starts(lines, i) -> bool, read(lines, i) -> [element, next index].
const BLOCKS = [
  { starts: (ls, i) => FENCE.test(ls[i]), read: readFence },
  { starts: (ls, i) => HEADING.test(ls[i]), read: readHeading },
  { starts: (ls, i) => RULE.test(ls[i]), read: (ls, i) => [node("hr"), i + 1] },
  { starts: (ls, i) => QUOTE.test(ls[i]), read: readQuote },
  { starts: (ls, i) => ITEM.test(ls[i]), read: readList },
  { starts: (ls, i) => isTable(ls, i), read: readTable },
];
const PARAGRAPH = { read: readParagraph };

function readFence(lines, i) {
  const [, mark, lang] = lines[i].match(FENCE);
  const body = [];
  let j = i + 1;
  while (j < lines.length && !lines[j].trim().startsWith(mark)) body.push(lines[j++]);
  const source = body.join("\n");
  if (lang === "mermaid") {
    const box = node("div", { class: "md-mermaid" });
    box.append(node("pre", {}, source));
    return [box, j + 1];
  }
  const pre = node("pre");
  pre.append(node("code", lang ? { class: `lang-${lang}` } : {}, source));
  return [pre, j + 1];
}

function readHeading(lines, i) {
  const [, hashes, title] = lines[i].match(HEADING);
  const h = node(`h${Math.min(6, hashes.length + 2)}`, { class: "md-h" }); // chat-sized
  h.append(inline(title));
  return [h, i + 1];
}

function readQuote(lines, i) {
  const body = [];
  while (i < lines.length && QUOTE.test(lines[i])) body.push(lines[i++].replace(QUOTE, ""));
  const q = node("blockquote");
  q.append(renderMarkdown(body.join("\n")));
  return [q, i];
}

// Items at the first item's indent; deeper lines belong to the item above them.
function readList(lines, i) {
  const [, indent, marker] = lines[i].match(ITEM);
  const base = indent.length;
  const list = node(/\d/.test(marker) ? "ol" : "ul");
  const start = parseInt(marker, 10);
  if (start > 1) list.setAttribute("start", String(start));
  let item = null, nested = [];
  const close = () => item && nested.length && item.append(renderMarkdown(dedent(nested)));
  for (; i < lines.length; i++) {
    const m = lines[i].match(ITEM);
    if (m && m[1].length === base) {
      close();
      item = node("li");
      item.append(inline(m[3]));
      list.append(item);
      nested = [];
    } else if (lines[i].trim() && indentOf(lines[i]) > base) {
      nested.push(lines[i]);
    } else break;
  }
  close();
  return [list, i];
}

function indentOf(line) {
  return line.match(/^\s*/)[0].length;
}

function dedent(lines) {
  const cut = Math.min(...lines.map(indentOf));
  return lines.map((l) => l.slice(cut)).join("\n");
}

function isTable(lines, i) {
  return lines[i].includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]) && lines[i + 1].includes("-");
}

function isRow(line) {
  return line.includes("|") && line.trim() !== "";
}

function cells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

function readTable(lines, i) {
  const table = node("table", { class: "md-table" });
  const align = cells(lines[i + 1]).map((c) => (c.endsWith(":") ? (c.startsWith(":") ? "center" : "right") : ""));
  const row = (line, tag) => {
    const tr = node("tr");
    cells(line).forEach((c, k) => {
      const td = node(tag, align[k] ? { style: `text-align:${align[k]}` } : {});
      td.append(inline(c));
      tr.append(td);
    });
    return tr;
  };
  const head = node("thead");
  head.append(row(lines[i], "th"));
  const body = node("tbody");
  let j = i + 2;
  for (; j < lines.length && isRow(lines[j]); j++) body.append(row(lines[j], "td"));
  table.append(head, body);
  const wrap = node("div", { class: "md-table-wrap" });
  wrap.append(table);
  return [wrap, j];
}

// Lines up to a blank line or the start of another block; single newlines are breaks.
function readParagraph(lines, i) {
  const p = node("p");
  let first = true;
  for (; i < lines.length && lines[i].trim(); i++) {
    if (!first && BLOCKS.some((b) => b.starts(lines, i))) break;
    if (!first) p.append(node("br"));
    p.append(inline(lines[i].trim()));
    first = false;
  }
  return [p, i];
}

// --- inline ----------------------------------------------------------------------

const SPANS = [
  { re: /`([^`]+)`/, make: (m) => node("code", {}, m[1]) },
  { re: /\*\*(.+?)\*\*|__(.+?)__/, make: (m) => wrap("strong", m[1] ?? m[2]) },
  { re: /~~(.+?)~~/, make: (m) => wrap("del", m[1]) },
  { re: /(?<![\w*])\*(?![\s*])(.+?)(?<!\s)\*(?![\w*])|(?<!\w)_(?![\s_])(.+?)(?<!\s)_(?!\w)/, make: (m) => wrap("em", m[1] ?? m[2]) },
  { re: /\[([^\]]+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)/, make: (m) => link(m[2], m[1]) }, // one level of () in URLs
  { re: /\bhttps?:\/\/[^\s<>()]+[^\s<>().,;:!?'"]/, make: (m) => link(m[0], m[0]) },
];

export function inline(text) {
  const frag = document.createDocumentFragment();
  let rest = text;
  while (rest) {
    let best = null;
    for (const span of SPANS) {
      const m = span.re.exec(rest);
      if (m && comesFirst(m, best)) best = { m, span };
    }
    if (!best) break;
    frag.append(rest.slice(0, best.m.index), best.span.make(best.m));
    rest = rest.slice(best.m.index + best.m[0].length);
  }
  if (rest) frag.append(rest);
  return frag;
}

function comesFirst(m, best) {
  return !best || m.index < best.m.index;
}

function wrap(tag, text) {
  const n = node(tag);
  n.append(inline(text));
  return n;
}

// Only web and mail links; anything else (javascript: and the like) stays plain text.
function link(href, text) {
  if (!/^(https?:|mailto:)/i.test(href)) return document.createTextNode(text);
  const a = node("a", { href, target: "_blank", rel: "noopener noreferrer" });
  a.append(inline(text));
  return a;
}

// --- mermaid -----------------------------------------------------------------------

const MERMAID = "vendor/mermaid/mermaid.min.js";
let mermaid = null; // a promise of the library, loaded on first use
let diagramCount = 0;

function loadMermaid() {
  mermaid ||= new Promise((resolve, reject) => {
    const s = node("script", { src: MERMAID });
    s.onload = () => {
      window.mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "strict" });
      resolve(window.mermaid);
    };
    s.onerror = () => reject(new Error("mermaid is not installed (web/vendor/mermaid)"));
    document.head.append(s);
  });
  return mermaid;
}

// Turn the ```mermaid blocks under `root` into diagrams. A block that fails to parse,
// or a missing library, leaves the source as a code block with a note.
export async function renderDiagrams(root) {
  const boxes = [...root.querySelectorAll(".md-mermaid:not(.done)")];
  if (!boxes.length) return;
  for (const box of boxes) box.classList.add("done");
  let lib;
  try {
    lib = await loadMermaid();
  } catch (e) {
    for (const box of boxes) box.append(node("div", { class: "md-note" }, e.message));
    return;
  }
  for (const box of boxes) await drawDiagram(lib, box);
}

async function drawDiagram(lib, box) {
  const source = box.querySelector("pre").textContent;
  const id = `md-diagram-${++diagramCount}`;
  try {
    const { svg } = await lib.render(id, source);
    box.innerHTML = svg; // mermaid's own output; securityLevel "strict" sanitizes it
  } catch (e) {
    document.getElementById(`d${id}`)?.remove(); // the scratch element mermaid leaves on errors
    box.append(node("div", { class: "md-note" }, `diagram error: ${e.message || e}`));
  }
}
