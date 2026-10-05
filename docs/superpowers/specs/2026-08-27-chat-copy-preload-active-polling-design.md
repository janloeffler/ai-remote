# Copy Button, Auto-Preload, Adaptive Poll Interval — Design

**Date:** 2026-08-27
**Status:** Approved for planning
**Builds on:** `docs/superpowers/specs/2026-08-06-load-more-eta-design.md` (agent check-in / ETA model),
`docs/superpowers/specs/2026-08-05-frontend-polish-design.md` (incremental history loading)

## Overview

Three independent frontend/agent improvements, bundled into one spec because they touch the same
files and were brainstormed together — each can be implemented and shipped separately if needed.

1. **Copy button** on every chat message, copying rich formatting (not just plain text).
2. **Auto-preload**: the periodic background sync already ships session metadata every cycle: it
   should also carry the last few messages, so a chat isn't empty until "Mehr laden" is clicked.
3. **Adaptive poll interval**: the agent's check-in interval (`AI_REMOTE_INTERVAL_SECONDS`,
   default 60s) temporarily drops to a faster `AI_REMOTE_ACTIVE_INTERVAL_SECONDS` (default 10s)
   for `ACTIVE_INTERVAL_DURATION_MIN` (default 5min) after the user does something that queues a
   job, so follow-up actions (further "Mehr laden" clicks, additional commands) don't each wait up
   to a minute.

No auth/security model changes. No new external dependencies.

## 1. Copy button per message

Purely frontend — `detail.html`'s message `<div>` already contains the server-rendered, sanitized
markdown→HTML (`markdown_filter.render_markdown`), so there's no new backend surface.

### Markup

`backend/app/templates/detail.html`, inside the `.messages` loop:

```html
<div class="message message-{{ m.role }}">
  <div class="message-head">
    <time>{{ m.timestamp | de_datetime }}</time>
    <button class="copy-button" type="button" aria-label="Nachricht kopieren">📋</button>
  </div>
  <div class="message-content">{{ m.content | markdown | safe }}</div>
</div>
```

`.message-content` is a new wrapper around the existing rendered-markdown `<div>` — needed so the
copy handler has an unambiguous element to read `innerHTML`/`textContent` from (today the rendered
HTML is the message div's only child; wrapping it explicitly avoids accidentally including the
`<time>`/button markup in what gets copied).

### Behavior

New block in `app.js`, using the Clipboard API's multi-format write so rich targets (Word, Gmail,
Slack) keep bold/lists/code blocks, and plain-text targets fall back cleanly — exactly what
ChatGPT's copy button does:

```js
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".copy-button").forEach((button) => {
    const content = button.closest(".message").querySelector(".message-content");
    button.addEventListener("click", async () => {
      const html = content.innerHTML;
      const text = content.textContent;
      try {
        if (window.ClipboardItem) {
          await navigator.clipboard.write([
            new ClipboardItem({
              "text/html": new Blob([html], { type: "text/html" }),
              "text/plain": new Blob([text], { type: "text/plain" }),
            }),
          ]);
        } else {
          await navigator.clipboard.writeText(text);
        }
        button.textContent = "✓";
      } catch (error) {
        button.textContent = "✗";
      }
      setTimeout(() => { button.textContent = "📋"; }, 1500);
    });
  });
});
```

- `ClipboardItem` feature-detected: Safari/older browsers without multi-MIME clipboard support
  fall back to plain text rather than throwing.
- Button is always visible (not hover-only) — consistent with the rest of this admin UI, where
  `Mehr laden` / `Gesamte Historie laden` etc. are always-on buttons, not hover affordances.
- No countdown/polling involved; this is a single synchronous-feeling action.

### CSS

`style.css`: `.message-head` becomes a flex row (`justify-content: space-between`) holding the
existing `<time>` and the new button; `.copy-button` styled as a small icon-only button matching
the existing `.history-actions button` sizing, not the full-width text buttons elsewhere.

## 2. Auto-preload recent messages on sync

### Current behavior

