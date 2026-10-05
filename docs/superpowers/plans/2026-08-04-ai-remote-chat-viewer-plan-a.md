# AI Remote Chat Viewer — Plan A (Read-Only Viewer) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a working, deployable read-only remote viewer for Claude Code + Cursor chat history: local sync agent, backend API + storage, and a mobile-first web UI with grouping/filtering/search and on-demand full-history loading. Plan B (remote commands / job execution beyond `fetch_full`) builds on top of this later.

**Architecture:** A local Python agent scans Claude Code's `.jsonl` files and Cursor's SQLite stores on a 60s loop, pushes deltas to a FastAPI backend (SQLite+FTS5) over HTTPS with a Bearer API key, and polls the same backend for `fetch_full` jobs. The backend also serves a server-rendered (Jinja2) mobile web UI, authenticated via a signed session cookie set once from the same API key.

**Tech Stack:** Python 3.12, FastAPI + Uvicorn, SQLite (stdlib `sqlite3`) with FTS5, Jinja2 templates, vanilla JS (no frontend build step), `httpx` for the agent's HTTP calls, `markdown` + `pygments` for rendering, pytest for tests, Docker for deployment.

## Global Constraints

- Single Mac, single user, personal use only — no multi-tenant auth.
- Backend deploys as one Docker container behind Plesk at `https://your-domain.example.com`; SQLite file lives on a mounted volume.
- Sync/job cycle runs every 60 seconds (both directions) — see spec's Sync Mechanics.
- Full-text search covers titles + last-message previews immediately; full message content becomes searchable only after a session's `fetch_full` job has completed (see spec's accepted trade-off).
- No file/message editing or deletion — read-only in this plan.
- The local agent's sync cursor lives only on the Mac (a local JSON file), never in the backend database.
- This plan implements job type `fetch_full` only. Job types `resume_message`/`new_session` and their guardrails belong to Plan B — do not implement command execution here.
- Reference spec: `docs/superpowers/specs/2026-08-04-ai-remote-chat-viewer-design.md`.
- Root-level operational scripts (`run.sh`, `build-and-push.sh`, `build-and-deploy.sh`, `deploy-production-scp.sh`) must follow Jan's standard app-script contract (`~/.claude/skills/create-jans-standard-app-scripts`): each script supports `--help`; `run.sh` also supports `--rebuild` and `--test` and ends every successful run with a printed verification block + boxed `RUNNING ✓` banner + next-steps section. `run.sh` is what makes the app locally runnable and testable against the user's real Claude Code / Cursor data — treat it as a hard requirement of this plan, not optional polish.

---

## File Structure

```
docker-compose.yml       # Task 8 — local/dev compose file, service "backend"
example.env              # Task 8 — copy to .env; API_KEY/SECRET_KEY/PORT/REGISTRY/SSH_HOST
run.sh                   # Task 16 — local run + test entrypoint (Jan's standard script contract)
build-and-push.sh        # Task 16
build-and-deploy.sh      # Task 16
deploy-production-scp.sh # Task 16

backend/
  requirements.txt
  Dockerfile
  README.md
  app/
    __init__.py
    main.py              # FastAPI app wiring, startup DB init, session middleware
    db.py                 # SQLite connection, schema init, all queries
    schema.sql             # table + FTS5 definitions
    models.py             # pydantic request/response models
    auth.py                # Bearer (agent) + cookie-session (browser) checks
    markdown_filter.py    # Jinja2 "markdown" filter
    routes_agent.py        # /sync/index, /jobs/pending, /jobs/{id}/complete
    routes_web.py           # /login, /, /chats/{id}, /chats/{id}/fetch-full, /chats/{id}/status
    templates/
      base.html
      login.html
      list.html
      detail.html
    static/
      manifest.json
      style.css
      app.js
  tests/
    conftest.py
    test_db.py
    test_auth_and_sync.py
    test_list.py
    test_detail_and_jobs.py

agent/
  requirements.txt
  README.md
  agent/
    __init__.py
    config.py              # env-var driven Config dataclass
    claude_code_source.py  # parses ~/.claude/projects/**/*.jsonl
    cursor_source.py        # reads conversation-search.db + state.vscdb
    state.py                # local sync-cursor persistence + delta computation
    uploader.py             # POST /sync/index
    jobs.py                  # GET /jobs/pending, POST /jobs/{id}/complete
    executor.py               # dispatches fetch_full jobs to the right source module
    main.py                   # the 60s loop
  launchd/
    com.example.ai-remote-agent.plist
  tests/
    fixtures/
      sample_session.jsonl
    test_claude_code_source.py
    test_cursor_source.py
    test_state.py
    test_uploader_and_jobs.py
```

---

## Task 1: Backend scaffold, SQLite schema, DB access layer

**Files:**
- Create: `backend/requirements.txt`
- Create: `backend/app/__init__.py` (empty)
- Create: `backend/app/schema.sql`
- Create: `backend/app/db.py`
- Test: `backend/tests/conftest.py`
- Test: `backend/tests/test_db.py`

**Interfaces:**
- Produces: `db.get_connection(db_path: str) -> sqlite3.Connection`, `db.init_db(conn) -> None`, `db.get_db_dependency() -> Generator[sqlite3.Connection, None, None]` (reads `DATABASE_PATH` env var), `db.upsert_session(conn, session: dict) -> None`, `db.get_sessions(conn, tool=None, project=None, date_group=None, q=None) -> list[dict]`, `db.get_session(conn, session_id: str) -> dict | None`, `db.get_messages(conn, session_id: str) -> list[dict]`, `db.replace_messages(conn, session_id: str, messages: list[dict]) -> None`, `db.create_job(conn, job_type: str, target: str, payload: str = "") -> str`, `db.claim_pending_jobs(conn) -> list[dict]`, `db.complete_job(conn, job_id: str, status: str, result_text: str, messages: list[dict]) -> None`, `db.get_job(conn, job_id: str) -> dict | None`, `db.fail_stale_jobs(conn, timeout_seconds: int = 300) -> None`, `db.record_agent_contact(conn) -> None`, `db.get_last_agent_contact(conn) -> str | None` (used by the frontend's "Mac last seen" staleness indicator, spec's Error Handling section).

- [ ] **Step 1: Write `backend/requirements.txt`**

```
fastapi>=0.115
uvicorn[standard]>=0.32
jinja2>=3.1
python-multipart>=0.0.9
itsdangerous>=2.2
markdown>=3.7
pygments>=2.18
httpx>=0.27
pytest>=8.3
```

- [ ] **Step 2: Install deps and write the failing schema test**

Run: `cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`

Create `backend/tests/conftest.py`:

```python
import pytest


@pytest.fixture
def conn(tmp_path):
    from app import db

    connection = db.get_connection(str(tmp_path / "test.db"))
    db.init_db(connection)
    yield connection
    connection.close()
```

Create `backend/tests/test_db.py`:

```python
def test_init_db_creates_expected_tables(conn):
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"sessions", "messages", "jobs"} <= tables
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app'` (package doesn't exist yet)

- [ ] **Step 4: Create `backend/app/__init__.py`** (empty file)

- [ ] **Step 5: Write `backend/app/schema.sql`**

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
    status TEXT NOT NULL DEFAULT 'idle'
);

CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL REFERENCES sessions(id),
    idx INTEGER NOT NULL,
    role TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (session_id, idx)
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    session_id UNINDEXED,
    kind UNINDEXED,
    text
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN ('fetch_full', 'resume_message', 'new_session')),
    target TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'running', 'done', 'failed')),
    result_text TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_contact_at TEXT
);
INSERT OR IGNORE INTO agent_status (id, last_contact_at) VALUES (1, NULL);
```

- [ ] **Step 6: Write `backend/app/db.py`**

```python
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


def get_db_dependency():
    conn = get_connection(os.environ.get("DATABASE_PATH", "app.db"))
    try:
        yield conn
    finally:
        conn.close()


def upsert_session(conn: sqlite3.Connection, session: dict) -> None:
    conn.execute(
        """
        INSERT INTO sessions (id, tool, entrypoint, project_path, title, created_at,
                               last_updated_at, message_count, last_message_preview, status)
        VALUES (:id, :tool, :entrypoint, :project_path, :title, :created_at,
                :last_updated_at, :message_count, :last_message_preview, :status)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            last_updated_at = excluded.last_updated_at,
            message_count = excluded.message_count,
            last_message_preview = excluded.last_message_preview,
            project_path = excluded.project_path,
            status = excluded.status
        """,
        session,
    )
    conn.execute(
        "DELETE FROM search_index WHERE session_id = ? AND kind = 'header'",
        (session["id"],),
    )
    conn.execute(
        "INSERT INTO search_index (session_id, kind, text) VALUES (?, 'header', ?)",
        (session["id"], f"{session['title']} {session['last_message_preview']}"),
    )
    conn.commit()


def _date_group_bounds(group: str) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if group == "today":
        start, end = today_start, today_start + timedelta(days=1)
    elif group == "yesterday":
        start, end = today_start - timedelta(days=1), today_start
    elif group == "week":
        start, end = today_start - timedelta(days=7), today_start - timedelta(days=1)
    else:  # "older"
        start, end = datetime.min.replace(tzinfo=timezone.utc), today_start - timedelta(days=7)
    return start.isoformat(), end.isoformat()


def get_sessions(
    conn: sqlite3.Connection,
    tool: str | None = None,
    project: str | None = None,
    date_group: str | None = None,
    q: str | None = None,
) -> list[dict]:
    ids_filter = None
    if q:
        safe_q = '"' + q.replace('"', '""') + '"'
        rows = conn.execute(
            "SELECT DISTINCT session_id FROM search_index WHERE search_index MATCH ?",
            (safe_q,),
        ).fetchall()
        ids_filter = {row["session_id"] for row in rows}
        if not ids_filter:
            return []

    query = "SELECT * FROM sessions WHERE 1=1"
    params: list = []
    if tool:
        query += " AND tool = ?"
        params.append(tool)
    if project:
        query += " AND project_path LIKE ?"
        params.append(f"%{project}%")
    if date_group:
        start, end = _date_group_bounds(date_group)
        query += " AND last_updated_at >= ? AND last_updated_at < ?"
        params.extend([start, end])
    query += " ORDER BY last_updated_at DESC"

    rows = [dict(row) for row in conn.execute(query, params).fetchall()]
    if ids_filter is not None:
        rows = [row for row in rows if row["id"] in ids_filter]
    return rows


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def get_messages(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT idx, role, timestamp, content FROM messages WHERE session_id = ? ORDER BY idx",
        (session_id,),
    ).fetchall()
    return [dict(row) for row in rows]


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
    conn.execute("UPDATE sessions SET full_content_synced = 1 WHERE id = ?", (session_id,))
    conn.commit()


