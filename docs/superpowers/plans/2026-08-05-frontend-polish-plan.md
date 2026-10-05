# Frontend Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Modernize the chat viewer's UI (responsive dark/light design), add sorting and a path autocomplete combobox to the chat list, reformat all timestamps as German date/time with a relative suffix, and replace all-at-once history loading with incremental "load more" pagination (plus a separate "load everything" button).

**Architecture:** Extends the existing Plan A backend (FastAPI + SQLite + Jinja2 + vanilla JS, no build step) in place — no new subsystems, no new job type. The `fetch_full` job gains an optional `{"count": N}` payload; the agent's existing in-memory full-message lists are simply sliced to the requested tail length before upload.

**Tech Stack:** Same as Plan A — Python 3.14, FastAPI, SQLite (stdlib `sqlite3`), Jinja2, vanilla JS/CSS, `zoneinfo` (stdlib) for timezone conversion.

## Global Constraints

- Single Mac, single user, personal use only (unchanged from Plan A).
- No JS build step, no external CDN dependencies — self-contained frontend.
- No new backend subsystems, no changes to auth/sync/guardrails, no new job type — `fetch_full`
  is extended via its existing `payload` field only.
- Sorting (`sort` query param) must combine with all existing filters (`tool`, `project`, `group`, `q`).
- Path filtering is substring match ("contains anywhere"), not prefix-only.
- `~/` and `~` in the path filter expand to `LOCAL_HOME_DIR` (env var, default `/Users/yourname`).
- All timestamps render as German date/time in `Europe/Berlin`, with a German relative-time
  suffix, via a single `de_datetime` Jinja2 filter used everywhere a timestamp is shown.
- "Mehr laden" loads `CHAT_HISTORY_PAGE_SIZE` (env var, default 10) additional, older messages
  per click; "Gesamte Historie laden" remains a one-shot full load.
- Reference spec: `docs/superpowers/specs/2026-08-05-frontend-polish-design.md`.

---

## File Structure

```
backend/
  example.env                        # + LOCAL_HOME_DIR, CHAT_HISTORY_PAGE_SIZE
  README.md                          # + new env var docs
  app/
    settings.py                      # + LOCAL_HOME_DIR, CHAT_HISTORY_PAGE_SIZE
    schema.sql                       # + sessions.loaded_message_count column
    db.py                            # + sort param, get_distinct_project_paths, migration,
                                      #   replace_messages updates loaded_message_count
    datetime_filter.py               # NEW — de_datetime Jinja2 filter
    main.py                          # sort/project_paths/~-expansion wiring, count-aware
                                      #   fetch-full route, loaded_message_count-based detail view
    templates/
      base.html                      # + inline SVG favicon/apple-touch-icon
      list.html                      # sort dropdown, path combobox, de_datetime timestamps
      detail.html                    # two-button pagination, de_datetime timestamps
    static/
      style.css                      # full rewrite — CSS custom properties, dark/light, cards
      app.js                         # + combobox behavior, two-button fetch/poll logic
  tests/
    test_settings.py                 # + LOCAL_HOME_DIR / CHAT_HISTORY_PAGE_SIZE tests
    test_db.py                       # + sort/distinct-paths/migration tests, 2 existing tests fixed
    test_datetime_filter.py          # NEW
    test_list.py                     # + sort test
    test_detail_and_jobs.py          # + full=true vs count-based payload test

agent/
  agent/
    executor.py                      # slices messages[-count:] from job payload
  tests/
    test_uploader_and_jobs.py        # + count-slicing tests
```

---

## Task 1: Settings — `LOCAL_HOME_DIR` and `CHAT_HISTORY_PAGE_SIZE`

**Files:**
- Modify: `backend/app/settings.py`
- Modify: `backend/example.env`
- Modify: `backend/README.md`
- Test: `backend/tests/test_settings.py`

**Interfaces:**
- Produces: `settings.LOCAL_HOME_DIR: str` (trailing slash stripped), `settings.CHAT_HISTORY_PAGE_SIZE: int`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_settings.py`:

```python
def test_local_home_dir_default():
    full_env = {**os.environ, "API_KEY": "a" * 20, "SECRET_KEY": "b" * 20}
    full_env.pop("LOCAL_HOME_DIR", None)
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.LOCAL_HOME_DIR)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/Users/yourname"


def test_local_home_dir_strips_trailing_slash():
    full_env = {**os.environ, "API_KEY": "a" * 20, "SECRET_KEY": "b" * 20, "LOCAL_HOME_DIR": "/Users/other/"}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.LOCAL_HOME_DIR)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/Users/other"


def test_chat_history_page_size_default():
    full_env = {**os.environ, "API_KEY": "a" * 20, "SECRET_KEY": "b" * 20}
    full_env.pop("CHAT_HISTORY_PAGE_SIZE", None)
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.CHAT_HISTORY_PAGE_SIZE)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "10"


def test_chat_history_page_size_override():
    full_env = {**os.environ, "API_KEY": "a" * 20, "SECRET_KEY": "b" * 20, "CHAT_HISTORY_PAGE_SIZE": "25"}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.CHAT_HISTORY_PAGE_SIZE)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "25"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_settings.py -v -k "local_home or page_size"`
Expected: FAIL — `AttributeError: module 'app.settings' has no attribute 'LOCAL_HOME_DIR'`

- [ ] **Step 3: Update `backend/app/settings.py`**

```python
import os


def _require_env(name: str, min_length: int = 16) -> str:
    value = os.environ.get(name, "")
    if len(value) < min_length:
        raise RuntimeError(f"{name} must be set to a value at least {min_length} characters long")
    return value