Every agent cycle (`AI_REMOTE_INTERVAL_SECONDS`, default 60s), `run_cycle` in `agent/agent/main.py`
computes `deltas` — sessions whose `last_updated_at` changed since the last sync — and uploads only
session-level metadata (title, `last_message_preview`, counts) via `POST /sync/index`. The
`messages` table is untouched by sync; it's populated only by a `fetch_full` job, itself only
created when the user clicks "Mehr laden" / "Gesamte Historie laden" on the detail page — meaning a
never-before-opened chat shows nothing but the one-line preview until the user clicks and waits for
an agent round-trip.

### Change

For every session in `deltas` (already the set of sessions worth re-reading — unchanged sessions
are untouched and cost nothing extra), the agent also collects the session's last
`CHAT_HISTORY_PAGE_SIZE` (10, existing setting, reused rather than adding a second page-size knob)
messages and attaches them to the sync payload as `recent_messages`.

**`claude_code_source.py`:** today `_parse_session_file` (metadata: title/counts/preview) and
`get_full_messages` (full message list, only called on-demand today) each read+decode the same
`.jsonl` file independently. Preloading on every changed cycle would double that cost, so this is
merged into one pass: `_parse_session_file` also collects `(idx, role, timestamp, content)` for
every user/assistant event as it already iterates them, and returns `recent_messages` (last N)
alongside the existing metadata fields. `get_full_messages` (used by the `fetch_full` job executor
for "Mehr laden") is unchanged — it still does its own full read, since a `fetch_full` job may ask
for the *entire* history (`count=None`), which the capped preload doesn't cover in one line, and
this on-demand path is decoupled from the every-cycle case by design (its cost is already spent on
an explicit user action, not once per idle cycle).

**`cursor_source.py`:** `enrich_with_messages` already loads the *entire* bubble list per changed
session (via `_load_bubbles`) just to compute `message_count`/`last_message_preview`, then discards
it. This one is free: it just also keeps `bubbles[-N:]` and attaches it as `recent_messages` — no
new read at all.

**`main.py`** (agent): after computing `deltas` for both sources, no change needed beyond the two
functions above already populating `recent_messages` on each session dict — `uploader.push_sync`
forwards whatever's in the dict.

### Backend

`SessionIn` (`backend/app/models.py`) gains:

```python
recent_messages: list[MessageIn] = []
```

`sync_index` (`main.py`), after `db.upsert_session(conn, session.model_dump())`, calls a new
`db.apply_recent_messages(conn, session.id, [m.model_dump() for m in session.recent_messages])`
that guards against ever *shrinking* what's loaded:

```python
def apply_recent_messages(conn, session_id: str, messages: list[dict]) -> None:
    if not messages:
        return
    session = get_session(conn, session_id)
    if session is None or len(messages) <= session["loaded_message_count"]:
        return
    replace_messages(conn, session_id, messages)  # is_complete stays None: existing
    # message_count-comparison logic in replace_messages already handles the flag correctly
```

This one guard is what makes auto-preload strictly additive:

- First-ever sync of a session (`loaded_message_count == 0`): preload fills in the last N
  immediately — the detail page shows real messages on first open, no click needed.
- A session that changed since last sync and currently has fewer than N loaded (e.g. nothing
  loaded, or a previous preload): preload refreshes to the latest last-N.
- A session where the user already clicked "Gesamte Historie laden" (loaded_message_count could be
  in the hundreds): a routine sync's 10-message tail is smaller, so the guard skips it — the
  already-loaded full history is never truncated back down to 10.

**Known limitation (accepted, not solved here):** if a user loaded full history and the session
keeps growing via the CLI directly, the newest messages beyond the full-history threshold still
need a manual "Mehr laden" — same as today's behavior for that case. This only regresses the
already-rare "loaded everything AND still actively growing" case; every other case (the vast
majority: first open, casually-sized chats) goes from "empty until you click" to "already there."

## 3. Adaptive agent poll interval

### Current behavior

The agent (`agent/agent/main.py`) sleeps a fixed `config.interval_seconds` (from
`AI_REMOTE_INTERVAL_SECONDS`, default 60) between cycles. `_compute_eta_seconds` in
`backend/app/main.py` estimates, from `db.get_last_agent_contact`, how long until the *next* check-in
— always assuming that fixed interval.

