// Provenance: the code in another project that an entity or type was derived from.
import { authFetch } from "./auth.js";
import { el } from "./dom.js";

export function sourceSection(refs) {
  const box = el("div", { class: "provenance" });
  box.append(el("h2", {}, "Source"));
  const view = el("div");
  for (const ref of [...new Set(refs)]) {
    const b = el("button", { class: "ref", title: "Show the code" });
    b.append(el("i", { class: "fa-solid fa-code" }), ` ${ref}`);
    b.onclick = () => showSnippet(ref, view);
    box.append(b);
  }
  box.append(view);
  return box;
}

async function showSnippet(ref, view) {
  view.innerHTML = "";
  const res = await authFetch(`api/sources/snippet?ref=${encodeURIComponent(ref)}`);
  const data = await res.json();
  if (!res.ok) return view.append(el("p", { class: "ag-error" }, data.detail));
  const pre = el("pre", { class: "snippet" });
  data.lines.forEach((line, i) => {
    const n = data.start + i;
    const hot = n >= data.first && n <= data.last;
    pre.append(el("span", { class: hot ? "hot" : "" }, `${String(n).padStart(4)}  ${line}\n`));
  });
  view.append(el("div", { class: "sub" }, data.file), pre);
}