API_KEY = _require_env("API_KEY")
SECRET_KEY = _require_env("SECRET_KEY")
LOCAL_HOME_DIR = os.environ.get("LOCAL_HOME_DIR", "/Users/yourname").rstrip("/")
CHAT_HISTORY_PAGE_SIZE = int(os.environ.get("CHAT_HISTORY_PAGE_SIZE", "10"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_settings.py -v`
Expected: PASS (all tests, existing + new)

- [ ] **Step 5: Document the new env vars**

Append to `backend/example.env`:

```
# Home directory this Mac's user — "~/" and "~" in the chat-list path filter
# expand to this value. Defaults to /Users/yourname.
LOCAL_HOME_DIR=

# How many additional messages "Mehr laden" loads per click on a chat's
# detail page. Defaults to 10.
CHAT_HISTORY_PAGE_SIZE=
```

Append to `backend/README.md`'s "Environment variables" section:

```markdown
- `LOCAL_HOME_DIR` — the Mac's home directory; `~/` and `~` in the chat-list path filter expand
  to this (default `/Users/yourname`).
- `CHAT_HISTORY_PAGE_SIZE` — how many additional messages "Mehr laden" loads per click on a
  chat's detail page (default `10`).
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/settings.py backend/example.env backend/README.md backend/tests/test_settings.py
git commit -m "feat(backend): add LOCAL_HOME_DIR and CHAT_HISTORY_PAGE_SIZE settings"
```

---

## Task 2: DB — sorting and distinct project paths

**Files:**
- Modify: `backend/app/db.py`
- Test: `backend/tests/test_db.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `db.get_sessions(..., sort: str = "date_desc")` (accepts `"date_desc"`, `"date_asc"`,
  `"title_asc"`, `"path_asc"`; unknown values fall back to `"date_desc"`),
  `db.get_distinct_project_paths(conn) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_db.py`:

```python
def test_get_sessions_sort_orders(conn):
    db.upsert_session(
        conn,
        _sample_session("claude-code:b")
        | {"id": "claude-code:b", "title": "Banana", "project_path": "/z/path", "last_updated_at": "2026-08-01T10:00:00Z"},
    )
    db.upsert_session(
        conn,
        _sample_session("claude-code:a")
        | {"id": "claude-code:a", "title": "Apple", "project_path": "/a/path", "last_updated_at": "2026-08-02T10:00:00Z"},
    )

    assert [s["id"] for s in db.get_sessions(conn, sort="date_desc")] == ["claude-code:a", "claude-code:b"]
    assert [s["id"] for s in db.get_sessions(conn, sort="date_asc")] == ["claude-code:b", "claude-code:a"]
    assert [s["id"] for s in db.get_sessions(conn, sort="title_asc")] == ["claude-code:a", "claude-code:b"]
    assert [s["id"] for s in db.get_sessions(conn, sort="path_asc")] == ["claude-code:a", "claude-code:b"]
    assert [s["id"] for s in db.get_sessions(conn, sort="unknown-value")] == ["claude-code:a", "claude-code:b"]


def test_get_distinct_project_paths(conn):
    db.upsert_session(conn, _sample_session("claude-code:a") | {"id": "claude-code:a", "project_path": "/a/path"})
    db.upsert_session(conn, _sample_session("claude-code:b") | {"id": "claude-code:b", "project_path": "/b/path"})
    db.upsert_session(conn, _sample_session("claude-code:c") | {"id": "claude-code:c", "project_path": "/a/path"})

    assert db.get_distinct_project_paths(conn) == ["/a/path", "/b/path"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_db.py -v -k "sort_orders or distinct_project_paths"`
Expected: FAIL — `TypeError: get_sessions() got an unexpected keyword argument 'sort'` /
`AttributeError: module 'app.db' has no attribute 'get_distinct_project_paths'`

- [ ] **Step 3: Update `get_sessions` in `backend/app/db.py`**

Add this module-level constant near the top of the file (after the imports):

```python
_SORT_CLAUSES = {
    "date_desc": "last_updated_at DESC",
    "date_asc": "last_updated_at ASC",
    "title_asc": "title COLLATE NOCASE ASC",
    "path_asc": "project_path COLLATE NOCASE ASC",
}
```

Change `get_sessions`'s signature and its `ORDER BY` line:

```python
def get_sessions(
    conn: sqlite3.Connection,
    tool: str | None = None,
    project: str | None = None,
    date_group: str | None = None,
    q: str | None = None,
    limit: int = 100,
    sort: str = "date_desc",
) -> list[dict]:
```

Replace the existing `query += " ORDER BY last_updated_at DESC"` line with:

```python
    query += f" ORDER BY {_SORT_CLAUSES.get(sort, _SORT_CLAUSES['date_desc'])}"
```

- [ ] **Step 4: Add `get_distinct_project_paths`**

Add this function to `backend/app/db.py`, right after `get_distinct_project_paths`'s natural
neighbor `get_sessions` (or anywhere else at module level — placement doesn't matter):

```python
def get_distinct_project_paths(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT project_path FROM sessions WHERE project_path != '' ORDER BY project_path"
    ).fetchall()
    return [row["project_path"] for row in rows]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_db.py -v`
Expected: PASS (all tests, existing + new)

- [ ] **Step 6: Commit**

```bash
git add backend/app/db.py backend/tests/test_db.py
git commit -m "feat(backend): sortable session queries and distinct project paths"
```

---

## Task 3: DB — `loaded_message_count` schema migration and pagination-aware `replace_messages`

**Files:**
- Modify: `backend/app/schema.sql`
- Modify: `backend/app/db.py`
- Test: `backend/tests/test_db.py`

**Interfaces:**
- Produces: `sessions.loaded_message_count` column (always present after `init_db`, including on
  pre-existing databases). `replace_messages` now sets `loaded_message_count = len(messages)` and
  derives `full_content_synced` from it, instead of always setting `full_content_synced = 1`.

This changes the meaning of "fully synced": it's now `loaded_message_count >= message_count`,
not just "a fetch happened at all." Two existing tests in `backend/tests/test_db.py` assert
`full_content_synced == 1` after calling `replace_messages` with only 1 message, while the shared
`_sample_session()` fixture has `message_count: 2` — under the new logic that would be
`loaded_message_count(1) < message_count(2)`, i.e. `full_content_synced == 0`, breaking those
assertions. Both are fixed in Step 5 below by loading 2 messages (matching the fixture) instead
of 1 — this preserves each test's original intent ("a full sync marks the session as fully
synced") under the corrected semantics.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_db.py`:

```python
def test_replace_messages_sets_loaded_count_and_partial_sync_flag(conn):
    db.upsert_session(conn, _sample_session() | {"message_count": 5})
    db.replace_messages(
        conn,
        "claude-code:abc",
        [
            {"idx": 3, "role": "user", "timestamp": "2026-08-01T10:03:00Z", "content": "d"},
            {"idx": 4, "role": "assistant", "timestamp": "2026-08-01T10:04:00Z", "content": "e"},
        ],
    )
    session = db.get_session(conn, "claude-code:abc")
    assert session["loaded_message_count"] == 2
    assert session["full_content_synced"] == 0  # 2 loaded < 5 total

    db.replace_messages(
        conn,
        "claude-code:abc",
        [
            {"idx": i, "role": "user", "timestamp": f"2026-08-01T10:0{i}:00Z", "content": str(i)}
            for i in range(5)
        ],
    )
    session = db.get_session(conn, "claude-code:abc")
    assert session["loaded_message_count"] == 5
    assert session["full_content_synced"] == 1  # 5 loaded >= 5 total


def test_migration_adds_loaded_message_count_to_pre_existing_db_and_backfills(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    legacy_conn = db.get_connection(db_path)
    legacy_conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            tool TEXT NOT NULL,
            entrypoint TEXT NOT NULL DEFAULT '',
            project_path TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_updated_at TEXT NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            last_message_preview TEXT NOT NULL DEFAULT '',
            full_content_synced INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'idle'
        )
        """
    )
    legacy_conn.execute(
        "INSERT INTO sessions (id, tool, title, created_at, last_updated_at, message_count, full_content_synced) "
        "VALUES ('claude-code:legacy', 'claude-code', 'Old', 't', 't', 7, 1)"
    )
    legacy_conn.commit()
    legacy_conn.close()

    migrated_conn = db.get_connection(db_path)
    db.init_db(migrated_conn)  # must not fail on a table that already exists without the new column

    columns = {row["name"] for row in migrated_conn.execute("PRAGMA table_info(sessions)").fetchall()}
    assert "loaded_message_count" in columns

    session = db.get_session(migrated_conn, "claude-code:legacy")
    assert session["loaded_message_count"] == 7  # backfilled from message_count since it was fully synced
    migrated_conn.close()


def test_partially_loaded_messages_are_searchable_immediately(conn):
    db.upsert_session(conn, _sample_session() | {"message_count": 5})
    db.replace_messages(
        conn,
        "claude-code:abc",
        [{"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": "zebra-unique-word"}],
    )
    session = db.get_session(conn, "claude-code:abc")
    assert session["full_content_synced"] == 0  # only a partial load, not the full 5 messages

    assert [s["id"] for s in db.get_sessions(conn, q="zebra-unique-word")] == ["claude-code:abc"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_db.py -v -k "loaded_count or migration or partially_loaded"`
Expected: FAIL — `sqlite3.OperationalError: no such column: loaded_message_count`

- [ ] **Step 3: Add the column to `backend/app/schema.sql`**

In the `sessions` table definition, add a new line right after `full_content_synced`:

```sql
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    tool TEXT NOT NULL CHECK(tool IN ('claude-code', 'cursor')),
    entrypoint TEXT NOT NULL DEFAULT '',
    project_path TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_updated_at TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    last_message_preview TEXT NOT NULL DEFAULT '',
    full_content_synced INTEGER NOT NULL DEFAULT 0,
    loaded_message_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'idle'
);
```

(Leave every other table in the file unchanged.)

- [ ] **Step 4: Add the migration and update `replace_messages` in `backend/app/db.py`**

Change `init_db` to run the migration after applying the schema:

```python
def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    _migrate_add_loaded_message_count(conn)


def _migrate_add_loaded_message_count(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "loaded_message_count" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN loaded_message_count INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "UPDATE sessions SET loaded_message_count = message_count WHERE full_content_synced = 1"
        )
        conn.commit()
```

Replace `replace_messages`'s final `UPDATE sessions` line:

```python
def replace_messages(conn: sqlite3.Connection, session_id: str, messages: list[dict]) -> None:
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM search_index WHERE session_id = ? AND kind = 'message'", (session_id,))
    for m in messages:
        conn.execute(
            "INSERT INTO messages (session_id, idx, role, timestamp, content) VALUES (?, ?, ?, ?, ?)",
            (session_id, m["idx"], m["role"], m["timestamp"], m["content"]),
        )
        conn.execute(
            "INSERT INTO search_index (session_id, kind, text) VALUES (?, 'message', ?)",
            (session_id, m["content"]),
        )
    conn.execute(
        "UPDATE sessions SET loaded_message_count = ?, "
        "full_content_synced = CASE WHEN ? >= message_count THEN 1 ELSE 0 END "
        "WHERE id = ?",
        (len(messages), len(messages), session_id),
    )
    conn.commit()
```

- [ ] **Step 5: Fix the two existing tests whose fixtures no longer match the corrected semantics**

In `backend/tests/test_db.py`, `test_job_lifecycle_and_message_replacement` currently completes a
job with a single message. Change its `messages` list to:

```python
    messages = [
        {"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": "hi"},
        {"idx": 1, "role": "assistant", "timestamp": "2026-08-01T10:01:00Z", "content": "hello"},
    ]
```

(`_sample_session()`'s `message_count` is 2, so loading 2 messages now correctly yields
`full_content_synced == 1`, matching the test's existing assertion — no other change needed in
that test.)

Similarly, in `test_upsert_session_resets_full_content_synced_when_last_updated_at_changes`,
change the `db.replace_messages(...)` call's message list from one message to:

```python
        [
            {"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": "hi"},
            {"idx": 1, "role": "assistant", "timestamp": "2026-08-01T10:01:00Z", "content": "hello"},
        ],
```

(Same reasoning — the rest of that test is unchanged.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_db.py -v`
Expected: PASS (all tests, existing + new)

- [ ] **Step 7: Commit**

```bash
git add backend/app/schema.sql backend/app/db.py backend/tests/test_db.py
git commit -m "feat(backend): track loaded_message_count for incremental history pagination"
```

---

## Task 4: `de_datetime` Jinja2 filter

**Files:**
- Create: `backend/app/datetime_filter.py`
- Test: `backend/tests/test_datetime_filter.py`

**Interfaces:**
- Produces: `format_de_datetime(iso_timestamp: str | None, now: datetime | None = None) -> str`.
  `now` is an injectable reference time for deterministic testing; production call sites (the
  Jinja2 filter registration) never pass it, so it defaults to the real current time.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_datetime_filter.py`:

```python
from datetime import datetime, timezone

from app.datetime_filter import BERLIN_TZ, format_de_datetime

_WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def test_none_and_empty_input_render_placeholder():
    assert format_de_datetime(None) == "unbekannt"
    assert format_de_datetime("") == "unbekannt"


def test_invalid_input_renders_placeholder_without_raising():
    assert format_de_datetime("not-a-timestamp") == "unbekannt"


def test_under_a_minute_says_gerade_eben():
    now = datetime(2026, 8, 5, 10, 50, 30, tzinfo=timezone.utc)
    result = format_de_datetime("2026-08-05T10:50:00+00:00", now=now)
    assert "(gerade eben)" in result


def test_minutes_bucket_singular_and_plural():
    now = datetime(2026, 8, 5, 10, 50, tzinfo=timezone.utc)
    assert "(vor 1 Minute)" in format_de_datetime("2026-08-05T10:49:00+00:00", now=now)
    assert "(vor 6 Minuten)" in format_de_datetime("2026-08-05T10:44:00+00:00", now=now)


def test_hours_bucket_singular_and_plural():
    now = datetime(2026, 8, 5, 10, 50, tzinfo=timezone.utc)
    assert "(vor 1 Stunde)" in format_de_datetime("2026-08-05T09:50:00+00:00", now=now)
    assert "(vor 3 Stunden)" in format_de_datetime("2026-08-05T07:50:00+00:00", now=now)


def test_days_bucket_and_same_year_date_format_omits_year():
    now = datetime(2026, 8, 5, 10, 50, tzinfo=timezone.utc)
    event = datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc)
    result = format_de_datetime(event.isoformat(), now=now)
    expected_weekday = _WEEKDAYS_DE[event.astimezone(BERLIN_TZ).weekday()]
    assert result.startswith(f"{expected_weekday}, 30.07. ")
    assert "(vor 6 Tagen)" in result


def test_weeks_bucket():
    now = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)
    event = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)  # 21 days = 3 weeks
    result = format_de_datetime(event.isoformat(), now=now)
    assert "(vor 3 Wochen)" in result


def test_months_bucket():
    now = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)
    event = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)  # ~96 days = 3 months at //30
    result = format_de_datetime(event.isoformat(), now=now)
    assert "Monat" in result  # exact count is an approximation; just prove the bucket is right


def test_different_year_includes_year_in_date_and_years_bucket():
    now = datetime(2026, 8, 5, 10, 50, tzinfo=timezone.utc)
    event = datetime(2025, 8, 5, 10, 50, tzinfo=timezone.utc)
    result = format_de_datetime(event.isoformat(), now=now)
    expected_weekday = _WEEKDAYS_DE[event.astimezone(BERLIN_TZ).weekday()]
    assert result.startswith(f"{expected_weekday}, 05.08.2025 ")
    assert "(vor 1 Jahr)" in result


def test_berlin_timezone_conversion_shifts_utc_to_local_summer_time():
    # 22:30 UTC in August is CEST (UTC+2) -> 00:30 the next local day.
    now = datetime(2026, 8, 6, 1, 0, tzinfo=timezone.utc)
    result = format_de_datetime("2026-08-05T22:30:00+00:00", now=now)
    assert "00:30" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_datetime_filter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.datetime_filter'`

- [ ] **Step 3: Write `backend/app/datetime_filter.py`**

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BERLIN_TZ = ZoneInfo("Europe/Berlin")
_WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def format_de_datetime(iso_timestamp: str | None, now: datetime | None = None) -> str:
    if not iso_timestamp:
        return "unbekannt"
    try:
        moment = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return "unbekannt"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    reference = now if now is not None else datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    local_moment = moment.astimezone(BERLIN_TZ)
    local_reference = reference.astimezone(BERLIN_TZ)

    weekday = _WEEKDAYS_DE[local_moment.weekday()]
    if local_moment.year == local_reference.year:
        date_part = f"{local_moment.day:02d}.{local_moment.month:02d}."
    else:
        date_part = f"{local_moment.day:02d}.{local_moment.month:02d}.{local_moment.year}"
    time_part = f"{local_moment.hour:02d}:{local_moment.minute:02d}"

    relative = _format_relative(reference - moment)
    return f"{weekday}, {date_part} {time_part} ({relative})"


def _format_relative(delta) -> str:
    seconds = delta.total_seconds()
    if seconds < 60:
        return "gerade eben"
    minutes = int(seconds // 60)
    if minutes < 60:
        return "vor 1 Minute" if minutes == 1 else f"vor {minutes} Minuten"
    hours = minutes // 60
    if hours < 24:
        return "vor 1 Stunde" if hours == 1 else f"vor {hours} Stunden"
    days = hours // 24
    if days < 7:
        return "vor 1 Tag" if days == 1 else f"vor {days} Tagen"
    if days < 31:
        weeks = days // 7
        return "vor 1 Woche" if weeks == 1 else f"vor {weeks} Wochen"
    if days < 365:
        months = days // 30
        return "vor 1 Monat" if months == 1 else f"vor {months} Monaten"
    years = days // 365
    return "vor 1 Jahr" if years == 1 else f"vor {years} Jahren"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_datetime_filter.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/datetime_filter.py backend/tests/test_datetime_filter.py
git commit -m "feat(backend): German date/time + relative-time Jinja2 filter"
```

---

## Task 5: Agent — slice messages to the requested page count

**Files:**
- Modify: `agent/agent/executor.py`
- Test: `agent/tests/test_uploader_and_jobs.py`

**Interfaces:**
- Consumes: nothing new — same `claude_code_source.get_full_messages` /
  `cursor_source.get_full_messages` as before.
- Produces: `execute_fetch_full(job)` now reads an optional `{"count": N}` from `job["payload"]`
  and returns only the last `N` messages when present; unchanged (returns everything) when
  `payload` is empty, missing, or doesn't parse as JSON with an integer `count`.

- [ ] **Step 1: Write the failing tests**

Append to `agent/tests/test_uploader_and_jobs.py`:

```python
def test_execute_fetch_full_slices_to_requested_count(monkeypatch):
    monkeypatch.setattr(
        "agent.claude_code_source.get_full_messages",
        lambda raw_id: [
            {"idx": 0, "role": "user", "timestamp": "t0", "content": "one"},
            {"idx": 1, "role": "assistant", "timestamp": "t1", "content": "two"},
            {"idx": 2, "role": "user", "timestamp": "t2", "content": "three"},
        ],
    )
    job = {"id": "j1", "type": "fetch_full", "target": "claude-code:abc", "payload": '{"count": 2}'}
    result = executor.execute_fetch_full(job)
    assert result["status"] == "done"
    assert [m["content"] for m in result["messages"]] == ["two", "three"]


def test_execute_fetch_full_returns_everything_when_payload_empty(monkeypatch):
    monkeypatch.setattr(
        "agent.claude_code_source.get_full_messages",
        lambda raw_id: [{"idx": 0, "role": "user", "timestamp": "t0", "content": "one"}],
    )
    job = {"id": "j1", "type": "fetch_full", "target": "claude-code:abc", "payload": ""}
    result = executor.execute_fetch_full(job)
    assert [m["content"] for m in result["messages"]] == ["one"]


def test_execute_fetch_full_count_larger_than_available_returns_everything(monkeypatch):
    monkeypatch.setattr(
        "agent.claude_code_source.get_full_messages",
        lambda raw_id: [{"idx": 0, "role": "user", "timestamp": "t0", "content": "only-one"}],
    )
    job = {"id": "j1", "type": "fetch_full", "target": "claude-code:abc", "payload": '{"count": 50}'}
    result = executor.execute_fetch_full(job)
    assert [m["content"] for m in result["messages"]] == ["only-one"]


def test_execute_fetch_full_malformed_payload_falls_back_to_everything(monkeypatch):
    monkeypatch.setattr(
        "agent.claude_code_source.get_full_messages",
        lambda raw_id: [{"idx": 0, "role": "user", "timestamp": "t0", "content": "one"}],
    )
    job = {"id": "j1", "type": "fetch_full", "target": "claude-code:abc", "payload": "not-json"}
    result = executor.execute_fetch_full(job)
    assert [m["content"] for m in result["messages"]] == ["one"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd agent && .venv/bin/pytest tests/test_uploader_and_jobs.py -v -k "slices or empty_payload or larger_than or malformed_payload"`
Expected: FAIL — `test_execute_fetch_full_slices_to_requested_count` gets all 3 messages instead
of the last 2 (payload is currently ignored entirely)

- [ ] **Step 3: Update `agent/agent/executor.py`**

```python
import json

from . import claude_code_source, cursor_source


def execute_fetch_full(job: dict) -> dict:
    parts = job["target"].split(":", 1)
    if len(parts) != 2:
        return {"status": "failed", "result_text": f"malformed job target: {job['target']!r}", "messages": []}
    tool, raw_id = parts
    if tool == "claude-code":
        messages = claude_code_source.get_full_messages(raw_id)
    elif tool == "cursor":
        messages = cursor_source.get_full_messages(raw_id)
    else:
        return {"status": "failed", "result_text": f"unknown tool: {tool}", "messages": []}

    if not messages:
        return {"status": "failed", "result_text": "no messages found for session", "messages": []}

    count = _extract_count(job.get("payload", ""))
    if count is not None and count > 0:
        messages = messages[-count:]

    return {"status": "done", "result_text": "", "messages": messages}


def _extract_count(payload: str) -> int | None:
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    count = data.get("count")
    return count if isinstance(count, int) else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd agent && .venv/bin/pytest tests/ -v`
Expected: PASS (all tests, existing + new)

- [ ] **Step 5: Commit**

```bash
git add agent/agent/executor.py agent/tests/test_uploader_and_jobs.py
git commit -m "feat(agent): slice fetch_full results to a requested message count"
```

---

## Task 6: Backend routes — sort, path filter (`~` expansion), and count-aware fetch-full

**Files:**
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_list.py`
- Test: `backend/tests/test_detail_and_jobs.py`

**Interfaces:**
- Consumes: `db.get_sessions(..., sort=...)`, `db.get_distinct_project_paths`,
  `settings.LOCAL_HOME_DIR`, `settings.CHAT_HISTORY_PAGE_SIZE`, `datetime_filter.format_de_datetime`
  (Tasks 1–4).
- Produces: `GET /` accepts `?sort=`; template context gains `sort`, `project_paths`,
  `local_home_dir`; `POST /chats/{id}/fetch-full` accepts `?full=true` for a one-shot full load
  (unchanged behavior) vs. the default (creates a `{"count": loaded_message_count + CHAT_HISTORY_PAGE_SIZE}`
  job); `GET /chats/{id}` passes `messages` whenever `loaded_message_count > 0`, not only when
  `full_content_synced`; `_format_last_contact` is removed (the template applies `de_datetime`
  directly to the raw timestamp instead); `_expand_home_dir(value: str) -> str` is a new
  module-level helper.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_list.py`:

```python
def test_list_page_sort_by_title(logged_in_client):
    for title, ts in [("Banana chat", "2026-08-01T10:00:00Z"), ("Apple chat", "2026-08-02T10:00:00Z")]:
        session = {
            "id": f"claude-code:{title}",
            "tool": "claude-code",
            "entrypoint": "cli",
            "project_path": "/Users/jan/source/demo",
            "title": title,
            "created_at": ts,
            "last_updated_at": ts,
            "message_count": 1,
            "last_message_preview": "hi",
            "status": "idle",
        }
        logged_in_client.post(
            "/sync/index", json={"sessions": [session]}, headers={"Authorization": "Bearer test-api-key-1234"}
        )

    response = logged_in_client.get("/", params={"sort": "title_asc"})
    assert response.text.index("Apple chat") < response.text.index("Banana chat")


def test_list_page_embeds_distinct_project_paths_as_json(logged_in_client):
    session = {
        "id": "claude-code:abc",
        "tool": "claude-code",
        "entrypoint": "cli",
        "project_path": "/Users/jan/source/demo",
        "title": "Demo",
        "created_at": "2026-08-01T10:00:00Z",
        "last_updated_at": "2026-08-01T10:05:00Z",
        "message_count": 1,
        "last_message_preview": "hi",
        "status": "idle",
    }
    logged_in_client.post(
        "/sync/index", json={"sessions": [session]}, headers={"Authorization": "Bearer test-api-key-1234"}
    )
    response = logged_in_client.get("/")
    assert "/Users/jan/source/demo" in response.text


def test_expand_home_dir_maps_tilde_to_local_home_dir():
    from app.main import _expand_home_dir
    from app import settings

    assert _expand_home_dir("~/source/demo") == f"{settings.LOCAL_HOME_DIR}/source/demo"
    assert _expand_home_dir("~") == settings.LOCAL_HOME_DIR
    assert _expand_home_dir("/already/absolute") == "/already/absolute"


def test_project_filter_matches_via_home_dir_expansion(logged_in_client, monkeypatch):
    from app import settings

    session = {
        "id": "claude-code:home",
        "tool": "claude-code",
        "entrypoint": "cli",
        "project_path": f"{settings.LOCAL_HOME_DIR}/source/demo",
        "title": "Home dir session",
        "created_at": "2026-08-01T10:00:00Z",
        "last_updated_at": "2026-08-01T10:05:00Z",
        "message_count": 1,
        "last_message_preview": "hi",
        "status": "idle",
    }
    logged_in_client.post(
        "/sync/index", json={"sessions": [session]}, headers={"Authorization": "Bearer test-api-key-1234"}
    )
    response = logged_in_client.get("/", params={"project": "~/source"})
    assert "Home dir session" in response.text
```

Append to `backend/tests/test_detail_and_jobs.py`:

```python
def test_fetch_full_default_creates_count_based_payload_job(logged_in_client):
    _sync_one(logged_in_client)

    enqueue = logged_in_client.post("/chats/claude-code:abc/fetch-full")
    job_id = enqueue.json()["job_id"]

    pending = logged_in_client.get(
        "/jobs/pending", headers={"Authorization": "Bearer test-api-key-1234"}
    ).json()["jobs"]
    assert pending[0]["id"] == job_id
    import json as json_module

    payload = json_module.loads(pending[0]["payload"])
    assert payload == {"count": 10}  # loaded_message_count(0) + default CHAT_HISTORY_PAGE_SIZE(10)


def test_fetch_full_with_full_true_creates_empty_payload_job(logged_in_client):
    _sync_one(logged_in_client)

    enqueue = logged_in_client.post("/chats/claude-code:abc/fetch-full", params={"full": "true"})
    job_id = enqueue.json()["job_id"]

    pending = logged_in_client.get(
        "/jobs/pending", headers={"Authorization": "Bearer test-api-key-1234"}
    ).json()["jobs"]
    assert pending[0]["id"] == job_id
    assert pending[0]["payload"] == ""
```

Note: this task deliberately does NOT add a test for the detail page's `#load-more`/`#load-all`
button markup — that markup doesn't exist until Task 9 rewrites `detail.html`. Adding such a test
here would leave it failing at the end of this task. Task 9 introduces it instead.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_list.py tests/test_detail_and_jobs.py -v`
Expected: FAIL — `_expand_home_dir` doesn't exist yet; `/chats/{id}/fetch-full` always creates an
empty-payload job regardless of `?full=`

- [ ] **Step 3: Rewrite `backend/app/main.py`**

Replace the file's full contents with:

```python
import json
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db, settings
from .auth import require_api_key, require_session
from .datetime_filter import format_de_datetime
from .markdown_filter import render_markdown
from .models import JobCompleteRequest, SyncIndexRequest

APP_DIR = Path(__file__).parent

app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
    https_only=os.environ.get("SESSION_COOKIE_HTTPS_ONLY", "true").lower() == "true",
)

templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.filters["markdown"] = render_markdown
templates.env.filters["de_datetime"] = format_de_datetime
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


@app.on_event("startup")
def on_startup() -> None:
    conn = db.get_connection(os.environ.get("DATABASE_PATH", "app.db"))
    db.init_db(conn)
    conn.close()


@app.get("/jobs/pending", dependencies=[Depends(require_api_key)])
def jobs_pending(conn=Depends(db.get_db_dependency)):
    db.fail_stale_jobs(conn)
    jobs = db.claim_pending_jobs(conn)
    db.record_agent_contact(conn)
    return {"jobs": jobs}


@app.post("/jobs/{job_id}/complete", dependencies=[Depends(require_api_key)])
def jobs_complete(job_id: str, body: JobCompleteRequest, conn=Depends(db.get_db_dependency)):
    messages = [m.model_dump() for m in body.messages]
    db.complete_job(conn, job_id, body.status, body.result_text, messages)
    return {"ok": True}


@app.post("/chats/{session_id}/fetch-full", dependencies=[Depends(require_session)])
def fetch_full(session_id: str, full: bool = False, conn=Depends(db.get_db_dependency)):
    if full:
        job_id = db.create_job(conn, "fetch_full", session_id)
    else:
        session = db.get_session(conn, session_id)
        current = session["loaded_message_count"] if session else 0
        next_count = current + settings.CHAT_HISTORY_PAGE_SIZE
        job_id = db.create_job(conn, "fetch_full", session_id, payload=json.dumps({"count": next_count}))
    return {"job_id": job_id}


@app.get("/chats/{session_id}/status", dependencies=[Depends(require_session)])
def job_status(session_id: str, job_id: str, conn=Depends(db.get_db_dependency)):
    job = db.get_job(conn, job_id)
    return {"status": job["status"] if job else "unknown"}


@app.post("/sync/index", dependencies=[Depends(require_api_key)])
def sync_index(body: SyncIndexRequest, conn=Depends(db.get_db_dependency)):
    for session in body.sessions:
        db.upsert_session(conn, session.model_dump())
    db.record_agent_contact(conn)
    return {"received": len(body.sessions)}


@app.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login_submit(request: Request, api_key: str = Form(...)):
    if api_key == settings.API_KEY:
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": "Invalid key"}, status_code=401
    )


def _expand_home_dir(value: str) -> str:
    if value == "~":
        return settings.LOCAL_HOME_DIR
    if value.startswith("~/"):
        return settings.LOCAL_HOME_DIR + "/" + value[2:]
    return value


@app.get("/", dependencies=[Depends(require_session)])
def list_chats(
    request: Request,
    tool: str | None = None,
    project: str | None = None,
    group: str | None = None,
    q: str | None = None,
    sort: str = "date_desc",
    limit: int = 100,
    conn=Depends(db.get_db_dependency),
):
    expanded_project = _expand_home_dir(project) if project else project
    sessions = db.get_sessions(
        conn, tool=tool, project=expanded_project, date_group=group, q=q, limit=limit, sort=sort
    )
    return templates.TemplateResponse(
        request,
        "list.html",
        {
            "sessions": sessions,
            "tool": tool,
            "project": project,
            "group": group,
            "q": q,
            "sort": sort,
            "last_agent_contact": db.get_last_agent_contact(conn),
            "project_paths": db.get_distinct_project_paths(conn),
            "local_home_dir": settings.LOCAL_HOME_DIR,
        },
    )


@app.get("/chats/{session_id}", dependencies=[Depends(require_session)])
def chat_detail(request: Request, session_id: str, conn=Depends(db.get_db_dependency)):
    session = db.get_session(conn, session_id)
    if session is None:
        return templates.TemplateResponse(
            request, "detail.html", {"session": None, "messages": []}, status_code=404
        )
    messages = db.get_messages(conn, session_id) if session["loaded_message_count"] > 0 else []
    return templates.TemplateResponse(request, "detail.html", {"session": session, "messages": messages})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/ -v`
Expected: PASS (all tests, existing + new)

- [ ] **Step 5: Commit**

```bash
git add backend/app/main.py backend/tests/test_list.py backend/tests/test_detail_and_jobs.py
git commit -m "feat(backend): sort/path-filter/home-dir-expansion and count-aware fetch-full route"
```

---

## Task 7: Visual redesign — CSS custom properties, dark/light theming, card layout

**Files:**
- Modify: `backend/app/static/style.css`
- Modify: `backend/app/templates/base.html`

**Interfaces:**
- Produces: CSS classes consumed by Tasks 8–9's templates: `.toolbar`, `.combobox`,
  `.combobox-listbox`, `.tool-badge-claude-code`, `.tool-badge-cursor`, `.history-actions`, plus
  the existing `.session-list`, `.staleness`, `.messages`, `.message`, `.message-user`, `.error`
  classes restyled in place.

- [ ] **Step 1: Rewrite `backend/app/static/style.css`**

```css
:root {
  --bg: #0f1115;
  --surface: #1a1d23;
  --surface-hover: #21252c;
  --text: #eaecef;
  --text-muted: #9aa0a8;
  --border: #2a2e35;
  --accent: #5b9dff;
  --accent-claude: #d97757;
  --accent-cursor: #7c6cf0;
  --radius: 10px;
  --shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
}

@media (prefers-color-scheme: light) {
  :root {
    --bg: #f5f6f8;
    --surface: #ffffff;
    --surface-hover: #f0f1f3;
    --text: #1a1d23;
    --text-muted: #6b7280;
    --border: #e2e4e8;
    --accent: #2563eb;
    --shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
  }
}

* { box-sizing: border-box; }

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  margin: 0;
  background: var(--bg);
  color: var(--text);
  -webkit-tap-highlight-color: transparent;
}

header {
  padding: 1rem 1.25rem;
  font-weight: 600;
  font-size: 1.1rem;
  border-bottom: 1px solid var(--border);
}
header a { color: var(--text); text-decoration: none; }

main {
  padding: 1rem 1rem 3rem;
  max-width: 720px;
  margin: 0 auto;
}

@media (min-width: 800px) {
  main { max-width: 800px; padding: 1.5rem 1.5rem 3rem; }
}

.staleness {
  font-size: 0.8rem;
  color: var(--text-muted);
  margin: 0 0 1rem;
}

.toolbar {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  align-items: center;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 0.75rem;
  margin-bottom: 1.25rem;
  box-shadow: var(--shadow);
}

input, select, button {
  padding: 0.6rem 0.75rem;
  min-height: 44px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--text);
  font-size: 0.95rem;
}

button {
  background: var(--accent);
  color: #fff;
  border: none;
  font-weight: 600;
  cursor: pointer;
}
button:hover { opacity: 0.9; }
button:disabled { opacity: 0.5; cursor: default; }

.combobox { position: relative; flex: 1 1 200px; }
.combobox input { width: 100%; }
.combobox-listbox {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  z-index: 10;
  list-style: none;
  margin: 0;
  padding: 4px;
  max-height: 240px;
  overflow-y: auto;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow);
}
.combobox-listbox li {
  padding: 0.5rem 0.6rem;
  border-radius: 6px;
  cursor: pointer;
  font-size: 0.85rem;
  word-break: break-all;
}
.combobox-listbox li:hover,
.combobox-listbox li.active { background: var(--surface-hover); }

.session-list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 0.6rem; }
.session-list li {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  transition: background 0.15s;
}
.session-list li:hover { background: var(--surface-hover); }
.session-list a { color: inherit; text-decoration: none; display: block; padding: 0.9rem 1rem; }
.session-list strong { display: block; font-size: 1rem; margin-bottom: 0.25rem; }
.session-list p { margin: 0.4rem 0; color: var(--text-muted); font-size: 0.85rem; }