### New settings (`backend/app/settings.py`)

```python
AI_REMOTE_ACTIVE_INTERVAL_SECONDS = int(os.environ.get("AI_REMOTE_ACTIVE_INTERVAL_SECONDS") or "10")
ACTIVE_INTERVAL_DURATION_MIN = int(os.environ.get("ACTIVE_INTERVAL_DURATION_MIN") or "5")
```

Backend-only: the agent no longer decides its own interval locally (see below), so these two don't
need to reach `agent/agent/config.py`, `setup-agent.sh`, or the launchd plist — only the backend
container needs them, following the existing `docker-compose.yml` /
`deploy-production-scp.sh` pattern for `CHAT_HISTORY_PAGE_SIZE`/`AI_REMOTE_INTERVAL_SECONDS`.

### DB: global active window

`schema.sql`'s `agent_status` singleton table gains a nullable column:

```sql
CREATE TABLE IF NOT EXISTS agent_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_contact_at TEXT,
    active_until TEXT
);
```

Plus a migration function mirroring `_migrate_add_loaded_message_count` for existing on-disk DBs.

`db.py` gains:

```python
def bump_active_interval(conn: sqlite3.Connection, duration_min: int) -> None:
    until = (datetime.now(timezone.utc) + timedelta(minutes=duration_min)).isoformat()
    conn.execute("UPDATE agent_status SET active_until = ? WHERE id = 1", (until,))
    conn.commit()

def current_poll_interval_seconds(conn: sqlite3.Connection, default_seconds: int, active_seconds: int) -> int:
    row = conn.execute("SELECT active_until FROM agent_status WHERE id = 1").fetchone()
    active_until = row["active_until"] if row else None
    if active_until and datetime.fromisoformat(active_until) > datetime.now(timezone.utc):
        return active_seconds
    return default_seconds
```

`bump_active_interval` is called from the three job-creating endpoints in `main.py` —
`fetch_full`, `send_command`, `send_new_session_command` — right after `db.create_job(...)`. Plain
page loads (`list_chats`, `chat_detail`, `new_session_form`) do **not** bump it, matching the
confirmed trigger: only actions that actually queue agent work extend the active window, and it's
one global window, not per-chat.

### `/jobs/pending` response

```python
@app.get("/jobs/pending", dependencies=[Depends(require_api_key)])
def jobs_pending(conn=Depends(db.get_db_dependency)):
    db.fail_stale_jobs(conn)
    jobs = db.claim_pending_jobs(conn)
    db.record_agent_contact(conn)
    interval = db.current_poll_interval_seconds(
        conn, settings.AI_REMOTE_INTERVAL_SECONDS, settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS
    )
    return {"jobs": jobs, "poll_interval_seconds": interval}
```

### Agent loop

`agent/agent/jobs.py`'s `fetch_pending_jobs` returns the full parsed body instead of just
`["jobs"]`, so `main.py` can read both fields:

```python
def fetch_pending_jobs(base_url, api_key, client) -> dict:
    response = client.get(f"{base_url}/jobs/pending", headers={...})
    response.raise_for_status()
    return response.json()  # {"jobs": [...], "poll_interval_seconds": N}
```

`main.py`'s `run_cycle` returns the interval it learned this cycle (or `None` on failure); `main()`
uses it for the next sleep, falling back to `config.interval_seconds` if the poll itself errored —
so a network hiccup can't wedge the agent into a stuck fast-poll loop or crash it:

```python
def run_cycle(config: Config, client: httpx.Client) -> int | None:
    ...
    try:
        response = jobs.fetch_pending_jobs(config.backend_url, config.api_key, client)
        pending_jobs = response["jobs"]
        next_interval = response["poll_interval_seconds"]
    except httpx.HTTPError as exc:
        print(f"fetch_pending_jobs failed: {exc}", file=sys.stderr)
        pending_jobs = []
        next_interval = None
    ...
    return next_interval

def main() -> None:
    config = load_config()
    with httpx.Client(timeout=10) as client:
        while True:
            try:
                next_interval = run_cycle(config, client)
            except Exception as exc:
                print(f"cycle failed: {exc}", file=sys.stderr)
                next_interval = None
            time.sleep(next_interval if next_interval is not None else config.interval_seconds)
```

