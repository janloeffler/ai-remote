# AI Remote — Backend

## Local development

    python3 -m venv .venv
    .venv/bin/pip install --require-hashes -r requirements-dev.txt
    DATABASE_PATH=./dev.db \
    API_KEY=dev-key-0123456789-0123456789-0123 \
    SECRET_KEY=dev-secret-0123456789-0123456789 \
    SESSION_COOKIE_HTTPS_ONLY=false .venv/bin/uvicorn app.main:app --reload

Visit http://localhost:8000/login and log in with `dev-key-0123456789-0123456789-0123`.

Both secrets must be at least 32 characters or the app refuses to start — the values
above are exactly that length and are for local development only. Generate real ones
with `./generate-secrets.sh` (from the repo root).

## Environment variables

- `API_KEY` — shared secret (**min 32 characters**); the agent sends it as
  `Authorization: Bearer <API_KEY>`, the browser sends it once via the `/login` form and gets a
  signed session cookie back. The app fails fast at startup if this is unset or shorter than 32
  characters. It is the only credential gating remote command execution — generate it with
  `./generate-secrets.sh`. Session cookies carry a fingerprint of it, so changing it logs
  everyone out.
- `SECRET_KEY` — cookie-signing secret for the browser session (`starlette.SessionMiddleware`,
  **min 32 characters**). Same fail-fast behavior as `API_KEY`. Rotating it invalidates every
  existing session cookie.
- Poll intervals (`AI_REMOTE_INTERVAL_SECONDS`, `AI_REMOTE_ACTIVE_INTERVAL_SECONDS`,
  `ACTIVE_INTERVAL_DURATION_MIN`) are defaults; the first two can be overridden at runtime on
  the Settings page (stored in the database). The full variable list is in the root README.
- `TRUSTED_PROXY_HOPS` / `TRUSTED_PROXIES` — opt-in, set together, for per-client login
  throttling behind a reverse proxy. Default (`0` / empty) ignores `X-Forwarded-For`
  entirely. See `example.env` and `SECURITY.md`.
- `ENABLE_API_DOCS` — set to `true` to serve `/docs`, `/redoc` and `/openapi.json` while
  developing. They are off by default because they were reachable unauthenticated.
- `DATABASE_PATH` — path to the SQLite file (defaults to `app.db` in the working directory).
- `SESSION_COOKIE_HTTPS_ONLY` — whether the session cookie is marked `Secure` (only sent over
  HTTPS). Defaults to `true`, which is required for the real production deploy (Plesk terminates
  TLS). Set to `false` only for local `http://` testing — `run.sh` sets this automatically.
- `LOCAL_HOME_DIR` — the Mac's home directory; `~/` and `~` in the chat-list path filter expand
  to this (default `/Users/yourname`).
- `CHAT_HISTORY_PAGE_SIZE` — how many additional messages "Load more" loads per click on a
  chat's detail page (default `10`).

## Tests

    .venv/bin/pytest -v

## PWA install

Visiting the site on iPhone Safari and choosing "Add to Home Screen" installs it as a standalone
app using `static/manifest.json`; the icons live in `static/icons/` (SVG sources plus the
rendered PNGs: Apple touch icon, 192/512 px, maskable). After changing the icon, remove the old
Home Screen entry and add it again — iOS caches it.

## Deployment (Plesk Docker, example subdomain `your-domain.example.com`)

1. Build and push/import the image: `docker build -t ai-remote-backend .`
2. In Plesk's Docker extension, create a container from `ai-remote-backend`:
   - Port mapping: container `8000` → host port of your choice.
   - Volume: host `./data` → container `/data` (persists the SQLite file across
     container recreation/updates).
   - Environment: `API_KEY`, `SECRET_KEY` (generate both with `./generate-secrets.sh`), and
     `SESSION_COOKIE_HTTPS_ONLY=true`.
3. Bind your subdomain (e.g. `your-domain.example.com`) to the container's host port
   and enable Plesk's Let's Encrypt SSL for it.

For local development and testing (not production), see the repo-root `run.sh`
instead — it drives `docker-compose.yml` directly.

## Remote commands: allow-list, kill switch, audit log

- `AI_REMOTE_ALLOWED_PROJECTS` (see `example.env`) controls which projects the
  chat detail page's composer and the `/projects/new` screen will offer at all —
  set it here AND in the agent's own `.env` (two independently-configured copies).
- The pause button in the header (and on the Settings page) stops the agent from ever
  receiving new `resume_message`/`new_session` jobs (they stay `pending`); `fetch_full`
  keeps working while paused.
- `/jobs` shows every job ever created — type, target, prompt, status, result — as
  a simple audit trail.