def create_job(conn: sqlite3.Connection, job_type: str, target: str, payload: str = "") -> str:
    job_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO jobs (id, type, target, payload, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
        (job_id, job_type, target, payload, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return job_id


def claim_pending_jobs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM jobs WHERE status = 'pending'").fetchall()
    claimed = []
    for row in rows:
        cursor = conn.execute(
            "UPDATE jobs SET status = 'running' WHERE id = ? AND status = 'pending'",
            (row["id"],),
        )
        if cursor.rowcount:
            claimed.append(dict(row))
    conn.commit()
    return claimed


def complete_job(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    result_text: str,
    messages: list[dict],
) -> None:
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        return
    conn.execute(
        "UPDATE jobs SET status = ?, result_text = ?, completed_at = ? WHERE id = ?",
        (status, result_text, datetime.now(timezone.utc).isoformat(), job_id),
    )
    conn.commit()
    if status == "done" and job["type"] == "fetch_full" and messages:
        replace_messages(conn, job["target"], messages)


def get_job(conn: sqlite3.Connection, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def fail_stale_jobs(conn: sqlite3.Connection, timeout_seconds: int = 300) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
    conn.execute(
        "UPDATE jobs SET status = 'failed', result_text = 'timed out', completed_at = ? "
        "WHERE status = 'running' AND created_at < ?",
        (datetime.now(timezone.utc).isoformat(), cutoff),
    )
    conn.commit()


def record_agent_contact(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE agent_status SET last_contact_at = ? WHERE id = 1",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()


def get_last_agent_contact(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT last_contact_at FROM agent_status WHERE id = 1").fetchone()
    return row["last_contact_at"] if row else None
```

- [ ] **Step 7: Run test to verify it passes**

Run: `cd backend && .venv/bin/pytest tests/test_db.py -v`
Expected: PASS

- [ ] **Step 8: Add a session-lifecycle test covering upsert/get/messages/jobs**

Append to `backend/tests/test_db.py`:

```python
from app import db


def _sample_session(session_id="claude-code:abc"):
    return {
        "id": session_id,
        "tool": "claude-code",
        "entrypoint": "cli",
        "project_path": "/Users/jan/source/demo",
        "title": "Demo session",
        "created_at": "2026-08-01T10:00:00Z",
        "last_updated_at": "2026-08-01T10:05:00Z",
        "message_count": 2,
        "last_message_preview": "done",
        "status": "idle",
    }


def test_upsert_and_get_session(conn):
    db.upsert_session(conn, _sample_session())
    fetched = db.get_session(conn, "claude-code:abc")
    assert fetched["title"] == "Demo session"
    assert fetched["full_content_synced"] == 0


def test_get_sessions_filters_by_tool_and_search(conn):
    db.upsert_session(conn, _sample_session("claude-code:abc"))
    db.upsert_session(conn, _sample_session("cursor:xyz") | {"tool": "cursor", "id": "cursor:xyz", "title": "Other"})

    assert [s["id"] for s in db.get_sessions(conn, tool="cursor")] == ["cursor:xyz"]
    assert [s["id"] for s in db.get_sessions(conn, q="Demo")] == ["claude-code:abc"]
    assert db.get_sessions(conn, q="nonexistent") == []


def test_job_lifecycle_and_message_replacement(conn):
    db.upsert_session(conn, _sample_session())
    job_id = db.create_job(conn, "fetch_full", "claude-code:abc")

    pending = db.claim_pending_jobs(conn)
    assert pending[0]["id"] == job_id
    assert pending[0]["status"] == "running"
    assert db.claim_pending_jobs(conn) == []  # already claimed, no double pickup

    messages = [{"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": "hi"}]
    db.complete_job(conn, job_id, "done", "", messages)

    job = db.get_job(conn, job_id)
    assert job["status"] == "done"
    stored = db.get_messages(conn, "claude-code:abc")
    assert stored == messages
    assert db.get_session(conn, "claude-code:abc")["full_content_synced"] == 1


def test_agent_contact_starts_unset_then_records_timestamp(conn):
    assert db.get_last_agent_contact(conn) is None
    db.record_agent_contact(conn)
    contact = db.get_last_agent_contact(conn)
    assert contact is not None
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_db.py -v`
Expected: PASS (5 tests)

- [ ] **Step 10: Commit**

```bash
git add backend/requirements.txt backend/app/__init__.py backend/app/schema.sql backend/app/db.py backend/tests/conftest.py backend/tests/test_db.py
git commit -m "feat(backend): SQLite schema and data access layer"
```

---

## Task 2: Auth — Bearer key for the agent, cookie session for the browser

**Files:**
- Create: `backend/app/auth.py`
- Create: `backend/app/main.py` (minimal app + auth-only routes for this task; routers added in later tasks)
- Test: `backend/tests/test_db.py` (unchanged)
- Test: `backend/tests/test_auth_and_sync.py`

**Interfaces:**
- Consumes: `db.get_db_dependency`, `db.get_connection`, `db.init_db` (Task 1)
- Produces: `auth.require_api_key` (FastAPI dependency, 401 on bad/missing Bearer token), `auth.is_authenticated(request) -> bool`, `auth.require_session` (FastAPI dependency, redirects to `/login` if not authenticated), a running `app.main.app` FastAPI instance other tasks add routers to.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_auth_and_sync.py`:

```python
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_agent_route_rejects_missing_key(client):
    response = client.get("/jobs/pending")
    assert response.status_code == 401


def test_agent_route_accepts_correct_key(client):
    response = client.get("/jobs/pending", headers={"Authorization": "Bearer test-key"})
    assert response.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/test_auth_and_sync.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.main'`

- [ ] **Step 3: Write `backend/app/auth.py`**

```python
import os

from fastapi import Header, HTTPException, Request


def require_api_key(authorization: str = Header(default="")) -> None:
    expected = os.environ["API_KEY"]
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid or missing API key")


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


def require_session(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=307, headers={"Location": "/login"})
```

- [ ] **Step 4: Write a minimal `backend/app/main.py` with a placeholder `/jobs/pending` route**

(The real `/jobs/pending` implementation with DB-backed job claiming is wired in Task 6; for now this task only needs to prove the auth dependency works, so route logic here is temporary scaffolding that Task 6 will replace with real body.)

```python
import os

from fastapi import Depends, FastAPI
from starlette.middleware.sessions import SessionMiddleware

from . import db
from .auth import require_api_key

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY", "dev-secret"))


@app.on_event("startup")
def on_startup() -> None:
    conn = db.get_connection(os.environ.get("DATABASE_PATH", "app.db"))
    db.init_db(conn)
    conn.close()


@app.get("/jobs/pending", dependencies=[Depends(require_api_key)])
def jobs_pending_stub():
    return {"jobs": []}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && .venv/bin/pytest tests/test_auth_and_sync.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Add a browser-session auth test**

Append to `backend/tests/test_auth_and_sync.py`:

```python
def test_browser_route_without_session_redirects_to_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/login"
```

Add a temporary stub route to `backend/app/main.py` (Task 4 replaces this with the real list page):

```python
from fastapi import Request
from .auth import require_session


@app.get("/", dependencies=[Depends(require_session)])
def list_chats_stub(request: Request):
    return {"ok": True}
```

- [ ] **Step 7: Run test to verify it passes**

Run: `cd backend && .venv/bin/pytest tests/test_auth_and_sync.py -v`
Expected: PASS (3 tests)

- [ ] **Step 8: Commit**

```bash
git add backend/app/auth.py backend/app/main.py backend/tests/test_auth_and_sync.py
git commit -m "feat(backend): Bearer auth for the agent, cookie session for the browser"
```

---

## Task 3: `POST /sync/index` — agent pushes session deltas

**Files:**
- Create: `backend/app/models.py`
- Modify: `backend/app/main.py` (add real `/sync/index` route; remove the `/jobs/pending` stub, moved to Task 6's router)
- Modify: `backend/tests/test_auth_and_sync.py`

**Interfaces:**
- Consumes: `db.upsert_session`, `db.get_db_dependency`, `auth.require_api_key` (Task 1 & 2)
- Produces: pydantic models `SessionIn`, `MessageIn`, `SyncIndexRequest`, `JobCompleteRequest` in `models.py`, reused by Tasks 6 and by the agent's payload shape.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_auth_and_sync.py`:

```python
def test_sync_index_upserts_sessions(client):
    payload = {
        "sessions": [
            {
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
        ]
    }
    response = client.post(
        "/sync/index", json=payload, headers={"Authorization": "Bearer test-key"}
    )
    assert response.status_code == 200
    assert response.json() == {"received": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/test_auth_and_sync.py -v`
Expected: FAIL — 404 (no `/sync/index` route yet)

- [ ] **Step 3: Write `backend/app/models.py`**

```python
from pydantic import BaseModel


class SessionIn(BaseModel):
    id: str
    tool: str
    entrypoint: str = ""
    project_path: str = ""
    title: str
    created_at: str
    last_updated_at: str
    message_count: int = 0
    last_message_preview: str = ""
    status: str = "idle"


class SyncIndexRequest(BaseModel):
    sessions: list[SessionIn]


class MessageIn(BaseModel):
    idx: int
    role: str
    timestamp: str
    content: str


class JobCompleteRequest(BaseModel):
    status: str
    result_text: str = ""
    messages: list[MessageIn] = []
```

- [ ] **Step 4: Add the real `/sync/index` route to `backend/app/main.py`**

Add these imports and route (keep the existing `/jobs/pending` and `/` stubs for now — Tasks 4/6 replace them):

```python
from .models import SyncIndexRequest


@app.post("/sync/index", dependencies=[Depends(require_api_key)])
def sync_index(body: SyncIndexRequest, conn=Depends(db.get_db_dependency)):
    for session in body.sessions:
        db.upsert_session(conn, session.model_dump())
    db.record_agent_contact(conn)
    return {"received": len(body.sessions)}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && .venv/bin/pytest tests/test_auth_and_sync.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/models.py backend/app/main.py backend/tests/test_auth_and_sync.py
git commit -m "feat(backend): POST /sync/index upserts session deltas"
```

---

## Task 4: List page — `GET /` with filter/group/search

**Files:**
- Create: `backend/app/templates/base.html`
- Create: `backend/app/templates/login.html`
- Create: `backend/app/templates/list.html`
- Create: `backend/app/static/style.css`
- Modify: `backend/app/main.py` (replace the `/` and add `/login` GET+POST routes with real implementations)
- Create: `backend/tests/test_list.py`

**Interfaces:**
- Consumes: `db.get_sessions`, `auth.require_session`, `auth.is_authenticated` (Tasks 1–2)
- Produces: working `/login` (GET+POST) and `/` HTML pages other tasks link to.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_list.py`:

```python
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def logged_in_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    from app.main import app

    with TestClient(app) as test_client:
        test_client.post("/login", data={"api_key": "test-key"})
        yield test_client


def test_login_with_wrong_key_shows_error(logged_in_client):
    response = logged_in_client.post("/login", data={"api_key": "wrong"})
    assert response.status_code == 401
    assert "Invalid" in response.text


def test_list_page_shows_synced_session_and_respects_tool_filter(logged_in_client):
    session = {
        "id": "claude-code:abc",
        "tool": "claude-code",
        "entrypoint": "cli",
        "project_path": "/Users/jan/source/demo",
        "title": "Demo session",
        "created_at": "2026-08-01T10:00:00Z",
        "last_updated_at": "2026-08-01T10:05:00Z",
        "message_count": 1,
        "last_message_preview": "hi there",
        "status": "idle",
    }
    logged_in_client.post(
        "/sync/index",
        json={"sessions": [session]},
        headers={"Authorization": "Bearer test-key"},
    )

    response = logged_in_client.get("/")
    assert "Demo session" in response.text

    filtered = logged_in_client.get("/", params={"tool": "cursor"})
    assert "Demo session" not in filtered.text


def test_list_page_shows_staleness_banner(logged_in_client):
    never = logged_in_client.get("/")
    assert "noch nie erreichbar" in never.text

    logged_in_client.post(
        "/sync/index", json={"sessions": []}, headers={"Authorization": "Bearer test-key"}
    )
    after_contact = logged_in_client.get("/")
    assert "zuletzt erreichbar" in after_contact.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/test_list.py -v`
Expected: FAIL — `/login` POST not implemented (404 or similar)

- [ ] **Step 3: Write templates**

`backend/app/templates/base.html`:

```html
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}AI Remote Chats{% endblock %}</title>
  <link rel="manifest" href="/static/manifest.json">
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <header><a href="/">AI Remote Chats</a></header>
  <main>{% block content %}{% endblock %}</main>
  <script src="/static/app.js"></script>
</body>
</html>
```

`backend/app/templates/login.html`:

```html
{% extends "base.html" %}
{% block content %}
<form method="post" action="/login">
  <label>API Key <input type="password" name="api_key"></label>
  <button type="submit">Login</button>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
</form>
{% endblock %}
```

`backend/app/templates/list.html`:

```html
{% extends "base.html" %}
{% block content %}
<p class="staleness">
  {% if last_agent_contact %}Mac zuletzt erreichbar: {{ last_agent_contact }}
  {% else %}Mac noch nie erreichbar gewesen.{% endif %}
</p>
<form method="get" action="/">
  <input type="text" name="q" value="{{ q or '' }}" placeholder="Suche...">
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
  <button type="submit">Filtern</button>
</form>
<ul class="session-list">
  {% for s in sessions %}
  <li>
    <a href="/chats/{{ s.id }}">
      <strong>{{ s.title }}</strong>
      <span class="tool-badge">{{ s.tool }}</span>
      <span class="project">{{ s.project_path }}</span>
      <p>{{ s.last_message_preview }}</p>
      <time>{{ s.last_updated_at }}</time>
    </a>
  </li>
  {% else %}
  <li>Keine Chats gefunden.</li>
  {% endfor %}
</ul>
{% endblock %}
```

- [ ] **Step 4: Write `backend/app/static/style.css`**

```css
body { font-family: -apple-system, sans-serif; margin: 0; background: #111; color: #eee; }
header { padding: 1rem; font-weight: bold; }
main { padding: 0 1rem 2rem; max-width: 640px; margin: 0 auto; }
form { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem; }
input, select, button { padding: 0.5rem; border-radius: 6px; border: 1px solid #444; background: #1c1c1c; color: #eee; }
.session-list { list-style: none; padding: 0; }
.session-list li { border-bottom: 1px solid #333; padding: 0.75rem 0; }
.session-list a { color: inherit; text-decoration: none; display: block; }
.tool-badge { font-size: 0.75rem; padding: 0.1rem 0.4rem; border-radius: 4px; background: #333; margin-left: 0.5rem; }
.project, time { display: block; font-size: 0.75rem; color: #999; }
.error { color: #f66; }
.message { border-bottom: 1px solid #333; padding: 0.75rem 0; }
.message-user { color: #9cf; }
```

- [ ] **Step 5: Replace `/`, add `/login`, in `backend/app/main.py`**

Remove the `list_chats_stub` function from Task 2 and replace the whole file's route section with:

```python
from datetime import datetime, timezone

from fastapi import Form
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .markdown_filter import render_markdown

templates = Jinja2Templates(directory="app/templates")
templates.env.filters["markdown"] = render_markdown
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login_submit(request: Request, api_key: str = Form(...)):
    if api_key == os.environ["API_KEY"]:
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": "Invalid key"}, status_code=401
    )


def _format_last_contact(iso_timestamp: str | None) -> str | None:
    if not iso_timestamp:
        return None
    contact = datetime.fromisoformat(iso_timestamp)
    minutes_ago = int((datetime.now(timezone.utc) - contact).total_seconds() // 60)
    if minutes_ago < 1:
        return "vor weniger als einer Minute"
    if minutes_ago == 1:
        return "vor 1 Minute"
    return f"vor {minutes_ago} Minuten"


@app.get("/", dependencies=[Depends(require_session)])
def list_chats(
    request: Request,
    tool: str | None = None,
    project: str | None = None,
    group: str | None = None,
    q: str | None = None,
    conn=Depends(db.get_db_dependency),
):
    sessions = db.get_sessions(conn, tool=tool, project=project, date_group=group, q=q)
    return templates.TemplateResponse(
        request,
        "list.html",
        {
            "sessions": sessions,
            "tool": tool,
            "project": project,
            "group": group,
            "q": q,
            "last_agent_contact": _format_last_contact(db.get_last_agent_contact(conn)),
        },
    )
```

Update the `require_session` import at the top of `main.py` to `from .auth import require_api_key, require_session`.

Create `backend/app/markdown_filter.py` now (Task 5 will exercise it further, but `main.py` already imports it):

```python
import markdown as _markdown


def render_markdown(text: str) -> str:
    return _markdown.markdown(text or "", extensions=["fenced_code", "codehilite"])
```

Create placeholder-free minimal `backend/app/static/manifest.json` and `backend/app/static/app.js` now so `StaticFiles` has a valid directory (Task 7 fills in real PWA behavior):

`backend/app/static/manifest.json`:

```json
{
  "name": "AI Remote Chats",
  "short_name": "AI Chats",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#111111",
  "theme_color": "#111111"
}
```

`backend/app/static/app.js`:

```js
// Populated in Task 7 with the "load full history" polling behavior.
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && .venv/bin/pytest tests/test_list.py tests/test_auth_and_sync.py -v`
Expected: PASS (all tests, including the redirect test from Task 2 which still exercises `require_session`)

- [ ] **Step 7: Commit**

```bash
git add backend/app/templates backend/app/static backend/app/main.py backend/app/markdown_filter.py backend/tests/test_list.py
git commit -m "feat(backend): login page and filterable/searchable chat list page"
```

---

## Task 5: Chat detail page — `GET /chats/{id}`

**Files:**
- Create: `backend/app/templates/detail.html`
- Modify: `backend/app/main.py` (add `/chats/{id}` route)
- Create: `backend/tests/test_detail_and_jobs.py`

**Interfaces:**
- Consumes: `db.get_session`, `db.get_messages`, `render_markdown` filter (Tasks 1 & 4)
- Produces: `/chats/{id}` HTML page; establishes the `detail.html` template that Task 6 extends with the "load full history" button.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_detail_and_jobs.py`:

```python
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def logged_in_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    from app.main import app

    with TestClient(app) as test_client:
        test_client.post("/login", data={"api_key": "test-key"})
        yield test_client


def _sync_one(client):
    session = {
        "id": "claude-code:abc",
        "tool": "claude-code",
        "entrypoint": "cli",
        "project_path": "/Users/jan/source/demo",
        "title": "Demo session",
        "created_at": "2026-08-01T10:00:00Z",
        "last_updated_at": "2026-08-01T10:05:00Z",
        "message_count": 1,
        "last_message_preview": "hi there",
        "status": "idle",
    }
    client.post(
        "/sync/index",
        json={"sessions": [session]},
        headers={"Authorization": "Bearer test-key"},
    )


def test_detail_page_shows_preview_when_not_fully_synced(logged_in_client):
    _sync_one(logged_in_client)
    response = logged_in_client.get("/chats/claude-code:abc")
    assert response.status_code == 200
    assert "hi there" in response.text


def test_detail_page_404_for_unknown_session(logged_in_client):
    response = logged_in_client.get("/chats/does-not-exist")
    assert response.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/test_detail_and_jobs.py -v`
Expected: FAIL — 404/`TemplateNotFound` for `/chats/...` (route doesn't exist yet)

- [ ] **Step 3: Write `backend/app/templates/detail.html`**

```html
{% extends "base.html" %}
{% block content %}
{% if session is none %}
<p>Chat nicht gefunden.</p>
{% else %}
<h1>{{ session.title }}</h1>
<p>{{ session.project_path }} — {{ session.tool }}</p>
{% if not session.full_content_synced %}
<div>
  <p>{{ session.last_message_preview | markdown | safe }}</p>
</div>
{% else %}
<div class="messages">
  {% for m in messages %}
  <div class="message message-{{ m.role }}">
    <time>{{ m.timestamp }}</time>
    <div>{{ m.content | markdown | safe }}</div>
  </div>
  {% endfor %}
</div>
{% endif %}
{% endif %}
{% endblock %}
```

- [ ] **Step 4: Add the route to `backend/app/main.py`**

```python
@app.get("/chats/{session_id}", dependencies=[Depends(require_session)])
def chat_detail(request: Request, session_id: str, conn=Depends(db.get_db_dependency)):
    session = db.get_session(conn, session_id)
    if session is None:
        return templates.TemplateResponse(
            request, "detail.html", {"session": None, "messages": []}, status_code=404
        )
    messages = db.get_messages(conn, session_id) if session["full_content_synced"] else []
    return templates.TemplateResponse(request, "detail.html", {"session": session, "messages": messages})
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && .venv/bin/pytest tests/test_detail_and_jobs.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/templates/detail.html backend/app/main.py backend/tests/test_detail_and_jobs.py
git commit -m "feat(backend): chat detail page showing preview or full messages"
```

---

## Task 6: Job lifecycle — fetch-full enqueue, agent polling, completion, status poll

**Files:**
- Modify: `backend/app/main.py` (real `/jobs/pending`, new `/jobs/{id}/complete`, `/chats/{id}/fetch-full`, `/chats/{id}/status`; remove the Task-2 stub)
- Modify: `backend/app/templates/detail.html` (add the "load full history" button)
- Modify: `backend/app/static/app.js` (polling behavior)
- Modify: `backend/tests/test_detail_and_jobs.py`

**Interfaces:**
- Consumes: `db.create_job`, `db.claim_pending_jobs`, `db.complete_job`, `db.get_job`, `db.fail_stale_jobs`, `JobCompleteRequest` (Tasks 1 & 3)
- Produces: the complete job-based read-refresh flow the agent (Task 13) and the browser both rely on. Job `status` values used consistently everywhere: `pending`, `running`, `done`, `failed`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_detail_and_jobs.py`:

```python
def test_fetch_full_job_round_trip(logged_in_client):
    _sync_one(logged_in_client)

    enqueue = logged_in_client.post("/chats/claude-code:abc/fetch-full")
    assert enqueue.status_code == 200
    job_id = enqueue.json()["job_id"]

    pending = logged_in_client.get(
        "/jobs/pending", headers={"Authorization": "Bearer test-key"}
    ).json()["jobs"]
    assert pending[0]["id"] == job_id
    assert pending[0]["target"] == "claude-code:abc"

    complete = logged_in_client.post(
        f"/jobs/{job_id}/complete",
        json={
            "status": "done",
            "result_text": "",
            "messages": [
                {"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": "hi"},
                {"idx": 1, "role": "assistant", "timestamp": "2026-08-01T10:01:00Z", "content": "hello back"},
            ],
        },
        headers={"Authorization": "Bearer test-key"},
    )
    assert complete.status_code == 200

    status = logged_in_client.get(f"/chats/claude-code:abc/status?job_id={job_id}")
    assert status.json() == {"status": "done"}

    detail = logged_in_client.get("/chats/claude-code:abc")
    assert "hello back" in detail.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/test_detail_and_jobs.py -v`
Expected: FAIL — `/chats/.../fetch-full` route doesn't exist

- [ ] **Step 3: Replace the `/jobs/pending` stub and add the new routes in `backend/app/main.py`**

Remove `jobs_pending_stub`. Add:

```python
from .models import JobCompleteRequest


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
def fetch_full(session_id: str, conn=Depends(db.get_db_dependency)):
    job_id = db.create_job(conn, "fetch_full", session_id)
    return {"job_id": job_id}


@app.get("/chats/{session_id}/status", dependencies=[Depends(require_session)])
def job_status(session_id: str, job_id: str, conn=Depends(db.get_db_dependency)):
    job = db.get_job(conn, job_id)
    return {"status": job["status"] if job else "unknown"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/bin/pytest tests/test_detail_and_jobs.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Wire the button into the UI**

Update `backend/app/templates/detail.html`'s "not fully synced" branch:

```html
{% if not session.full_content_synced %}
<div>
  <p>{{ session.last_message_preview | markdown | safe }}</p>
  <button id="fetch-full" data-session-id="{{ session.id }}">Vollständige Historie laden</button>
  <p id="fetch-status"></p>
</div>
{% else %}
```

Write `backend/app/static/app.js`:

```js
document.addEventListener("DOMContentLoaded", () => {
  const button = document.getElementById("fetch-full");
  if (!button) return;
  button.addEventListener("click", async () => {
    const sessionId = button.dataset.sessionId;
    const status = document.getElementById("fetch-status");
    button.disabled = true;
    status.textContent = "Wird geladen...";
    const res = await fetch(`/chats/${sessionId}/fetch-full`, { method: "POST" });
    const { job_id } = await res.json();
    const poll = async () => {
      const statusRes = await fetch(`/chats/${sessionId}/status?job_id=${job_id}`);
      const data = await statusRes.json();
      if (data.status === "done" || data.status === "failed") {
        status.textContent = data.status === "done" ? "Fertig, lade neu..." : "Fehlgeschlagen.";
        if (data.status === "done") location.reload();
      } else {
        setTimeout(poll, 3000);
      }
    };
    poll();
  });
});
```

- [ ] **Step 6: Manually verify the button renders** (no automated browser test in this plan — see spec's Testing Strategy)

Run: `cd backend && DATABASE_PATH=/tmp/manual.db API_KEY=devkey SECRET_KEY=devsecret .venv/bin/uvicorn app.main:app --reload`
Then open `http://localhost:8000/login`, log in with `devkey`, confirm the list/detail pages render without errors.

- [ ] **Step 7: Commit**

```bash
git add backend/app/main.py backend/app/templates/detail.html backend/app/static/app.js backend/tests/test_detail_and_jobs.py
git commit -m "feat(backend): fetch-full job lifecycle wired end-to-end with UI polling"
```

---

## Task 7: PWA polish — manifest, base styling already done, README

**Files:**
- Modify: `backend/app/static/manifest.json` (already valid from Task 4 — verify and document)
- Create: `backend/README.md`

**Interfaces:**
- Consumes: nothing new.
- Produces: developer-facing run/deploy instructions used by Task 8's Dockerfile task.

- [ ] **Step 1: Write `backend/README.md`**

```markdown
# AI Remote Chat Viewer — Backend

## Local development

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    DATABASE_PATH=./dev.db API_KEY=devkey SECRET_KEY=devsecret .venv/bin/uvicorn app.main:app --reload

Visit http://localhost:8000/login and log in with `devkey`.

## Environment variables

- `API_KEY` — shared secret; the agent sends it as `Authorization: Bearer <API_KEY>`, the browser
  sends it once via the `/login` form and gets a signed session cookie back.
- `SECRET_KEY` — cookie-signing secret for the browser session (`starlette.SessionMiddleware`).
- `DATABASE_PATH` — path to the SQLite file (defaults to `app.db` in the working directory).

## Tests

    .venv/bin/pytest -v

## PWA install

Visiting the site on iPhone Safari and choosing "Add to Home Screen" installs it as a standalone
app using `static/manifest.json`. No custom icon is configured for v1 — Safari falls back to a
generic icon; add an `icons` array to the manifest later if desired.
```

- [ ] **Step 2: Verify manifest is valid JSON**

Run: `python3 -c "import json; json.load(open('backend/app/static/manifest.json'))"`
Expected: no output (valid JSON, no exception)

- [ ] **Step 3: Commit**

```bash
git add backend/README.md
git commit -m "docs(backend): local dev, env vars, and PWA install notes"
```

---

## Task 8: Dockerfile, docker-compose, and deployment docs

**Files:**
- Create: `backend/Dockerfile`
- Create: `backend/.dockerignore`
- Create: `docker-compose.yml` (repo root)
- Create: `example.env` (repo root)
- Modify: `backend/README.md` (add Docker section)

**Interfaces:**
- Consumes: `backend/requirements.txt`, `backend/app/` (all prior tasks)
- Produces: a buildable, runnable Docker image, and a root `docker-compose.yml` + `example.env` that Task 16's `run.sh` drives directly (service name `backend`, env vars `API_KEY`/`SECRET_KEY`/`PORT`).

- [ ] **Step 1: Write `backend/.dockerignore`**

```
.venv
__pycache__
*.pyc
dev.db
tests
```

- [ ] **Step 2: Write `backend/Dockerfile`**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV DATABASE_PATH=/data/app.db
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 3: Build the image**

Run: `cd backend && docker build -t ai-remote-backend .`
Expected: build completes successfully

- [ ] **Step 4: Run the container and smoke-test it**

Run:

```bash
mkdir -p /tmp/ai-remote-data
docker run --rm -d --name ai-remote-smoke \
  -p 8000:8000 \
  -v /tmp/ai-remote-data:/data \
  -e API_KEY=devkey -e SECRET_KEY=devsecret \
  ai-remote-backend
sleep 2
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/login
docker stop ai-remote-smoke
```

Expected: prints `200`

- [ ] **Step 5: Add Docker/Plesk deployment section to `backend/README.md`**

```markdown
## Deployment (Plesk Docker, subdomain `your-domain.example.com`)

1. Build and push/import the image: `docker build -t ai-remote-backend .`
2. In Plesk's Docker extension, create a container from `ai-remote-backend`:
   - Port mapping: container `8000` → host port of your choice.
   - Volume: host `./data` → container `/data` (persists the SQLite file across
     container recreation/updates).
   - Environment: `API_KEY`, `SECRET_KEY` (generate both with `openssl rand -hex 32`).
3. Bind the subdomain `your-domain.example.com` to the container's host port and
   enable Plesk's Let's Encrypt SSL for it.

For local development and testing (not production), see the repo-root `run.sh`
(Task 16) instead — it drives `docker-compose.yml` directly.
```

- [ ] **Step 6: Write root `docker-compose.yml`**

```yaml
services:
  backend:
    build: ./backend
    ports:
      - "${PORT:-8000}:8000"
    volumes:
      - ./data:/data
    environment:
      - API_KEY=${API_KEY}
      - SECRET_KEY=${SECRET_KEY}
      - DATABASE_PATH=/data/app.db
    restart: unless-stopped
```

- [ ] **Step 7: Write root `example.env`**

```
# Copy to .env and fill in real values (never commit .env itself).
API_KEY=
SECRET_KEY=
PORT=8000

# Used by run.sh to point a locally-run agent cycle at this container.
AI_REMOTE_BACKEND_URL=http://localhost:8000

# Used by build-and-push.sh / deploy-production-scp.sh (Task 16) — fill in
# before using those; not needed for local `run.sh` usage.
REGISTRY=
SSH_HOST=
SSH_PATH=/opt/ai-remote-backend
```

- [ ] **Step 8: Verify `docker compose config` accepts the compose file**

Run: `cd /Users/yourname/source/ai-remote-management/.claude/worktrees/ai-remote-plan-a && cp example.env .env && sed -i '' 's/^API_KEY=$/API_KEY=devkey/; s/^SECRET_KEY=$/SECRET_KEY=devsecret/' .env && docker compose config`
Expected: prints the resolved compose config without error (confirms `${VAR}` substitution works)

- [ ] **Step 9: Commit**

```bash
git add backend/Dockerfile backend/.dockerignore backend/README.md docker-compose.yml example.env
git commit -m "feat(backend): Dockerfile, docker-compose, and deployment instructions"
```

Do not commit the `.env` file created in Step 8 — it's a local scratch file for verification only; add `.env` to the repo-root `.gitignore` if it isn't already ignored (check with `git check-ignore -q .env`; if that fails, append `.env` to `.gitignore` and commit that too).

---

## Task 9: Local agent scaffold + Claude Code indexer

**Files:**
- Create: `agent/requirements.txt`
- Create: `agent/agent/__init__.py` (empty)
- Create: `agent/agent/claude_code_source.py`
- Create: `agent/tests/fixtures/sample_session.jsonl`
- Create: `agent/tests/test_claude_code_source.py`

**Interfaces:**
- Produces: `claude_code_source.list_claude_code_sessions(projects_dir: Path = CLAUDE_PROJECTS_DIR) -> list[dict]` and `claude_code_source.get_full_messages(raw_session_id: str, projects_dir: Path = CLAUDE_PROJECTS_DIR) -> list[dict]`. Each session dict has exactly the fields of the backend's `SessionIn` model (Task 3) plus a composite `id` of the form `claude-code:<uuid>`. Each message dict has exactly `{idx, role, timestamp, content}` — the same shape the backend's `MessageIn`/`JobCompleteRequest` expect.

This module is grounded in the real on-disk format, verified directly against this machine's
`~/.claude/projects/**/*.jsonl` files: event `type` values seen are `user`, `assistant`,
`ai-title`, `last-prompt`, `queue-operation`, `attachment`, `file-history-snapshot`. Only files
directly inside a project directory are session files — `subagents/*.jsonl` files are
intentionally excluded (they're internal sidechains, not something the user browses or resumes
directly).

- [ ] **Step 1: Write the failing test with a real fixture**

Create `agent/tests/fixtures/sample_session.jsonl` (mirrors real event shapes, fictional content):

```
{"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-08-01T10:00:00.000Z", "sessionId": "test-session-1"}
{"parentUuid": null, "isSidechain": false, "type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "What changed recently?"}]}, "uuid": "u1", "timestamp": "2026-08-01T10:00:00.100Z", "entrypoint": "claude-vscode", "cwd": "/Users/jan/source/demo", "sessionId": "test-session-1"}
{"parentUuid": "u1", "isSidechain": false, "type": "assistant", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "let me check"}]}, "timestamp": "2026-08-01T10:00:01.000Z", "sessionId": "test-session-1"}
{"parentUuid": "u1", "isSidechain": false, "type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "git log"}}]}, "timestamp": "2026-08-01T10:00:02.000Z", "sessionId": "test-session-1"}
{"parentUuid": "u1", "isSidechain": false, "type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Three commits landed yesterday."}]}, "timestamp": "2026-08-01T10:00:03.000Z", "sessionId": "test-session-1"}
{"type": "ai-title", "sessionId": "test-session-1", "aiTitle": "Recent project changes"}
{"type": "last-prompt", "lastPrompt": "What changed recently?", "sessionId": "test-session-1"}
```

Create `agent/tests/test_claude_code_source.py`:

```python
import shutil
from pathlib import Path

import pytest

from agent import claude_code_source

FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


@pytest.fixture
def projects_dir(tmp_path):
    project_dir = tmp_path / "-Users-jan-source-demo"
    project_dir.mkdir()
    shutil.copy(FIXTURE, project_dir / "test-session-1.jsonl")
    subagents_dir = project_dir / "subagents"
    subagents_dir.mkdir()
    shutil.copy(FIXTURE, subagents_dir / "agent-should-be-ignored.jsonl")
    return tmp_path


def test_list_claude_code_sessions_extracts_title_and_preview(projects_dir):
    sessions = claude_code_source.list_claude_code_sessions(projects_dir)
    assert len(sessions) == 1  # subagent file excluded

    session = sessions[0]
    assert session["id"] == "claude-code:test-session-1"
    assert session["tool"] == "claude-code"
    assert session["entrypoint"] == "claude-vscode"
    assert session["project_path"] == "/Users/jan/source/demo"
    assert session["title"] == "Recent project changes"
    assert session["last_message_preview"] == "Three commits landed yesterday."
    assert session["message_count"] == 4  # 1 user + 3 assistant events
    assert session["created_at"] == "2026-08-01T10:00:00.100Z"
    assert session["last_updated_at"] == "2026-08-01T10:00:03.000Z"


def test_get_full_messages_skips_events_without_text(projects_dir):
    messages = claude_code_source.get_full_messages("test-session-1", projects_dir)
    assert [m["content"] for m in messages] == [
        "What changed recently?",
        "Three commits landed yesterday.",
    ]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["idx"] == 0
    assert messages[1]["idx"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd agent && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pytest tests/test_claude_code_source.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent'`

- [ ] **Step 3: Write `agent/requirements.txt`**

```
httpx>=0.27
pytest>=8.3
```

- [ ] **Step 4: Create `agent/agent/__init__.py`** (empty file)

- [ ] **Step 5: Write `agent/agent/claude_code_source.py`**

```python
import json
from pathlib import Path

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def list_claude_code_sessions(projects_dir: Path = CLAUDE_PROJECTS_DIR) -> list[dict]:
    sessions = []
    if not projects_dir.exists():
        return sessions
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):  # non-recursive: skips subagents/*.jsonl
            session = _parse_session_file(jsonl_file)
            if session:
                sessions.append(session)
    return sessions


def _extract_text(message: dict) -> str:
    parts = []
    for block in message.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts).strip()


def _parse_session_file(path: Path) -> dict | None:
    session_id = path.stem
    ai_title = None
    last_prompt = None
    first_timestamp = None
    last_timestamp = None
    last_assistant_text = None
    last_user_text = None
    cwd = ""
    entrypoint = ""
    message_count = 0

    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")
            timestamp = event.get("timestamp")
            if timestamp:
                if first_timestamp is None:
                    first_timestamp = timestamp
                last_timestamp = timestamp

            if event_type == "ai-title":
                ai_title = event.get("aiTitle")
            elif event_type == "last-prompt":
                last_prompt = event.get("lastPrompt")
            elif event_type == "user":
                message_count += 1
                cwd = event.get("cwd", cwd)
                entrypoint = event.get("entrypoint", entrypoint)
                text = _extract_text(event.get("message", {}))
                if text:
                    last_user_text = text
            elif event_type == "assistant":
                message_count += 1
                text = _extract_text(event.get("message", {}))
                if text:
                    last_assistant_text = text

    if first_timestamp is None:
        return None

    title = ai_title or (last_prompt[:80] if last_prompt else "(untitled)")
    preview = last_assistant_text or last_user_text or ""

    return {
        "id": f"claude-code:{session_id}",
        "tool": "claude-code",
        "entrypoint": entrypoint or "cli",
        "project_path": cwd,
        "title": title,
        "created_at": first_timestamp,
        "last_updated_at": last_timestamp,
        "message_count": message_count,
        "last_message_preview": preview[:500],
        "status": "idle",
    }


def _find_session_file(raw_session_id: str, projects_dir: Path) -> Path | None:
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{raw_session_id}.jsonl"
        if candidate.exists():
            return candidate
    return None


def get_full_messages(raw_session_id: str, projects_dir: Path = CLAUDE_PROJECTS_DIR) -> list[dict]:
    path = _find_session_file(raw_session_id, projects_dir)
    if path is None:
        return []
    messages = []
    idx = 0
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") not in ("user", "assistant"):
                continue
            text = _extract_text(event.get("message", {}))
            if not text:
                continue
            messages.append(
                {"idx": idx, "role": event["type"], "timestamp": event.get("timestamp", ""), "content": text}
            )
            idx += 1
    return messages
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd agent && .venv/bin/pytest tests/test_claude_code_source.py -v`
Expected: PASS (2 tests)

- [ ] **Step 7: Commit**

```bash
git add agent/requirements.txt agent/agent/__init__.py agent/agent/claude_code_source.py agent/tests/fixtures/sample_session.jsonl agent/tests/test_claude_code_source.py
git commit -m "feat(agent): Claude Code session indexer"
```

---

## Task 10: Local agent — Cursor indexer

**Files:**
- Create: `agent/agent/cursor_source.py`
- Create: `agent/tests/test_cursor_source.py`

**Interfaces:**
- Produces: `cursor_source.list_cursor_sessions(search_db_path=CONVERSATION_SEARCH_DB) -> list[dict]`, `cursor_source.enrich_with_messages(sessions: list[dict], state_db_path=STATE_DB) -> None` (mutates in place), `cursor_source.get_full_messages(composer_id: str, state_db_path=STATE_DB) -> list[dict]`, `cursor_source.resolve_workspace_path(workspace_id: str, workspace_storage=CURSOR_WORKSPACE_STORAGE) -> str | None`. Same session/message dict shapes as `claude_code_source` (Task 9).

Grounded in schemas verified directly against this machine's real Cursor install:
- `conversation-search.db` → `conversations(source, scope, id, title, updated_at [epoch ms], is_archived)` — Cursor's own conversation search index; `source='local'` rows are this machine's sessions.
- `state.vscdb` → `composerHeaders(composerId, workspaceId, ...)` and `cursorDiskKV` rows keyed `bubbleId:<composerId>:<bubbleId>` whose JSON value has `type` (confirmed: `1` = user, `2` = assistant, verified against a real conversation on this machine — the single `type=1` bubble was the earliest message with real text; all later `type=2` bubbles were the assistant's turns, several with empty `text` for tool-only steps), `createdAt` (ISO8601 string), `text`.
- `workspaceStorage/<workspaceId>/workspace.json` → `{"folder": "file:///abs/path"}`, resolving a composer's project path.
- **Known limitation** (already called out in the spec): this is Cursor's undocumented internal format; if a future Cursor version changes these tables/keys, this module needs re-verification against a real DB — the same defensive pattern (never crash on unexpected JSON, skip and move on) should be preserved.

- [ ] **Step 1: Write the failing test**

Create `agent/tests/test_cursor_source.py`:

```python
import json
import sqlite3
from pathlib import Path

import pytest

from agent import cursor_source


@pytest.fixture
def search_db(tmp_path):
    path = tmp_path / "conversation-search.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE conversations (
            fts_rowid INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            scope TEXT NOT NULL,
            id TEXT NOT NULL,
            title TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            is_archived INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO conversations VALUES (1, 'local', '', 'composer-1', 'Demo Cursor chat', 1754040000000, 0)"
    )
    conn.execute(
        "INSERT INTO conversations VALUES (2, 'local', '', 'composer-archived', 'Old', 1700000000000, 1)"
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def state_db(tmp_path):
    path = tmp_path / "state.vscdb"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE composerHeaders (composerId TEXT PRIMARY KEY, workspaceId TEXT)")
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO composerHeaders VALUES ('composer-1', 'workspace-1')")
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:composer-1:b1", json.dumps({"type": 1, "createdAt": "2026-08-01T10:00:00.000Z", "text": "Hi there"})),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:composer-1:b2", json.dumps({"type": 2, "createdAt": "2026-08-01T10:00:05.000Z", "text": ""})),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:composer-1:b3", json.dumps({"type": 2, "createdAt": "2026-08-01T10:00:10.000Z", "text": "Sure, here you go."})),
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def workspace_storage(tmp_path):
    workspace_dir = tmp_path / "workspaceStorage" / "workspace-1"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "workspace.json").write_text(json.dumps({"folder": "file:///Users/jan/source/demo"}))
    return tmp_path / "workspaceStorage"


def test_list_cursor_sessions_excludes_archived(search_db):
    sessions = cursor_source.list_cursor_sessions(search_db)
    assert len(sessions) == 1
    assert sessions[0]["id"] == "cursor:composer-1"
    assert sessions[0]["title"] == "Demo Cursor chat"
    assert sessions[0]["tool"] == "cursor"


def test_enrich_with_messages_fills_preview_and_project_path(search_db, state_db, workspace_storage, monkeypatch):
    sessions = cursor_source.list_cursor_sessions(search_db)
    monkeypatch.setattr(cursor_source, "CURSOR_WORKSPACE_STORAGE", workspace_storage)
    cursor_source.enrich_with_messages(sessions, state_db)

    assert sessions[0]["message_count"] == 2  # the empty-text bubble is skipped
    assert sessions[0]["last_message_preview"] == "Sure, here you go."
    assert sessions[0]["project_path"] == "/Users/jan/source/demo"


def test_get_full_messages_orders_by_timestamp(state_db):
    messages = cursor_source.get_full_messages("composer-1", state_db)
    assert [m["content"] for m in messages] == ["Hi there", "Sure, here you go."]
    assert [m["role"] for m in messages] == ["user", "assistant"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd agent && .venv/bin/pytest tests/test_cursor_source.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.cursor_source'`

- [ ] **Step 3: Write `agent/agent/cursor_source.py`**

```python
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

CURSOR_GLOBAL_STORAGE = Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage"
CONVERSATION_SEARCH_DB = CURSOR_GLOBAL_STORAGE / "conversation-search.db"
STATE_DB = CURSOR_GLOBAL_STORAGE / "state.vscdb"
CURSOR_WORKSPACE_STORAGE = Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "workspaceStorage"


def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _read_only_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _query_with_retry(conn: sqlite3.Connection, sql: str, params: tuple = (), retries: int = 3, delay: float = 0.2) -> list:
    """Cursor may hold a brief write lock on its own DB (WAL checkpoints) while running.
    Retry a couple of times before giving up, rather than crashing the whole sync cycle."""
    for attempt in range(retries):
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            if attempt == retries - 1:
                raise
            time.sleep(delay)
    return []


def list_cursor_sessions(search_db_path: Path = CONVERSATION_SEARCH_DB) -> list[dict]:
    if not search_db_path.exists():
        return []
    conn = _read_only_connection(search_db_path)
    try:
        rows = _query_with_retry(
            conn, "SELECT id, title, updated_at, is_archived FROM conversations WHERE source = 'local'"
        )
    finally:
        conn.close()

    sessions = []
    for raw_id, title, updated_at_ms, is_archived in rows:
        if is_archived:
            continue
        updated_at = _ms_to_iso(updated_at_ms)
        sessions.append(
            {
                "id": f"cursor:{raw_id}",
                "tool": "cursor",
                "entrypoint": "cursor",
                "project_path": "",
                "title": title or "(untitled)",
                "created_at": updated_at,  # Cursor's search index only tracks updated_at
                "last_updated_at": updated_at,
                "message_count": 0,
                "last_message_preview": "",
                "status": "idle",
            }
        )
    return sessions


def _load_bubbles(conn: sqlite3.Connection, composer_id: str) -> list[dict]:
    rows = _query_with_retry(
        conn, "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?", (f"bubbleId:{composer_id}:%",)
    )
    bubbles = []
    for key, raw_value in rows:
        try:
            data = json.loads(raw_value)
        except (json.JSONDecodeError, TypeError):
            continue
        text = data.get("text") or ""
        if not text:
            continue
        bubbles.append(
            {
                "key": key,
                "role": "user" if data.get("type") == 1 else "assistant",
                "timestamp": data.get("createdAt", ""),
                "content": text,
            }
        )
    bubbles.sort(key=lambda b: (b.get("timestamp") or "", b["key"]))
    return bubbles


def _load_workspace_id(conn: sqlite3.Connection, composer_id: str) -> str | None:
    rows = _query_with_retry(
        conn, "SELECT workspaceId FROM composerHeaders WHERE composerId = ?", (composer_id,)
    )
    return rows[0][0] if rows else None


def resolve_workspace_path(workspace_id: str, workspace_storage: Path = CURSOR_WORKSPACE_STORAGE) -> str | None:
    workspace_json = workspace_storage / workspace_id / "workspace.json"
    if not workspace_json.exists():
        return None
    try:
        data = json.loads(workspace_json.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    folder_uri = data.get("folder")
    if not folder_uri:
        return None
    return unquote(urlparse(folder_uri).path)


def enrich_with_messages(sessions: list[dict], state_db_path: Path = STATE_DB) -> None:
    if not state_db_path.exists():
        return
    conn = _read_only_connection(state_db_path)
    try:
        for session in sessions:
            composer_id = session["id"].split(":", 1)[1]
            bubbles = _load_bubbles(conn, composer_id)
            if bubbles:
                session["message_count"] = len(bubbles)
                session["last_message_preview"] = bubbles[-1]["content"][:500]
            workspace_id = _load_workspace_id(conn, composer_id)
            if workspace_id:
                session["project_path"] = resolve_workspace_path(workspace_id) or ""
    finally:
        conn.close()


def get_full_messages(composer_id: str, state_db_path: Path = STATE_DB) -> list[dict]:
    if not state_db_path.exists():
        return []
    conn = _read_only_connection(state_db_path)
    try:
        bubbles = _load_bubbles(conn, composer_id)
    finally:
        conn.close()
    return [
        {"idx": i, "role": b["role"], "timestamp": b["timestamp"], "content": b["content"]}
        for i, b in enumerate(bubbles)
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd agent && .venv/bin/pytest tests/test_cursor_source.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add agent/agent/cursor_source.py agent/tests/test_cursor_source.py
git commit -m "feat(agent): Cursor session indexer using conversation-search.db + state.vscdb"
```

---

## Task 11: Local agent — sync-cursor persistence and delta computation

**Files:**
- Create: `agent/agent/state.py`
- Create: `agent/tests/test_state.py`

**Interfaces:**
- Consumes: nothing (pure functions + a JSON file on disk)
- Produces: `state.load_synced_ids(state_path) -> dict[str, str]`, `state.save_synced_ids(synced: dict, state_path) -> None`, `state.compute_deltas(sessions: list[dict], synced: dict) -> list[dict]`. Used by `main.py` (Task 14) to decide what to push each cycle.

- [ ] **Step 1: Write the failing test**

Create `agent/tests/test_state.py`:

```python
from agent import state


def test_load_synced_ids_missing_file_returns_empty_dict(tmp_path):
    assert state.load_synced_ids(tmp_path / "missing.json") == {}


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "sync_state.json"
    state.save_synced_ids({"claude-code:abc": "2026-08-01T10:00:00Z"}, path)
    assert state.load_synced_ids(path) == {"claude-code:abc": "2026-08-01T10:00:00Z"}


def test_compute_deltas_returns_new_and_changed_sessions_only():
    sessions = [
        {"id": "a", "last_updated_at": "t1"},
        {"id": "b", "last_updated_at": "t2"},
        {"id": "c", "last_updated_at": "t3"},
    ]
    synced = {"a": "t1", "b": "old-t2"}  # a unchanged, b changed, c new
    deltas = state.compute_deltas(sessions, synced)
    assert {s["id"] for s in deltas} == {"b", "c"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd agent && .venv/bin/pytest tests/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.state'`

- [ ] **Step 3: Write `agent/agent/state.py`**

```python
import json
from pathlib import Path

DEFAULT_STATE_PATH = Path.home() / ".ai-remote-agent" / "sync_state.json"


def load_synced_ids(state_path: Path = DEFAULT_STATE_PATH) -> dict:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_synced_ids(synced: dict, state_path: Path = DEFAULT_STATE_PATH) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(synced))


def compute_deltas(sessions: list[dict], synced: dict) -> list[dict]:
    return [s for s in sessions if synced.get(s["id"]) != s["last_updated_at"]]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd agent && .venv/bin/pytest tests/test_state.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add agent/agent/state.py agent/tests/test_state.py
git commit -m "feat(agent): local sync-cursor persistence and delta computation"
```

---

## Task 12: Local agent — uploader (push sync deltas to the backend)

**Files:**
- Create: `agent/agent/uploader.py`
- Create: `agent/tests/test_uploader_and_jobs.py`

**Interfaces:**
- Consumes: nothing new (takes a list of session dicts shaped like Task 9/10's output)
- Produces: `uploader.push_sync(base_url: str, api_key: str, sessions: list[dict], client: httpx.Client) -> bool` — returns `True` on success (2xx), `False` on any `httpx.HTTPError` so the caller (Task 14) knows not to advance the sync cursor.

- [ ] **Step 1: Write the failing test**

Create `agent/tests/test_uploader_and_jobs.py`:

```python
import json

import httpx

from agent import uploader


def test_push_sync_returns_true_on_success():
    def handler(request):
        assert request.headers["authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {"sessions": [{"id": "a"}]}
        return httpx.Response(200, json={"received": 1})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = uploader.push_sync("http://backend.example", "test-key", [{"id": "a"}], client=client)
    assert result is True


def test_push_sync_returns_false_on_http_error():
    def handler(request):
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = uploader.push_sync("http://backend.example", "test-key", [{"id": "a"}], client=client)
    assert result is False


def test_push_sync_skips_request_when_no_deltas():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    result = uploader.push_sync("http://backend.example", "test-key", [], client=client)
    assert result is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd agent && .venv/bin/pytest tests/test_uploader_and_jobs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.uploader'`

- [ ] **Step 3: Write `agent/agent/uploader.py`**

```python
import httpx


def push_sync(base_url: str, api_key: str, sessions: list[dict], client: httpx.Client) -> bool:
    if not sessions:
        return True
    try:
        response = client.post(
            f"{base_url}/sync/index",
            json={"sessions": sessions},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        return True
    except httpx.HTTPError:
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd agent && .venv/bin/pytest tests/test_uploader_and_jobs.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add agent/agent/uploader.py agent/tests/test_uploader_and_jobs.py
git commit -m "feat(agent): push sync deltas to the backend"
```

---

## Task 13: Local agent — job polling and fetch_full execution

**Files:**
- Create: `agent/agent/jobs.py`
- Create: `agent/agent/executor.py`
- Modify: `agent/tests/test_uploader_and_jobs.py`

**Interfaces:**
- Consumes: `claude_code_source.get_full_messages`, `cursor_source.get_full_messages` (Tasks 9 & 10)
- Produces: `jobs.fetch_pending_jobs(base_url, api_key, client) -> list[dict]`, `jobs.report_job_result(base_url, api_key, job_id, status, client, result_text="", messages=None) -> None`, `executor.execute_fetch_full(job: dict) -> dict` returning `{"status": "done"|"failed", "result_text": str, "messages": list[dict]}` — consumed by Task 14's main loop.

- [ ] **Step 1: Write the failing test**

Append to `agent/tests/test_uploader_and_jobs.py`:

```python
from agent import executor, jobs


def test_fetch_pending_jobs_parses_response():
    def handler(request):
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"jobs": [{"id": "j1", "type": "fetch_full", "target": "claude-code:abc"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = jobs.fetch_pending_jobs("http://backend.example", "test-key", client)
    assert result == [{"id": "j1", "type": "fetch_full", "target": "claude-code:abc"}]


def test_report_job_result_sends_expected_body():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    jobs.report_job_result(
        "http://backend.example", "test-key", "j1", "done", client,
        result_text="", messages=[{"idx": 0, "role": "user", "timestamp": "t", "content": "hi"}],
    )
    assert captured["body"] == {
        "status": "done",
        "result_text": "",
        "messages": [{"idx": 0, "role": "user", "timestamp": "t", "content": "hi"}],
    }


def test_execute_fetch_full_dispatches_to_claude_code_source(monkeypatch):
    monkeypatch.setattr(
        "agent.claude_code_source.get_full_messages",
        lambda raw_id: [{"idx": 0, "role": "user", "timestamp": "t", "content": "hi"}],
    )
    job = {"id": "j1", "type": "fetch_full", "target": "claude-code:abc"}
    result = executor.execute_fetch_full(job)
    assert result["status"] == "done"
    assert result["messages"] == [{"idx": 0, "role": "user", "timestamp": "t", "content": "hi"}]


def test_execute_fetch_full_fails_when_no_messages_found(monkeypatch):
    monkeypatch.setattr("agent.cursor_source.get_full_messages", lambda composer_id: [])
    job = {"id": "j1", "type": "fetch_full", "target": "cursor:missing"}
    result = executor.execute_fetch_full(job)
    assert result["status"] == "failed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd agent && .venv/bin/pytest tests/test_uploader_and_jobs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.jobs'`

- [ ] **Step 3: Write `agent/agent/jobs.py`**

```python
import httpx


def fetch_pending_jobs(base_url: str, api_key: str, client: httpx.Client) -> list[dict]:
    response = client.get(f"{base_url}/jobs/pending", headers={"Authorization": f"Bearer {api_key}"})
    response.raise_for_status()
    return response.json()["jobs"]


def report_job_result(
    base_url: str,
    api_key: str,
    job_id: str,
    status: str,
    client: httpx.Client,
    result_text: str = "",
    messages: list[dict] | None = None,
) -> None:
    body = {"status": status, "result_text": result_text, "messages": messages or []}
    response = client.post(
        f"{base_url}/jobs/{job_id}/complete",
        json=body,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    response.raise_for_status()
```

- [ ] **Step 4: Write `agent/agent/executor.py`**

```python
from . import claude_code_source, cursor_source


def execute_fetch_full(job: dict) -> dict:
    tool, raw_id = job["target"].split(":", 1)
    if tool == "claude-code":
        messages = claude_code_source.get_full_messages(raw_id)
    elif tool == "cursor":
        messages = cursor_source.get_full_messages(raw_id)
    else:
        return {"status": "failed", "result_text": f"unknown tool: {tool}", "messages": []}

    if not messages:
        return {"status": "failed", "result_text": "no messages found for session", "messages": []}
    return {"status": "done", "result_text": "", "messages": messages}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd agent && .venv/bin/pytest tests/test_uploader_and_jobs.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add agent/agent/jobs.py agent/agent/executor.py agent/tests/test_uploader_and_jobs.py
git commit -m "feat(agent): job polling and fetch_full execution"
```

---

## Task 14: Local agent — main loop, config, and launchd install

**Files:**
- Create: `agent/agent/config.py`
- Create: `agent/agent/main.py`
- Create: `agent/launchd/com.example.ai-remote-agent.plist`
- Modify: `agent/README.md` (create if absent)

**Interfaces:**
- Consumes: every module from Tasks 9–13.
- Produces: `agent.main.run_cycle(config, client) -> None` (one iteration — the piece under test) and `agent.main.main()` (the real infinite loop, exercised manually, not under pytest).

- [ ] **Step 1: Write the failing test**

Create `agent/tests/test_main_cycle.py`:

```python
from agent import main, state


def test_run_cycle_pushes_deltas_and_executes_pending_jobs(tmp_path, monkeypatch):
    calls = []

    monkeypatch.setattr(
        "agent.claude_code_source.list_claude_code_sessions",
        lambda: [{"id": "claude-code:abc", "last_updated_at": "t1"}],
    )
    monkeypatch.setattr("agent.cursor_source.list_cursor_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.enrich_with_messages", lambda sessions: None)

    def fake_push_sync(base_url, api_key, sessions, client):
        calls.append(("push", sessions))
        return True

    def fake_fetch_pending_jobs(base_url, api_key, client):
        return [{"id": "j1", "type": "fetch_full", "target": "claude-code:abc"}]

    def fake_execute_fetch_full(job):
        return {"status": "done", "result_text": "", "messages": []}

    def fake_report_job_result(base_url, api_key, job_id, status, client, result_text="", messages=None):
        calls.append(("report", job_id, status))

    monkeypatch.setattr("agent.uploader.push_sync", fake_push_sync)
    monkeypatch.setattr("agent.jobs.fetch_pending_jobs", fake_fetch_pending_jobs)
    monkeypatch.setattr("agent.jobs.report_job_result", fake_report_job_result)
    monkeypatch.setattr("agent.executor.execute_fetch_full", fake_execute_fetch_full)

    config = main.Config(
        backend_url="http://backend.example",
        api_key="test-key",
        state_path=tmp_path / "sync_state.json",
        interval_seconds=60,
    )
    main.run_cycle(config, client=None)

    assert ("push", [{"id": "claude-code:abc", "last_updated_at": "t1"}]) in calls
    assert ("report", "j1", "done") in calls
    assert state.load_synced_ids(config.state_path) == {"claude-code:abc": "t1"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd agent && .venv/bin/pytest tests/test_main_cycle.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.main'`

- [ ] **Step 3: Write `agent/agent/config.py`**

```python
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    backend_url: str
    api_key: str
    state_path: Path
    interval_seconds: int = 60


def load_config() -> Config:
    return Config(
        backend_url=os.environ["AI_REMOTE_BACKEND_URL"].rstrip("/"),
        api_key=os.environ["AI_REMOTE_API_KEY"],
        state_path=Path(
            os.environ.get("AI_REMOTE_STATE_PATH", str(Path.home() / ".ai-remote-agent" / "sync_state.json"))
        ),
        interval_seconds=int(os.environ.get("AI_REMOTE_INTERVAL_SECONDS", "60")),
    )
```

- [ ] **Step 4: Write `agent/agent/main.py`**

```python
import time

import httpx

from . import claude_code_source, cursor_source, executor, jobs, state, uploader
from .config import Config, load_config


def run_cycle(config: Config, client: httpx.Client) -> None:
    claude_sessions = claude_code_source.list_claude_code_sessions()
    cursor_sessions = cursor_source.list_cursor_sessions()
    cursor_source.enrich_with_messages(cursor_sessions)
    all_sessions = claude_sessions + cursor_sessions

    synced = state.load_synced_ids(config.state_path)
    deltas = state.compute_deltas(all_sessions, synced)
    if uploader.push_sync(config.backend_url, config.api_key, deltas, client=client):
        for s in deltas:
            synced[s["id"]] = s["last_updated_at"]
        state.save_synced_ids(synced, config.state_path)

    try:
        pending_jobs = jobs.fetch_pending_jobs(config.backend_url, config.api_key, client)
    except httpx.HTTPError:
        pending_jobs = []

    for job in pending_jobs:
        if job["type"] == "fetch_full":
            result = executor.execute_fetch_full(job)
        else:
            result = {"status": "failed", "result_text": f"job type {job['type']} not supported in this version"}
        jobs.report_job_result(
            config.backend_url,
            config.api_key,
            job["id"],
            result["status"],
            client,
            result.get("result_text", ""),
            result.get("messages"),
        )


def main() -> None:
    config = load_config()
    with httpx.Client(timeout=10) as client:
        while True:
            run_cycle(config, client)
            time.sleep(config.interval_seconds)


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd agent && .venv/bin/pytest tests/test_main_cycle.py -v`
Expected: PASS

- [ ] **Step 6: Run the full agent test suite**

Run: `cd agent && .venv/bin/pytest -v`
Expected: PASS (all tests across Tasks 9–14)

- [ ] **Step 7: Write the launchd plist**

Create `agent/launchd/com.example.ai-remote-agent.plist` (paths are placeholders the install step below fills in with real absolute paths — this is a template file the user copies, not application code):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.example.ai-remote-agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/yourname/source/ai-remote-management/agent/.venv/bin/python3</string>
        <string>-m</string>
        <string>agent.main</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/yourname/source/ai-remote-management/agent</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>AI_REMOTE_BACKEND_URL</key>
        <string>https://your-domain.example.com</string>
        <key>AI_REMOTE_API_KEY</key>
        <string>REPLACE_WITH_REAL_API_KEY</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/yourname/Library/Logs/ai-remote-agent.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/yourname/Library/Logs/ai-remote-agent.error.log</string>
</dict>
</plist>
```

- [ ] **Step 8: Write `agent/README.md`**

```markdown
# AI Remote Chat Viewer — Local Agent

## Setup

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt

## Run once, manually, for testing

    AI_REMOTE_BACKEND_URL=https://your-domain.example.com \
    AI_REMOTE_API_KEY=<your key> \
    .venv/bin/python3 -c "from agent.main import load_config, run_cycle; import httpx; c=load_config(); \
    client=httpx.Client(timeout=10); run_cycle(c, client)"

## Install as a background service (launchd)

1. Edit `launchd/com.example.ai-remote-agent.plist`: set the real API key and confirm the
   `.venv` path matches where you cloned this repo.
2. Copy it into place and load it:

       cp launchd/com.example.ai-remote-agent.plist ~/Library/LaunchAgents/
       launchctl load ~/Library/LaunchAgents/com.example.ai-remote-agent.plist

3. Check it's running: `launchctl list | grep ai-remote-agent`
4. Logs: `~/Library/Logs/ai-remote-agent.log` / `.error.log`

## Tests

    .venv/bin/pytest -v
```

- [ ] **Step 9: Commit**

```bash
git add agent/agent/config.py agent/agent/main.py agent/tests/test_main_cycle.py agent/launchd agent/README.md
git commit -m "feat(agent): 60s sync+job loop, config, and launchd install"
```

---

## Task 15: End-to-end manual smoke test

**Files:** none (verification only)

**Interfaces:** none — this task exercises the whole system built in Tasks 1–14 together.

- [ ] **Step 1: Start the backend locally**

Run: `cd backend && DATABASE_PATH=/tmp/e2e.db API_KEY=e2ekey SECRET_KEY=e2esecret .venv/bin/uvicorn app.main:app --reload`

- [ ] **Step 2: Run one agent cycle by hand against a real (small) Claude Code project**

Have at least one real session under `~/.claude/projects/`. Run:

```bash
cd agent
AI_REMOTE_BACKEND_URL=http://localhost:8000 AI_REMOTE_API_KEY=e2ekey \
  .venv/bin/python3 -c "
from agent.main import load_config, run_cycle
import httpx
config = load_config()
with httpx.Client(timeout=10) as client:
    run_cycle(config, client)
"
```

Expected: no exceptions raised.

- [ ] **Step 3: Verify the session appears in the browser**

Open `http://localhost:8000/login`, log in with `e2ekey`, confirm at least one real session title
and preview appear on the list page, filter by tool and confirm the count changes correctly.

- [ ] **Step 4: Verify full-history load round-trips**

Open a session's detail page, click "Vollständige Historie laden", confirm the status message
updates and the page reloads showing the full message thread (not just the preview).

- [ ] **Step 5: Verify a Cursor session, if one exists on this machine**

Repeat steps 2–4 for a session whose `id` starts with `cursor:` — confirm title, project path,
and full-history load all work the same way as for Claude Code.

- [ ] **Step 6: Record the result**

If everything above works, Plan A is complete and ready for Plan B (remote commands). If any step
fails, note the exact failure (which step, what error) before starting Plan B — do not build
Plan B's command execution on an unverified sync foundation.

---

## Task 16: Standard root scripts (`run.sh`, `build-and-push.sh`, `build-and-deploy.sh`, `deploy-production-scp.sh`)

This project follows Jan's standard app-script contract
(`~/.claude/skills/create-jans-standard-app-scripts`). `run.sh` is the primary
deliverable of this task — it's what makes the whole app locally runnable and
testable against the user's real Claude Code / Cursor data without touching
production. The other three scripts complete the required contract for when
production deployment is actually wanted later; they are real and functional,
but their SSH/registry targets must be filled in via `.env` before use.

**Files:**
- Create: `run.sh` (repo root)
- Create: `build-and-push.sh` (repo root)
- Create: `build-and-deploy.sh` (repo root)
- Create: `deploy-production-scp.sh` (repo root)
- Modify: `.gitignore` (add `.env` if not already ignored)

**Interfaces:**
- Consumes: `docker-compose.yml` + `example.env` (Task 8), `agent/agent/main.py`'s `load_config`/`run_cycle` (Task 14), `backend/requirements.txt` + `agent/requirements.txt` (Tasks 1 & 9).
- Produces: the four scripts other humans/tools invoke — no other task depends on their internals.

- [ ] **Step 1: Check `.env` is ignored**

Run: `cd /Users/yourname/source/ai-remote-management/.claude/worktrees/ai-remote-plan-a && git check-ignore -q .env && echo ignored || echo "NOT ignored"`

If it prints "NOT ignored", append `.env` to the repo-root `.gitignore` and commit that first:

```bash
echo ".env" >> .gitignore
git add .gitignore
git commit -m "chore: ignore local .env"
```

- [ ] **Step 2: Write `run.sh`**

```bash
#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

REBUILD=false
RUN_TESTS=false

show_help() {
  cat <<'EOF'
Usage: ./run.sh [OPTIONS]

Run the AI Remote Chat Viewer backend locally via Docker Compose, then run
one local-agent sync cycle so real Claude Code / Cursor chats show up
immediately in the browser.

Options:
  --rebuild    Force a clean rebuild of the backend image before starting
  --test       Run the backend + agent pytest suites instead of starting the app
  --help, -h   Show this help
EOF
}

for arg in "$@"; do
  case "$arg" in
    --rebuild) REBUILD=true ;;
    --test) RUN_TESTS=true ;;
    --help|-h) show_help; exit 0 ;;
    *) echo "Unknown option: $arg"; show_help; exit 1 ;;
  esac
done

ensure_venv() {
  local dir="$1"
  if [ ! -d "$dir/.venv" ]; then
    echo -e "${BLUE}==> Creating venv in $dir${NC}"
    python3 -m venv "$dir/.venv"
  fi
  "$dir/.venv/bin/pip" install -q -r "$dir/requirements.txt"
}

if [ "$RUN_TESTS" = true ]; then
  echo -e "${BLUE}==> Backend tests${NC}"
  ensure_venv backend
  (cd backend && .venv/bin/pytest -v)

  echo -e "${BLUE}==> Agent tests${NC}"
  ensure_venv agent
  (cd agent && .venv/bin/pytest -v)

  echo -e "${GREEN}All tests passed.${NC}"
  exit 0
fi

if [ ! -f .env ]; then
  echo -e "${YELLOW}==> No .env found, creating one from example.env with generated secrets${NC}"
  cp example.env .env
  sed -i '' "s/^API_KEY=.*/API_KEY=$(openssl rand -hex 32)/" .env
  sed -i '' "s/^SECRET_KEY=.*/SECRET_KEY=$(openssl rand -hex 32)/" .env
fi

set -a
# shellcheck disable=SC1091
source .env
set +a
PORT="${PORT:-8000}"

if [ "$REBUILD" = true ]; then
  echo -e "${BLUE}==> Rebuilding backend image (no cache)${NC}"
  docker compose build --no-cache
fi

echo -e "${BLUE}==> Starting backend container${NC}"
docker compose up -d --build

echo -e "${BLUE}==> Waiting for backend to become healthy${NC}"
HEALTHY=false
for _ in $(seq 1 30); do
  if curl -s -o /dev/null -w "%{http_code}" "http://localhost:${PORT}/login" | grep -q "200"; then
    HEALTHY=true
    break
  fi
  sleep 1
done

if [ "$HEALTHY" != true ]; then
  echo -e "${RED}Backend did not become healthy within 30s. Check: docker compose logs backend${NC}"
  exit 1
fi

echo -e "${BLUE}==> Running one local-agent sync cycle against real Claude Code / Cursor data${NC}"
ensure_venv agent
AI_REMOTE_BACKEND_URL="http://localhost:${PORT}" \
AI_REMOTE_API_KEY="${API_KEY}" \
AI_REMOTE_STATE_PATH="$SCRIPT_DIR/agent/.local_run_state.json" \
agent/.venv/bin/python3 -c "
from agent.main import load_config, run_cycle
import httpx
config = load_config()
with httpx.Client(timeout=15) as client:
    run_cycle(config, client)
print('sync cycle complete')
" || echo -e "${YELLOW}Warning: local sync cycle failed — the app is still running; retry with: (cd agent && AI_REMOTE_BACKEND_URL=http://localhost:${PORT} AI_REMOTE_API_KEY=<key> .venv/bin/python3 -m agent.main)${NC}"

echo ""
echo -e "${BLUE}═══ VERIFICATION ═══${NC}"
if [ -n "$(docker compose ps -q backend 2>/dev/null)" ]; then
  echo -e "Container running:     ${GREEN}yes${NC}"
else
  echo -e "Container running:     ${RED}no${NC}"
fi
echo -n "Health check (/login): "
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:${PORT}/login"
if nc -z localhost "${PORT}" 2>/dev/null; then
  echo -e "Port reachable:        ${GREEN}yes${NC}"
else
  echo -e "Port reachable:        ${RED}no${NC}"
fi

echo ""
echo -e "${GREEN}┌─────────────────────────────┐${NC}"
echo -e "${GREEN}│         RUNNING ✓           │${NC}"
echo -e "${GREEN}└─────────────────────────────┘${NC}"
echo ""
echo "NEXT STEPS:"
echo "  1. Open http://localhost:${PORT}/login in your browser"
echo "  2. Log in with the API key from .env (API_KEY=${API_KEY})"
echo "  3. Your real Claude Code / Cursor sessions should already be listed"
echo ""
echo "URLS:"
echo "  Local:      http://localhost:${PORT}"
echo "  Production: https://your-domain.example.com (after deploy-production-scp.sh)"
echo ""
echo "COMMANDS:"
echo "  Logs:        docker compose logs -f backend"
echo "  Stop:        docker compose down"
echo "  Re-sync now: ./run.sh   (safe to re-run any time — does one sync cycle each call)"
echo "  Persistent background sync: see agent/README.md (launchd install)"
```

- [ ] **Step 3: Make it executable and check help**

Run: `chmod +x run.sh && ./run.sh --help`
Expected: prints usage and exits 0

- [ ] **Step 4: Write `build-and-push.sh`**

```bash
#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

NO_CACHE=false
SKIP_BUILD=false

show_help() {
  cat <<'EOF'
Usage: ./build-and-push.sh [OPTIONS]

Build the backend Docker image and push it to the registry configured in
.env (REGISTRY=...). Requires `docker login` to that registry beforehand.

Options:
  --no-cache    Build without Docker layer cache
  --skip-build  Skip the build step, push whatever image is already tagged locally
  --help, -h    Show this help
EOF
}

for arg in "$@"; do
  case "$arg" in
    --no-cache) NO_CACHE=true ;;
    --skip-build) SKIP_BUILD=true ;;
    --help|-h) show_help; exit 0 ;;
    *) echo "Unknown option: $arg"; show_help; exit 1 ;;
  esac
done

[ -f .env ] && { set -a; source .env; set +a; }
: "${REGISTRY:?Set REGISTRY in .env first, e.g. REGISTRY=ghcr.io/yourname}"
TAG="${TAG:-$(git rev-parse --short HEAD)}"
IMAGE="${REGISTRY}/ai-remote-backend:${TAG}"

if [ "$SKIP_BUILD" != true ]; then
  echo "==> Building ${IMAGE}"
  if [ "$NO_CACHE" = true ]; then
    docker build --no-cache -t "$IMAGE" backend
  else
    docker build -t "$IMAGE" backend
  fi
fi

echo "==> Pushing ${IMAGE}"
docker push "$IMAGE"
echo "Pushed ${IMAGE}"
```

- [ ] **Step 5: Write `deploy-production-scp.sh`**

```bash
#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

NO_CACHE=false
SKIP_BUILD=false

show_help() {
  cat <<'EOF'
Usage: ./deploy-production-scp.sh [OPTIONS]

Build the backend image, ship it to the production server via SCP (docker
save | ssh | docker load — no registry needed), write a minimal production
compose file there, and restart the container via SSH.

Requires SSH_HOST (and optionally SSH_PATH) set in .env, and passwordless
SSH access to that host.

Options:
  --no-cache    Build without Docker layer cache
  --skip-build  Skip building/shipping the image; only rewrite the compose
                file and restart whatever image is already loaded on the server
  --help, -h    Show this help
EOF
}

for arg in "$@"; do
  case "$arg" in
    --no-cache) NO_CACHE=true ;;
    --skip-build) SKIP_BUILD=true ;;
    --help|-h) show_help; exit 0 ;;
    *) echo "Unknown option: $arg"; show_help; exit 1 ;;
  esac
done

[ -f .env ] && { set -a; source .env; set +a; }
: "${SSH_HOST:?Set SSH_HOST in .env first, e.g. SSH_HOST=your-domain.example.com}"
SSH_PATH="${SSH_PATH:-/opt/ai-remote-backend}"
IMAGE_TAG="ai-remote-backend:$(git rev-parse --short HEAD)"

if [ "$SKIP_BUILD" != true ]; then
  echo "==> Building ${IMAGE_TAG}"
  if [ "$NO_CACHE" = true ]; then
    docker build --no-cache -t "$IMAGE_TAG" backend
  else
    docker build -t "$IMAGE_TAG" backend
  fi

  echo "==> Shipping image to ${SSH_HOST} via SCP"
  docker save "$IMAGE_TAG" | gzip | ssh "$SSH_HOST" "gunzip | docker load"
fi

echo "==> Writing production compose file on ${SSH_HOST}"
ssh "$SSH_HOST" "mkdir -p ${SSH_PATH}/data"
cat > /tmp/ai-remote-compose.prod.yml <<COMPOSE
services:
  backend:
    image: ${IMAGE_TAG}
    ports:
      - "\${PORT:-8000}:8000"
    volumes:
      - ./data:/data
    environment:
      - API_KEY=\${API_KEY}
      - SECRET_KEY=\${SECRET_KEY}
      - DATABASE_PATH=/data/app.db
    restart: unless-stopped
COMPOSE
scp /tmp/ai-remote-compose.prod.yml "${SSH_HOST}:${SSH_PATH}/docker-compose.yml"
scp .env "${SSH_HOST}:${SSH_PATH}/.env"
rm -f /tmp/ai-remote-compose.prod.yml

echo "==> Restarting container on ${SSH_HOST}"
ssh "$SSH_HOST" "cd ${SSH_PATH} && docker compose up -d"

echo "Deployed ${IMAGE_TAG} to ${SSH_HOST}:${SSH_PATH}"
echo "Verify: curl -s -o /dev/null -w '%{http_code}\n' https://your-domain.example.com/login"
```

- [ ] **Step 6: Write `build-and-deploy.sh`**

```bash
#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RUN_TESTS=false

show_help() {
  cat <<'EOF'
Usage: ./build-and-deploy.sh [OPTIONS]

Rebuild locally (optionally running tests first and aborting on failure),
then deploy to production via deploy-production-scp.sh.

Options:
  --test       Run ./run.sh --test first; abort the deploy if tests fail
  --help, -h   Show this help
EOF
}

for arg in "$@"; do
  case "$arg" in
    --test) RUN_TESTS=true ;;
    --help|-h) show_help; exit 0 ;;
    *) echo "Unknown option: $arg"; show_help; exit 1 ;;
  esac
done

if [ "$RUN_TESTS" = true ]; then
  echo "==> Running tests (must pass before deploy)"
  ./run.sh --test
fi

echo "==> Rebuilding locally"
./run.sh --rebuild

echo "==> Deploying to production"
./deploy-production-scp.sh
```

- [ ] **Step 7: Make all scripts executable and smoke-check `--help`**

Run:

```bash
chmod +x run.sh build-and-push.sh build-and-deploy.sh deploy-production-scp.sh
./run.sh --help
./build-and-push.sh --help
./build-and-deploy.sh --help
./deploy-production-scp.sh --help
```

Expected: each exits 0 and prints its usage, mentioning its required flags. `build-and-push.sh --help` and `deploy-production-scp.sh --help` must succeed even without `REGISTRY`/`SSH_HOST` set — the `: "${VAR:?...}"` checks only fire once execution reaches that line, which is after the `--help` case has already exited.

- [ ] **Step 8: Actually run `run.sh` for real (safe — local Docker only)**

Run: `./run.sh`
Expected: builds/starts the container, prints the `═══ VERIFICATION ═══` block with `Container running: yes`, a `200` health check, `Port reachable: yes`, the boxed `RUNNING ✓` banner, and the NEXT STEPS/URLS/COMMANDS sections. Confirm by actually opening `http://localhost:8000/login` (or via `curl`) and logging in with the printed `API_KEY` — the list page should load (empty or with real synced sessions, depending on what Task 9/10's indexers found).

- [ ] **Step 9: Run `run.sh --test` and confirm both suites pass**

Run: `./run.sh --test`
Expected: prints backend pytest results, then agent pytest results, then `All tests passed.`, exit 0

- [ ] **Step 10: Commit**

```bash
git add run.sh build-and-push.sh build-and-deploy.sh deploy-production-scp.sh
git commit -m "feat: add Jan's standard run/build/deploy scripts"
```

---