.tool-badge {
  display: inline-block;
  font-size: 0.7rem;
  font-weight: 600;
  padding: 0.15rem 0.5rem;
  border-radius: 999px;
  margin-left: 0.5rem;
  color: #fff;
  vertical-align: middle;
}
.tool-badge-claude-code { background: var(--accent-claude); }
.tool-badge-cursor { background: var(--accent-cursor); }

.project, time { display: block; font-size: 0.75rem; color: var(--text-muted); }
.error { color: #ef4444; }

.messages { display: flex; flex-direction: column; gap: 0.75rem; }
.message {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 0.75rem 1rem;
}
.message-user { border-left: 3px solid var(--accent); }
.message time { margin-bottom: 0.35rem; }

.history-actions { display: flex; gap: 0.5rem; align-items: center; margin-top: 1rem; flex-wrap: wrap; }
```

- [ ] **Step 2: Add an inline SVG favicon/apple-touch-icon to `backend/app/templates/base.html`**

```html
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}AI Remote Chats{% endblock %}</title>
  <link rel="manifest" href="/static/manifest.json">
  <link rel="stylesheet" href="/static/style.css">
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='22' fill='%235b9dff'/><text x='50' y='66' font-size='54' text-anchor='middle' fill='white' font-family='-apple-system,sans-serif'>AI</text></svg>">
  <link rel="apple-touch-icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='22' fill='%235b9dff'/><text x='50' y='66' font-size='54' text-anchor='middle' fill='white' font-family='-apple-system,sans-serif'>AI</text></svg>">
