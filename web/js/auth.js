// The access token (only when the server sets SIMBENCH_TOKEN; see server/auth.py) and
// URLs relative to the page, so the workbench also works under a path prefix such as
// https://host/sim-bench/.

const KEY = "sim-bench-token";

export const token = () => localStorage.getItem(KEY) || "";

// fetch with the token as a bearer header. A 401 means a missing or wrong token:
// forget it and go to the landing page, which asks for it.
export async function authFetch(url, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (token()) headers.Authorization = `Bearer ${token()}`;
  const res = await fetch(url, { ...opts, headers });
  if (res.status === 401) {
    localStorage.removeItem(KEY);
    location.replace("./");
  }
  return res;
}

// A WebSocket to a path relative to the page. Browsers can't set headers on one, so
// the token travels as a subprotocol, which the server answers with "sim-bench".
export function openSocket(path) {
  const url = new URL(path, location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return token() ? new WebSocket(url, ["sim-bench", `token.${token()}`]) : new WebSocket(url);
}