### ETA countdown accuracy

`_compute_eta_seconds` (`backend/app/main.py`) currently always estimates against
`settings.AI_REMOTE_INTERVAL_SECONDS`. It switches to the same active-vs-default helper used above,
so a second action fired while already inside an active window shows a ~10s countdown instead of a
misleading up-to-60s one:

```python
interval = db.current_poll_interval_seconds(
    conn, settings.AI_REMOTE_INTERVAL_SECONDS, settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS
)
eta_seconds = _compute_eta_seconds(db.get_last_agent_contact(conn), interval)
```

Applied at all three call sites: `fetch_full`, `send_command`, `send_new_session_command`.

The very first action after a period of inactivity is unaffected — the agent is already mid-sleep
on the old (possibly 60s) interval and can't be woken early; only its *next* cycle onward runs
fast. This matches the requested behavior exactly ("sobald einmal gepollt... automatisch für 5 min
auf Active Interval").

### UI indicator

`base.html`'s header gains a small badge next to the existing pause-toggle, following the same
per-request-computed-context pattern already used for `remote_commands_paused`:

```html
{% if poll_mode is defined %}
<span class="poll-mode-badge poll-mode-{{ poll_mode }}">
  {{ '⚡ Aktiv (10s)' if poll_mode == 'active' else 'Standard (60s)' }}
</span>
{% endif %}
```

Each of the four template-rendering endpoints (`list_chats`, `chat_detail`, `new_session_form`,
`jobs_audit_log`) passes `poll_mode: "active" | "default"` (derived from the same
`db.current_poll_interval_seconds` check) into its template context, mirroring how
`remote_commands_paused` is already independently fetched per-endpoint today. No new shared
context-building mechanism is introduced — that repetition already exists in the codebase and is
out of scope to refactor here.

## Testing

- **Copy button**: no JS test framework exists in this repo (`app.js` has none today) — verified
  manually via `./run.sh`, checking both a rich-text paste target (e.g. a contenteditable/Word) and
  a plain `<input>`.
- **Auto-preload**:
  - `agent/tests/test_claude_code_source.py`: `_parse_session_file` returns `recent_messages`
    capped at N and in original order; a file with fewer than N messages returns all of them.
  - `agent/tests/test_cursor_source.py`: `enrich_with_messages` attaches `recent_messages` from the
    already-loaded bubbles without an extra DB query (assert via call-count on the connection or a
    spy).
  - `backend/tests/test_db.py`: `apply_recent_messages` — first sync populates messages; a shorter
    `recent_messages` than the already-`loaded_message_count` is a no-op; a longer one replaces.
  - `backend/tests/test_auth_and_sync.py`: `POST /sync/index` end-to-end with `recent_messages` in
    the body populates the `messages` table.
- **Adaptive poll interval**:
  - `backend/tests/test_db.py`: `current_poll_interval_seconds` before/during/after an
    `active_until` window.
  - `backend/tests/test_commands.py` / `test_detail_and_jobs.py`: the three job-creating endpoints
    bump `active_until`; `eta_seconds` reflects the active interval once bumped.
  - `agent/tests/test_main_cycle.py`: `run_cycle` returns the server-provided
    `poll_interval_seconds`; on a `fetch_pending_jobs` failure it returns `None` and `main()`'s loop
    falls back to `config.interval_seconds` (covered via a short-lived loop / mocked `time.sleep`).
  - `agent/tests/test_uploader_and_jobs.py`: `fetch_pending_jobs` parses the new response shape.

## Non-Goals

- No change to job execution, allow-listing, or auth.
- No per-chat active-interval scoping — explicitly one global window, as requested.
- No solving the "full history loaded AND still actively growing" edge case in auto-preload (see
  Known Limitation above) — falls back to today's existing manual-reload behavior.
- No new page-size configuration for preload — reuses `CHAT_HISTORY_PAGE_SIZE`.
- No retry/backoff redesign for the agent's HTTP calls — failure handling for the new
  `poll_interval_seconds` field reuses the existing `except httpx.HTTPError` pattern.