</head>
<body>
  <header><a href="/">AI Remote Chats</a></header>
  <main>{% block content %}{% endblock %}</main>
  <script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 3: Manually verify rendering**

Run: `cd backend && DATABASE_PATH=/tmp/style-check.db API_KEY=devkeydevkeydevkey1 SECRET_KEY=devsecretdevsecret1 SESSION_COOKIE_HTTPS_ONLY=false .venv/bin/uvicorn app.main:app --reload`
Then open `http://localhost:8000/login` in a browser, log in, and confirm: the page uses the new
color scheme, respects the OS light/dark setting when toggled, the tab shows the new icon, and
nothing is visually broken (existing `list.html`/`detail.html` markup from before Tasks 8–9 still
renders fine against the new CSS — the class names it already uses, like `.session-list` and
`.staleness`, are unchanged).

- [ ] **Step 4: Commit**

```bash
git add backend/app/static/style.css backend/app/templates/base.html
git commit -m "feat(backend): modern responsive dark/light visual redesign"
```

---

## Task 8: `list.html` — sort dropdown, path combobox, card list, German timestamps

**Files:**
- Modify: `backend/app/templates/list.html`
- Test: `backend/tests/test_list.py`

**Interfaces:**
- Consumes: `sort`, `project_paths`, `local_home_dir` template context (Task 6);
  `de_datetime` filter (Task 4); `.toolbar`/`.combobox`/`.combobox-listbox`/`.tool-badge-*` CSS
  classes (Task 7).
