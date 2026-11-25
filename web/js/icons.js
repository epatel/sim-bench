// Draw Font Awesome icons on a canvas: look up each icon's glyph via the CSS.

export const ICON_FONT = '900 18px "Font Awesome 7 Free"';
const cache = new Map();

export async function loadIconFont() {
  await document.fonts.load(ICON_FONT, "");
}

// "fa-server" -> the glyph character, read from the --fa custom property.
export function glyph(name) {
  if (cache.has(name)) return cache.get(name);
  const el = document.createElement("i");
  el.className = `fa-solid ${name}`;
  el.style.position = "absolute";
  el.style.visibility = "hidden";
  document.body.appendChild(el);
  const raw = getComputedStyle(el).getPropertyValue("--fa").trim();
  el.remove();
  const ch = decode(raw) || ""; // fa-cube as a fallback
  cache.set(name, ch);
  return ch;
}

function decode(raw) {
  const s = raw.replace(/^["']|["']$/g, "");
  if (!s) return "";
  if (s.startsWith("\\")) return String.fromCodePoint(parseInt(s.slice(1), 16));
  return s;
}
