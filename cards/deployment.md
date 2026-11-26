# Deployment (a public host behind nginx)

Read when changing the access token, URLs in the frontend, or `deploy/`.

- **Token gate** (`server/auth.py`). Set `SIMBENCH_TOKEN` in the environment or in
  `.env` at the repo root (gitignored, chmod 600). With no token, nothing changes, so
  localhost stays open. With a token, `TokenGate` (pure ASGI middleware, so it also
  covers WebSockets) refuses `/api/*` and `/ws/*` without it. Static files stay public,
  since the repo is public anyway.
  - Fetches send `Authorization: Bearer <token>` (`web/js/auth.js` `authFetch`). A 401
    clears the stored token and goes back to the landing page.
  - WebSockets can't carry headers from a browser. They offer the subprotocols
    `["sim-bench", "token.<token>"]`, and the server accepts with `sim-bench`
    (`app.accept`). This keeps the token out of URLs and nginx logs.
  - `/` serves `web/landing.html` when gated. It takes `?token=` (stores it and strips
    it from the URL) or asks for it, checks it against `/api/auth`, keeps it in
    localStorage (`sim-bench-token`) and opens `index.html`.
- **Path prefix.** Every frontend URL is relative to the page (`api/...`, `ws/run`,
  `css/...`), so the app runs under any prefix with nginx stripping it. There is no
  `root_path` setting. Never add a URL that starts with `/`.
- **`make deploy`** rsyncs the working tree (rules in `deploy/rsync-exclude`; `.env`,
  `.simbench/`, the runs DB and saved models in `projects/` are never touched) to
  `DEPLOY_HOST:DEPLOY_DIR` (default `home:sim-bench`). It then runs
  `deploy/remote-install.sh`, which creates the venv, makes a random token in `.env`
  on first run, installs `deploy/sim-bench.service` (port 8780, loopback) and
  restarts the service.
- The server fetches its own mermaid bundle (`scripts/vendor_mermaid.py`) during the install. rsync excludes `web/vendor/mermaid/`, so `make deploy` neither copies nor deletes it. A failed fetch doesn't stop the deploy; diagrams then show as code.
- **nginx.** `deploy/nginx-sim-bench.conf` is the location block, with upgrade
  headers, no buffering and long timeouts. On home.memention.net it is appended to
  `/etc/nginx/snippets/memention-endpoints.conf` (443 only). That was done by hand
  once, with a backup next to the file. `make deploy` doesn't touch nginx.
- **Agent.** The service runs as the user who ran `claude` login, with
  `~/.local/bin` on PATH. API keys are stripped as everywhere else.