- Produces: a `#project-input`/`#project-listbox` combobox and a `#project-paths-data` JSON
  `<script>` tag that Task 10's `app.js` reads.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_list.py`:

```python
def test_list_page_shows_sort_dropdown_and_combobox_markup(logged_in_client):
    response = logged_in_client.get("/")
    assert 'name="sort"' in response.text
    assert 'id="project-input"' in response.text
    assert 'id="project-listbox"' in response.text
    assert 'id="project-paths-data"' in response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/test_list.py -v -k combobox_markup`
Expected: FAIL — none of those ids/names exist in the current template

- [ ] **Step 3: Rewrite `backend/app/templates/list.html`**

```html
{% extends "base.html" %}
{% block content %}
<p class="staleness">
  {% if last_agent_contact %}Mac zuletzt erreichbar: {{ last_agent_contact | de_datetime }}
  {% else %}Mac noch nie erreichbar gewesen.{% endif %}
</p>
<form method="get" action="/" class="toolbar">
  <input type="text" name="q" value="{{ q or '' }}" placeholder="Suche...">
  <div class="combobox">
    <input type="text" id="project-input" name="project" value="{{ project or '' }}"
           placeholder="Pfad..." autocomplete="off" role="combobox"
           aria-expanded="false" aria-autocomplete="list" aria-controls="project-listbox">
    <ul id="project-listbox" class="combobox-listbox" role="listbox" hidden></ul>
  </div>
  <select name="tool">
    <option value="">Alle Tools</option>
    <option value="claude-code" {% if tool == "claude-code" %}selected{% endif %}>Claude Code</option>
    <option value="cursor" {% if tool == "cursor" %}selected{% endif %}>Cursor</option>
  </select>
  <select name="group">
    <option value="">Alle</option>
    <option value="today" {% if group == "today" %}selected{% endif %}>Heute</option>
    <option value="yesterday" {% if group == "yesterday" %}selected{% endif %}>Gestern</option>
    <option value="week" {% if group == "week" %}selected{% endif %}>Diese Woche</option>
    <option value="older" {% if group == "older" %}selected{% endif %}>Älter</option>
  </select>
  <select name="sort">
    <option value="date_desc" {% if sort == "date_desc" %}selected{% endif %}>Neueste zuerst</option>
    <option value="date_asc" {% if sort == "date_asc" %}selected{% endif %}>Älteste zuerst</option>
    <option value="title_asc" {% if sort == "title_asc" %}selected{% endif %}>Titel A-Z</option>
    <option value="path_asc" {% if sort == "path_asc" %}selected{% endif %}>Pfad A-Z</option>
  </select>
  <button type="submit">Filtern</button>
</form>
<script type="application/json" id="project-paths-data">{{ {"paths": project_paths, "homeDir": local_home_dir} | tojson }}</script>
<ul class="session-list">
  {% for s in sessions %}
  <li>
    <a href="/chats/{{ s.id }}">
      <strong>{{ s.title }}</strong>
      <span class="tool-badge tool-badge-{{ s.tool }}">{{ s.tool }}</span>
      <span class="project">{{ s.project_path }}</span>
      <p>{{ s.last_message_preview }}</p>
      <time>{{ s.last_updated_at | de_datetime }}</time>
    </a>
  </li>
  {% else %}
  <li>Keine Chats gefunden.</li>
  {% endfor %}
</ul>
{% endblock %}
```

(Note: the preview line stays plain-escaped `{{ s.last_message_preview }}`, not markdown-rendered
— this was deliberately changed in Plan A's final review for page-load performance across
thousands of sessions; do not reintroduce the `| markdown | safe` filter here.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_list.py -v`
Expected: PASS (all tests, existing + new)

- [ ] **Step 5: Commit**

```bash
git add backend/app/templates/list.html backend/tests/test_list.py
git commit -m "feat(backend): sort dropdown, path combobox markup, and German timestamps on the list page"
```

---

## Task 9: `detail.html` — incremental "load more" / "load all" pagination, German timestamps

**Files:**
- Modify: `backend/app/templates/detail.html`
- Test: `backend/tests/test_detail_and_jobs.py`

**Interfaces:**
- Consumes: `session.loaded_message_count`, `session.message_count` (Task 3);
  `de_datetime` filter (Task 4); `.history-actions` CSS class (Task 7).
- Produces: `#load-more` / `#load-all` buttons and `#fetch-status` that Task 10's `app.js` wires up.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_detail_and_jobs.py`:

```python
def test_detail_page_shows_partial_messages_and_load_more_button(logged_in_client):
    import os

    _sync_one(logged_in_client)
    db_path = os.environ["DATABASE_PATH"]
    conn = db.get_connection(db_path)
    db.upsert_session(
        conn,
        {
            "id": "claude-code:abc",
            "tool": "claude-code",
            "entrypoint": "cli",
            "project_path": "/Users/jan/source/demo",
            "title": "Demo session",
            "created_at": "2026-08-01T10:00:00Z",
            "last_updated_at": "2026-08-01T10:05:00Z",
            "message_count": 5,
            "last_message_preview": "hi there",
            "status": "idle",
        },
    )
    db.replace_messages(
        conn,
        "claude-code:abc",
        [{"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": "partial one"}],
    )
    conn.close()

    response = logged_in_client.get("/chats/claude-code:abc")
    assert "partial one" in response.text
    assert 'id="load-more"' in response.text
    assert 'id="load-all"' in response.text


def test_detail_page_hides_buttons_once_fully_loaded(logged_in_client):
    import os

    _sync_one(logged_in_client)
    db_path = os.environ["DATABASE_PATH"]
    conn = db.get_connection(db_path)
    db.replace_messages(
        conn,
        "claude-code:abc",
        [{"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": "the only message"}],
    )  # _sync_one's message_count is 1, so loading 1 message here means fully loaded
    conn.close()

    response = logged_in_client.get("/chats/claude-code:abc")
    assert "the only message" in response.text
    assert 'id="load-more"' not in response.text
    assert 'id="load-all"' not in response.text
```

Run: `cd backend && .venv/bin/pytest tests/test_detail_and_jobs.py -v -k load_more_button`
Expected: FAIL — no `id="load-more"` / `id="load-all"` in the rendered page

- [ ] **Step 2: Rewrite `backend/app/templates/detail.html`**

```html
{% extends "base.html" %}
{% block content %}
{% if session is none %}
<p>Chat nicht gefunden.</p>
{% else %}
<h1>{{ session.title }}</h1>
<p>{{ session.project_path }} — {{ session.tool }}</p>
{% if session.loaded_message_count == 0 %}
<p>{{ session.last_message_preview | markdown | safe }}</p>
{% else %}
<div class="messages">
  {% for m in messages %}
  <div class="message message-{{ m.role }}">
    <time>{{ m.timestamp | de_datetime }}</time>
    <div>{{ m.content | markdown | safe }}</div>
  </div>
  {% endfor %}
</div>
{% endif %}
{% if session.loaded_message_count < session.message_count %}
<div class="history-actions">
  <button id="load-more" data-session-id="{{ session.id }}">Mehr laden</button>
  <button id="load-all" data-session-id="{{ session.id }}">Gesamte Historie laden</button>
  <p id="fetch-status"></p>
</div>
{% endif %}
{% endif %}
{% endblock %}
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/ -v`
Expected: PASS (the full backend suite, including every test from Tasks 1–8)

- [ ] **Step 4: Commit**

```bash
git add backend/app/templates/detail.html backend/tests/test_detail_and_jobs.py
git commit -m "feat(backend): incremental load-more/load-all pagination on the chat detail page"
```

---

## Task 10: `app.js` — path combobox behavior and two-button fetch/poll logic

**Files:**
- Modify: `backend/app/static/app.js`

**Interfaces:**
- Consumes: `#project-input`/`#project-listbox`/`#project-paths-data` (Task 8),
  `#load-more`/`#load-all`/`#fetch-status` (Task 9).
- Produces: none consumed by later tasks — this is the last piece of wiring.

No automated test framework exists for JS in this project (a deliberate decision carried over from
Plan A) — this task is verified manually in a browser in Step 3.

- [ ] **Step 1: Replace the contents of `backend/app/static/app.js`**

```js
document.addEventListener("DOMContentLoaded", () => {
  const status = document.getElementById("fetch-status");
  const loadMoreButton = document.getElementById("load-more");
  const loadAllButton = document.getElementById("load-all");
  if (!status || (!loadMoreButton && !loadAllButton)) return;

  const sessionId = (loadMoreButton || loadAllButton).dataset.sessionId;

  const setButtonsDisabled = (disabled) => {
    if (loadMoreButton) loadMoreButton.disabled = disabled;
    if (loadAllButton) loadAllButton.disabled = disabled;
  };

  const poll = async (jobId) => {
    try {
      const statusRes = await fetch(`/chats/${sessionId}/status?job_id=${jobId}`);
      if (!statusRes.ok) {
        throw new Error(`HTTP ${statusRes.status}`);
      }
      const data = await statusRes.json();
      if (data.status === "done" || data.status === "failed") {
        status.textContent = data.status === "done" ? "Fertig, lade neu..." : "Fehlgeschlagen.";
        if (data.status === "done") {
          location.reload();
        } else {
          setButtonsDisabled(false);
        }
      } else {
        setTimeout(() => poll(jobId), 3000);
      }
    } catch (error) {
      status.textContent = "Verbindung verloren — bitte Seite neu laden oder erneut versuchen.";
      setButtonsDisabled(false);
    }
  };

  const startFetch = async (full) => {
    setButtonsDisabled(true);
    status.textContent = "Wird geladen...";
    try {
      const url = `/chats/${sessionId}/fetch-full${full ? "?full=true" : ""}`;
      const res = await fetch(url, { method: "POST" });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const { job_id } = await res.json();
      poll(job_id);
    } catch (error) {
      status.textContent = "Fehler beim Starten — bitte erneut versuchen.";
      setButtonsDisabled(false);
    }
  };

  if (loadMoreButton) loadMoreButton.addEventListener("click", () => startFetch(false));
  if (loadAllButton) loadAllButton.addEventListener("click", () => startFetch(true));
});

document.addEventListener("DOMContentLoaded", () => {
  const input = document.getElementById("project-input");
  const listbox = document.getElementById("project-listbox");
  const dataScript = document.getElementById("project-paths-data");
  if (!input || !listbox || !dataScript) return;

  const { paths, homeDir } = JSON.parse(dataScript.textContent);
  let activeIndex = -1;

  const expandHome = (value) => {
    if (value === "~") return homeDir;
    if (value.startsWith("~/")) return homeDir.replace(/\/$/, "") + "/" + value.slice(2);
    return value;
  };

  const currentMatches = () => {
    const needle = expandHome(input.value).toLowerCase();
    if (!needle) return [];
    return paths.filter((p) => p.toLowerCase().includes(needle));
  };

  const select = (path) => {
    input.value = path;
    listbox.hidden = true;
    activeIndex = -1;
    input.setAttribute("aria-expanded", "false");
  };

  const render = (matches) => {
    listbox.innerHTML = "";
    matches.forEach((path, i) => {
      const li = document.createElement("li");
      li.textContent = path;
      li.setAttribute("role", "option");
      li.dataset.index = String(i);
      if (i === activeIndex) li.classList.add("active");
      li.addEventListener("mousedown", (event) => {
        event.preventDefault();
        select(path);
      });
      listbox.appendChild(li);
    });
    listbox.hidden = matches.length === 0;
    input.setAttribute("aria-expanded", matches.length > 0 ? "true" : "false");
  };

  input.addEventListener("input", () => {
    activeIndex = -1;
    render(currentMatches());
  });

  input.addEventListener("keydown", (event) => {
    const matches = currentMatches();
    if (listbox.hidden && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      render(matches);
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      activeIndex = Math.min(activeIndex + 1, matches.length - 1);
      render(matches);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      activeIndex = Math.max(activeIndex - 1, 0);
      render(matches);
    } else if (event.key === "Enter") {
      if (activeIndex >= 0 && matches[activeIndex]) {
        event.preventDefault();
        select(matches[activeIndex]);
      }
    } else if (event.key === "Escape") {
      listbox.hidden = true;
      activeIndex = -1;
    }
  });

  input.addEventListener("blur", () => {
    setTimeout(() => {
      listbox.hidden = true;
    }, 100);
  });
});
```

- [ ] **Step 2: Run the full backend test suite once more**

Run: `cd backend && .venv/bin/pytest tests/ -v`
Expected: PASS (JS changes don't affect Python tests, but this confirms nothing else regressed)

- [ ] **Step 3: Manually verify in a browser**

Run: `cd backend && DATABASE_PATH=/tmp/js-check.db API_KEY=devkeydevkeydevkey1 SECRET_KEY=devsecretdevsecret1 SESSION_COOKIE_HTTPS_ONLY=false .venv/bin/uvicorn app.main:app --reload`

Then, with at least one session synced into `/tmp/js-check.db` (use the `/sync/index` endpoint
with a `curl` call, or point a real agent at this backend), open the list page and confirm:

- Typing in the path field narrows the dropdown to matching paths (substring match); typing
  `~/` shows paths under the configured home directory.
- Arrow keys move the highlighted option; Enter selects it into the input; Escape closes the
  dropdown; clicking an option selects it.
- On a chat's detail page with only a preview loaded, both "Mehr laden" and "Gesamte Historie
  laden" appear; clicking "Mehr laden" shows "Wird geladen...", then reloads showing more
  messages once an agent processes the job (or shows the graceful failure message if no agent is
  running to pick up the job — that's expected without a live agent attached).

- [ ] **Step 4: Commit**

```bash
git add backend/app/static/app.js
git commit -m "feat(backend): path combobox and two-button load-more/load-all wiring"
```

---

## Task 11: End-to-end manual smoke test against real data

**Files:** none (verification only)

**Interfaces:** none — this task exercises everything built in Tasks 1–10 together, the same way
Plan A's final smoke test did.

- [ ] **Step 1: Start the backend locally against real data**

Run: `./run.sh` from the repo root (builds/starts the Docker container and runs one real agent
sync cycle against your actual Claude Code / Cursor history, per Plan A's existing script).

- [ ] **Step 2: Verify sorting**

Open the list page, try all four `sort` options, confirm the ordering visually matches each
(newest-first, oldest-first, alphabetical by title, alphabetical by path).

- [ ] **Step 3: Verify the path combobox against real project folders**

Type a few characters of a real project folder name; confirm the dropdown narrows to matching
real paths. Type `~/source/` (or whatever your real home-relative prefix is) and confirm it
matches real absolute paths under that prefix.

- [ ] **Step 4: Verify timestamp formatting**

Open a chat updated within the last hour and one from more than a year ago (if one exists);
confirm both render as `Wochentag, DD.MM.[YYYY] HH:MM (vor ...)` with correct relative wording,
and that the "Mac zuletzt erreichbar" line at the top of the list page shows the same format.

- [ ] **Step 5: Verify incremental history pagination end-to-end**

Open a real chat with more than `CHAT_HISTORY_PAGE_SIZE` messages that hasn't been fully loaded
yet. Click "Mehr laden", wait for the next agent sync cycle (or trigger one manually per Plan A's
agent README) to pick up and complete the job, confirm the page reloads showing more messages
than before but not all of them (assuming the chat has more than one page's worth), and that
"Mehr laden" is still present. Click "Gesamte Historie laden" on the same chat and confirm it
loads everything and the buttons disappear once `loaded_message_count >= message_count`.

- [ ] **Step 6: Record the result**

If everything above works, this plan is complete. If any step fails, note the exact failure
(which step, what error) before considering the feature done.
