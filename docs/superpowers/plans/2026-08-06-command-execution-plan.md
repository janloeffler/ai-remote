# Remote Command Execution ("Plan B") Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the web app send a follow-up prompt to resume an existing Claude Code/Cursor session, or start a brand-new session in an allow-listed project, on the Mac — with a kill switch and an audit log.

**Architecture:** Reuses the existing `jobs` table/poll-loop (`resume_message`/`new_session` types are already reserved in the schema's `CHECK` constraint). The backend and the agent each read their own copy of an `AI_REMOTE_ALLOWED_PROJECTS` env var and independently validate the target project path before, respectively, creating and executing a job. The kill switch is enforced entirely server-side: while paused, `GET /jobs/pending` withholds `resume_message`/`new_session` jobs from the agent (never hands them the job body at all).

**Tech Stack:** Same as Plan A/frontend-polish — Python 3.12, FastAPI, SQLite (stdlib `sqlite3`), Jinja2, vanilla JS, `httpx` (agent), `subprocess`/`os.killpg` (agent's CLI invocation), pytest.

## Global Constraints

- Single Mac, single user, personal use only (unchanged).
- `AI_REMOTE_ALLOWED_PROJECTS` is a comma-separated list of absolute paths, set independently in the backend's `.env` (server) and the agent's `.env` (Mac) — never editable via any API route, never synced between the two.
- Allow-list matching is **exact** (after stripping a trailing `/`), never prefix/substring — an allow-listed `/Users/jan/source/foo` must never match `/Users/jan/source/foo-evil`.
- Never use `--dangerously-skip-permissions` for Claude Code. Use `--permission-mode dontAsk`.
- Cursor's only real scoping is `--force`/`--workspace <path>` — accepted as weaker than Claude Code's, per Plan A.
- Command-job (`resume_message`/`new_session`) timeout is 30 minutes (1800s) on both sides — the backend's DB bookkeeping cutoff and the agent's actual subprocess kill. `fetch_full`'s existing 300s cutoff is unchanged.
- No new job-type-specific frontend build step; stays vanilla JS/Jinja2, consistent with Plan A and frontend-polish.
- No automatic writing of `permissions.allow`/`deny` profiles into allow-listed projects by the agent — ship a template, document that the user places it manually.
- Reference spec: `docs/superpowers/specs/2026-08-06-command-execution-design.md`.

---

## File Structure

```
backend/
  app/
    schema.sql          # Task 1 — new `settings` table
    db.py                # Task 1 — paused get/set, claim_pending_jobs filter, per-type fail_stale_jobs, get_all_jobs
    settings.py           # Task 2 — ALLOWED_PROJECTS
    allowlist.py           # Task 2 — is_allowed()
    models.py                # Task 3 — CommandRequest, NewSessionCommandRequest
    main.py                    # Tasks 4,5,6,7 — new routes
    templates/
      detail.html               # Task 8 — command composer
      new_session.html           # Task 5 — new file
      jobs.html                   # Task 6 — new file
      base.html                    # Task 7 — nav links + paused banner
      list.html                     # Task 7 — pause toggle
    static/
      app.js                        # Task 8 — command-form + new-session-form JS
      style.css                      # Task 8 — composer/banner styles
  tests/
    test_db.py                        # Task 1
    test_settings.py                   # Task 2
    test_allowlist.py                   # Task 2
    test_commands.py                     # Tasks 4,5
    test_detail_and_jobs.py               # Tasks 6,7,8 (extends existing file)
    test_list.py                           # Task 7 (extends existing file)

agent/
  agent/
    config.py                               # Task 9 — allowed_projects
    allowlist.py                              # Task 9 — is_allowed()
    claude_code_source.py                      # Task 10 — get_project_path()
    cursor_source.py                            # Task 10 — get_project_path()
    executor.py                                  # Task 11 — _run_subprocess, execute_resume_message, execute_new_session
    main.py                                        # Task 12 — dispatch wiring
  tests/
    test_config.py                                  # Task 9
    test_allowlist.py                                # Task 9
    test_claude_code_source.py                        # Task 10 (extends existing file)
    test_cursor_source.py                              # Task 10 (extends existing file)
    test_executor_commands.py                           # Task 11 — new file
    test_main_cycle.py                                    # Task 12 (extends existing file)
    fixtures/
      fake_cli.py                                          # Task 11 — stub CLI script

docs/superpowers/reference/
  remote-agent-permissions.settings.json                     # Task 13 — new file

agent/README.md, backend/README.md, example.env                # Task 13
```

---

## Task 1: Backend — settings table, paused flag, per-type job timeout, audit query

**Files:**
- Modify: `backend/app/schema.sql`
- Modify: `backend/app/db.py`
- Modify: `backend/tests/test_db.py`

**Interfaces:**
- Consumes: nothing new (builds on existing `db.py`/`schema.sql`).
- Produces: `db.get_remote_commands_paused(conn) -> bool`, `db.set_remote_commands_paused(conn, paused: bool) -> None`, `db.get_all_jobs(conn) -> list[dict]`. Changes behavior (same signatures) of `db.claim_pending_jobs(conn) -> list[dict]` (now paused-aware) and `db.fail_stale_jobs(conn) -> None` (now per-type, **drops** the `timeout_seconds` parameter — check no other caller passes it before this task, see Step 1).

- [ ] **Step 1: Confirm no caller passes `timeout_seconds` to `fail_stale_jobs`**

Run: `grep -rn "fail_stale_jobs" backend/ agent/`
Expected: only the definition in `db.py` and a no-argument call `db.fail_stale_jobs(conn)` in `backend/app/main.py`. If any call site passes a second argument, note it — the implementation below removes that parameter.

- [ ] **Step 2: Write the failing tests**

Append to `backend/tests/test_db.py`:

```python
def test_remote_commands_paused_starts_false_then_can_be_set(conn):
    assert db.get_remote_commands_paused(conn) is False
    db.set_remote_commands_paused(conn, True)
    assert db.get_remote_commands_paused(conn) is True
    db.set_remote_commands_paused(conn, False)
    assert db.get_remote_commands_paused(conn) is False


def test_claim_pending_jobs_withholds_command_jobs_while_paused(conn):
    db.upsert_session(conn, _sample_session())
    fetch_job = db.create_job(conn, "fetch_full", "claude-code:abc")
    resume_job = db.create_job(conn, "resume_message", "claude-code:abc", payload='{"prompt": "hi"}')
    new_session_job = db.create_job(conn, "new_session", "/allowed/path", payload='{"prompt": "hi", "tool": "claude-code"}')

    db.set_remote_commands_paused(conn, True)
    claimed = db.claim_pending_jobs(conn)
    assert {j["id"] for j in claimed} == {fetch_job}

    db.set_remote_commands_paused(conn, False)
    claimed_after_unpause = db.claim_pending_jobs(conn)
    assert {j["id"] for j in claimed_after_unpause} == {resume_job, new_session_job}


def test_fail_stale_jobs_uses_shorter_timeout_for_fetch_full(conn):
    import time
    from datetime import datetime, timedelta, timezone

    db.upsert_session(conn, _sample_session())
    old_fetch_job = db.create_job(conn, "fetch_full", "claude-code:abc")
    old_resume_job = db.create_job(conn, "resume_message", "claude-code:abc", payload='{"prompt": "hi"}')
    db.claim_pending_jobs(conn)  # both -> running

    # Backdate created_at past fetch_full's 300s cutoff but well within resume_message's 1800s one.
    backdated = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    conn.execute("UPDATE jobs SET created_at = ? WHERE id IN (?, ?)", (backdated, old_fetch_job, old_resume_job))
    conn.commit()

    db.fail_stale_jobs(conn)

    assert db.get_job(conn, old_fetch_job)["status"] == "failed"
    assert db.get_job(conn, old_resume_job)["status"] == "running"


def test_get_all_jobs_returns_newest_first(conn):
    db.upsert_session(conn, _sample_session())
    first = db.create_job(conn, "fetch_full", "claude-code:abc")
    second = db.create_job(conn, "resume_message", "claude-code:abc", payload='{"prompt": "hi"}')

    jobs = db.get_all_jobs(conn)
    assert [j["id"] for j in jobs] == [second, first]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_db.py -v -k "paused or withholds or timeout or newest_first"`
Expected: FAIL — `AttributeError: module 'app.db' has no attribute 'get_remote_commands_paused'` (and similar for the others)

- [ ] **Step 4: Add the `settings` table to `backend/app/schema.sql`**

Append:

```sql
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    remote_commands_paused INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO settings (id, remote_commands_paused) VALUES (1, 0);
```

- [ ] **Step 5: Implement in `backend/app/db.py`**

Add near `record_agent_contact`/`get_last_agent_contact`:

```python
def get_remote_commands_paused(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT remote_commands_paused FROM settings WHERE id = 1").fetchone()
    return bool(row["remote_commands_paused"]) if row else False


def set_remote_commands_paused(conn: sqlite3.Connection, paused: bool) -> None:
    conn.execute("UPDATE settings SET remote_commands_paused = ? WHERE id = 1", (int(paused),))
    conn.commit()


def get_all_jobs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]
```

Replace `claim_pending_jobs`:

```python
def claim_pending_jobs(conn: sqlite3.Connection) -> list[dict]:
    query = "SELECT * FROM jobs WHERE status = 'pending'"
    if get_remote_commands_paused(conn):
        query += " AND type NOT IN ('resume_message', 'new_session')"
    rows = conn.execute(query).fetchall()
    claimed = []
    for row in rows:
        cursor = conn.execute(
            "UPDATE jobs SET status = 'running' WHERE id = ? AND status = 'pending'",
            (row["id"],),
        )
        if cursor.rowcount:
            job_dict = dict(row)
            job_dict["status"] = "running"
            claimed.append(job_dict)
    conn.commit()
    return claimed
```

Replace `fail_stale_jobs`:

```python
_JOB_TIMEOUTS_SECONDS = {"fetch_full": 300, "resume_message": 1800, "new_session": 1800}


def fail_stale_jobs(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc)
    for job_type, timeout_seconds in _JOB_TIMEOUTS_SECONDS.items():
        cutoff = (now - timedelta(seconds=timeout_seconds)).isoformat()
        conn.execute(
            "UPDATE jobs SET status = 'failed', result_text = 'timed out', completed_at = ? "
            "WHERE status = 'running' AND type = ? AND created_at < ?",
            (now.isoformat(), job_type, cutoff),
        )
    conn.commit()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_db.py -v`
Expected: PASS (all tests, including pre-existing ones)

- [ ] **Step 7: Commit**

```bash
git add backend/app/schema.sql backend/app/db.py backend/tests/test_db.py
git commit -m "feat(backend): remote-commands pause flag, per-type job timeout, audit query"
```

---

## Task 2: Backend — allow-list settings and exact-match check

**Files:**
- Modify: `backend/app/settings.py`
- Create: `backend/app/allowlist.py`
- Modify: `example.env`
- Create: `backend/tests/test_allowlist.py`
- Modify: `backend/tests/test_settings.py`

**Interfaces:**
- Consumes: `backend/app/settings.py`'s existing `_require_env` pattern.
- Produces: `settings.ALLOWED_PROJECTS: list[str]`, `allowlist.is_allowed(project_path: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_allowlist.py`:

```python
def test_is_allowed_exact_match(monkeypatch):
    from app import settings, allowlist

    monkeypatch.setattr(settings, "ALLOWED_PROJECTS", ["/Users/jan/source/foo", "/Users/jan/source/bar"])
    assert allowlist.is_allowed("/Users/jan/source/foo") is True


def test_is_allowed_rejects_substring_lookalike(monkeypatch):
    from app import settings, allowlist

    monkeypatch.setattr(settings, "ALLOWED_PROJECTS", ["/Users/jan/source/foo"])
    assert allowlist.is_allowed("/Users/jan/source/foo-evil") is False


def test_is_allowed_ignores_trailing_slash(monkeypatch):
    from app import settings, allowlist

    monkeypatch.setattr(settings, "ALLOWED_PROJECTS", ["/Users/jan/source/foo/"])
    assert allowlist.is_allowed("/Users/jan/source/foo") is True


def test_is_allowed_rejects_empty_path(monkeypatch):
    from app import settings, allowlist

    monkeypatch.setattr(settings, "ALLOWED_PROJECTS", [""])
    assert allowlist.is_allowed("") is False
```

Append to `backend/tests/test_settings.py`:

```python
def test_allowed_projects_default_empty():
    full_env = {**os.environ, "API_KEY": "a" * 20, "SECRET_KEY": "b" * 20}
    full_env.pop("AI_REMOTE_ALLOWED_PROJECTS", None)
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.ALLOWED_PROJECTS)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_allowed_projects_parses_comma_separated_list():
    full_env = {
        **os.environ,
        "API_KEY": "a" * 20,
        "SECRET_KEY": "b" * 20,
        "AI_REMOTE_ALLOWED_PROJECTS": "/Users/jan/source/foo, /Users/jan/source/bar ,",
    }
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.ALLOWED_PROJECTS)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['/Users/jan/source/foo', '/Users/jan/source/bar']"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_allowlist.py tests/test_settings.py -v -k "allowed"`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.allowlist'` and `AttributeError: module 'app.settings' has no attribute 'ALLOWED_PROJECTS'`

- [ ] **Step 3: Add `ALLOWED_PROJECTS` to `backend/app/settings.py`**

Append:

```python
ALLOWED_PROJECTS = [
    p.strip() for p in (os.environ.get("AI_REMOTE_ALLOWED_PROJECTS") or "").split(",") if p.strip()
]
```

- [ ] **Step 4: Write `backend/app/allowlist.py`**

```python
from . import settings


def is_allowed(project_path: str) -> bool:
    if not project_path:
        return False
    normalized = project_path.rstrip("/")
    return normalized in {p.rstrip("/") for p in settings.ALLOWED_PROJECTS}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_allowlist.py tests/test_settings.py -v`
Expected: PASS (all tests, including pre-existing ones)

- [ ] **Step 6: Document the new env var in `example.env`**

Append to `example.env`:

```
# Comma-separated absolute paths where remote commands (resume/new-session)
# are allowed to run. Must be set to the SAME value in the local agent's own
# .env on the Mac — these are two independently-configured copies, neither
# editable via the API. Leave empty to disable remote command execution
# entirely (the composer/new-session UI simply won't offer any project).
AI_REMOTE_ALLOWED_PROJECTS=
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/settings.py backend/app/allowlist.py backend/tests/test_allowlist.py backend/tests/test_settings.py example.env
git commit -m "feat(backend): project allow-list setting and exact-match check"
```

---

## Task 3: Backend — request models for command endpoints

**Files:**
- Modify: `backend/app/models.py`

**Interfaces:**
- Produces: `CommandRequest`, `NewSessionCommandRequest` pydantic models, used by Tasks 4 and 5.

- [ ] **Step 1: Add the models**

Append to `backend/app/models.py`:

```python
class CommandRequest(BaseModel):
    prompt: str


class NewSessionCommandRequest(BaseModel):
    project_path: str
    tool: Literal["claude-code", "cursor"]
    prompt: str
```

- [ ] **Step 2: Verify the module still imports cleanly**

Run: `cd backend && .venv/bin/python -c "from app.models import CommandRequest, NewSessionCommandRequest"`
Expected: no output, no error

- [ ] **Step 3: Commit**

```bash
git add backend/app/models.py
git commit -m "feat(backend): request models for resume-message and new-session commands"
```

---

## Task 4: Backend — `POST /chats/{id}/command` (resume an existing session)

**Files:**
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_commands.py`

**Interfaces:**
- Consumes: `db.get_session`, `db.get_remote_commands_paused`, `db.create_job` (Tasks 1, existing), `allowlist.is_allowed` (Task 2), `models.CommandRequest` (Task 3).
- Produces: `POST /chats/{session_id}/command` — 404 unknown session, 403 not allow-listed, 409 paused, 200 `{job_id}` on success. Establishes the `logged_in_client` + allow-list env-var fixture pattern Task 5's tests reuse.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_commands.py`:

```python
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def logged_in_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("API_KEY", "test-api-key-1234")
    monkeypatch.setenv("SECRET_KEY", "test-secret-value1")
    monkeypatch.setenv("SESSION_COOKIE_HTTPS_ONLY", "false")
    monkeypatch.setenv("AI_REMOTE_ALLOWED_PROJECTS", "/Users/jan/source/demo")
    from app.main import app

    with TestClient(app) as test_client:
        test_client.post("/login", data={"api_key": "test-api-key-1234"})
        yield test_client


def _sync_session(client, project_path="/Users/jan/source/demo", session_id="claude-code:abc"):
    session = {
        "id": session_id,
        "tool": "claude-code",
        "entrypoint": "cli",
        "project_path": project_path,
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
        headers={"Authorization": "Bearer test-api-key-1234"},
    )


def test_command_creates_job_for_allowlisted_session(logged_in_client):
    _sync_session(logged_in_client)
    response = logged_in_client.post("/chats/claude-code:abc/command", json={"prompt": "keep going"})
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    pending = logged_in_client.get(
        "/jobs/pending", headers={"Authorization": "Bearer test-api-key-1234"}
    ).json()["jobs"]
    assert pending[0]["id"] == job_id
    assert pending[0]["type"] == "resume_message"
    assert pending[0]["target"] == "claude-code:abc"
    assert '"prompt": "keep going"' in pending[0]["payload"]


def test_command_404_for_unknown_session(logged_in_client):
    response = logged_in_client.post("/chats/does-not-exist/command", json={"prompt": "hi"})
    assert response.status_code == 404


def test_command_403_when_project_not_allowlisted(logged_in_client):
    _sync_session(logged_in_client, project_path="/Users/jan/source/not-allowed", session_id="claude-code:xyz")
    response = logged_in_client.post("/chats/claude-code:xyz/command", json={"prompt": "hi"})
    assert response.status_code == 403


def test_command_409_when_paused(logged_in_client):
    from app import db

    _sync_session(logged_in_client)
    import os

    conn = db.get_connection(os.environ["DATABASE_PATH"])
    db.set_remote_commands_paused(conn, True)
    conn.close()

    response = logged_in_client.post("/chats/claude-code:abc/command", json={"prompt": "hi"})
    assert response.status_code == 409
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_commands.py -v`
Expected: FAIL — 404/405 for `/chats/claude-code:abc/command` (route doesn't exist yet)

- [ ] **Step 3: Add the route to `backend/app/main.py`**

Update three existing import lines at the top of the file:
- `from fastapi import Depends, FastAPI, Form, Request` → `from fastapi import Depends, FastAPI, Form, HTTPException, Request`
- `from . import db, rate_limit, settings` → `from . import allowlist, db, rate_limit, settings`
- `from .models import JobCompleteRequest, SyncIndexRequest` → `from .models import CommandRequest, JobCompleteRequest, SyncIndexRequest`

Add the route:

```python
@app.post("/chats/{session_id}/command", dependencies=[Depends(require_session)])
def send_command(session_id: str, body: CommandRequest, conn=Depends(db.get_db_dependency)):
    session = db.get_session(conn, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if db.get_remote_commands_paused(conn):
        raise HTTPException(status_code=409, detail="remote commands are paused")
    if not allowlist.is_allowed(session["project_path"]):
        raise HTTPException(status_code=403, detail="project path is not allow-listed")
    job_id = db.create_job(
        conn, "resume_message", session_id, payload=json.dumps({"prompt": body.prompt})
    )
    return {"job_id": job_id}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_commands.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full backend suite to check for regressions**

Run: `cd backend && .venv/bin/pytest -v`
Expected: PASS (all tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/main.py backend/tests/test_commands.py
git commit -m "feat(backend): POST /chats/{id}/command creates a resume_message job"
```

---

## Task 5: Backend — new-session screen and command

**Files:**
- Modify: `backend/app/main.py`
- Create: `backend/app/templates/new_session.html`
- Modify: `backend/tests/test_commands.py`

**Interfaces:**
- Consumes: `settings.ALLOWED_PROJECTS`, `allowlist.is_allowed`, `db.get_remote_commands_paused`, `db.create_job` (existing/Task 2), `models.NewSessionCommandRequest` (Task 3).
- Produces: `GET /projects/new` (HTML), `POST /projects/command` — 403 not allow-listed, 409 paused, 200 `{job_id}`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_commands.py`:

```python
def test_new_session_form_lists_allowed_projects(logged_in_client):
    response = logged_in_client.get("/projects/new")
    assert response.status_code == 200
    assert "/Users/jan/source/demo" in response.text


def test_new_session_command_creates_job(logged_in_client):
    response = logged_in_client.post(
        "/projects/command",
        json={"project_path": "/Users/jan/source/demo", "tool": "claude-code", "prompt": "start fresh"},
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    pending = logged_in_client.get(
        "/jobs/pending", headers={"Authorization": "Bearer test-api-key-1234"}
    ).json()["jobs"]
    assert pending[0]["id"] == job_id
    assert pending[0]["type"] == "new_session"
    assert pending[0]["target"] == "/Users/jan/source/demo"
    assert '"tool": "claude-code"' in pending[0]["payload"]


def test_new_session_command_403_when_not_allowlisted(logged_in_client):
    response = logged_in_client.post(
        "/projects/command",
        json={"project_path": "/Users/jan/source/not-allowed", "tool": "claude-code", "prompt": "hi"},
    )
    assert response.status_code == 403


def test_new_session_command_works_even_with_zero_prior_sessions(logged_in_client):
    # No /sync/index call for this path at all — allow-list membership must be
    # the only requirement, independent of whether anything was ever synced from it.
    response = logged_in_client.post(
        "/projects/command",
        json={"project_path": "/Users/jan/source/demo", "tool": "cursor", "prompt": "hi"},
    )
    assert response.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_commands.py -v -k new_session`
Expected: FAIL — 404 for `/projects/new` and `/projects/command`

- [ ] **Step 3: Write `backend/app/templates/new_session.html`**

```html
{% extends "base.html" %}
{% block content %}
<h1>Neue Session starten</h1>
{% if not allowed_projects %}
<p>Keine Projekte in der Allow-List konfiguriert.</p>
{% endif %}
<ul class="project-command-list">
  {% for path in allowed_projects %}
  <li>
    <form class="new-session-form" data-project-path="{{ path }}">
      <strong>{{ path }}</strong>
      <select name="tool">
        <option value="claude-code">Claude Code</option>
        <option value="cursor">Cursor</option>
      </select>
      <textarea name="prompt" placeholder="Prompt..." required></textarea>
      <button type="submit">Senden</button>
      <p class="command-status"></p>
    </form>
  </li>
  {% endfor %}
</ul>
{% endblock %}
```

- [ ] **Step 4: Add the routes to `backend/app/main.py`**

Update the import added in Task 4 to also include `NewSessionCommandRequest`:
`from .models import CommandRequest, JobCompleteRequest, SyncIndexRequest` → `from .models import CommandRequest, JobCompleteRequest, NewSessionCommandRequest, SyncIndexRequest`

Add the routes:

```python
@app.get("/projects/new", dependencies=[Depends(require_session)])
def new_session_form(request: Request):
    return templates.TemplateResponse(
        request, "new_session.html", {"allowed_projects": settings.ALLOWED_PROJECTS}
    )


@app.post("/projects/command", dependencies=[Depends(require_session)])
def send_new_session_command(body: NewSessionCommandRequest, conn=Depends(db.get_db_dependency)):
    if db.get_remote_commands_paused(conn):
        raise HTTPException(status_code=409, detail="remote commands are paused")
    if not allowlist.is_allowed(body.project_path):
        raise HTTPException(status_code=403, detail="project path is not allow-listed")
    job_id = db.create_job(
        conn,
        "new_session",
        body.project_path,
        payload=json.dumps({"prompt": body.prompt, "tool": body.tool}),
    )
    return {"job_id": job_id}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_commands.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/main.py backend/app/templates/new_session.html backend/tests/test_commands.py
git commit -m "feat(backend): new-session screen and POST /projects/command"
```

---

## Task 6: Backend — generic job-status endpoint and audit log page

**Files:**
- Modify: `backend/app/main.py`
- Create: `backend/app/templates/jobs.html`
- Modify: `backend/tests/test_detail_and_jobs.py`

**Interfaces:**
- Consumes: `db.get_job`, `db.get_all_jobs` (Task 1).
- Produces: `GET /jobs/{job_id}/status` (used by Task 8's new-session/command-composer JS, since those flows have no session id to key the existing `/chats/{id}/status` route on), `GET /jobs` (audit log HTML page).

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_detail_and_jobs.py`:

```python
def test_generic_job_status_endpoint(logged_in_client):
    _sync_one(logged_in_client)
    enqueue = logged_in_client.post("/chats/claude-code:abc/fetch-full", params={"full": "true"})
    job_id = enqueue.json()["job_id"]

    response = logged_in_client.get(f"/jobs/{job_id}/status")
    assert response.status_code == 200
    assert response.json()["status"] in ("pending", "running")


def test_generic_job_status_unknown_job(logged_in_client):
    response = logged_in_client.get("/jobs/does-not-exist/status")
    assert response.json() == {"status": "unknown", "result_text": None}


def test_jobs_audit_page_lists_jobs_newest_first(logged_in_client):
    _sync_one(logged_in_client)
    logged_in_client.post("/chats/claude-code:abc/fetch-full", params={"full": "true"})

    response = logged_in_client.get("/jobs")
    assert response.status_code == 200
    assert "fetch_full" in response.text
    assert "claude-code:abc" in response.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_detail_and_jobs.py -v -k "job_status or audit"`
Expected: FAIL — 404 for `/jobs/{job_id}/status` and `/jobs`

- [ ] **Step 3: Write `backend/app/templates/jobs.html`**

```html
{% extends "base.html" %}
{% block content %}
<h1>Audit-Log</h1>
<ul class="job-list">
  {% for j in jobs %}
  <li>
    <div class="job-header">
      <strong>{{ j.type }}</strong>
      <span class="job-status job-status-{{ j.status }}">{{ j.status }}</span>
    </div>
    <p class="job-target">{{ j.target }}</p>
    {% if j.payload %}<p class="job-payload">{{ j.payload }}</p>{% endif %}
    {% if j.result_text %}<pre class="job-result">{{ j.result_text }}</pre>{% endif %}
    <time>{{ j.created_at | de_datetime }}</time>
  </li>
  {% else %}
  <li>Keine Jobs vorhanden.</li>
  {% endfor %}
</ul>
{% endblock %}
```

- [ ] **Step 4: Add the routes to `backend/app/main.py`**

```python
@app.get("/jobs/{job_id}/status", dependencies=[Depends(require_session)])
def job_status_generic(job_id: str, conn=Depends(db.get_db_dependency)):
    job = db.get_job(conn, job_id)
    return {"status": job["status"] if job else "unknown", "result_text": job["result_text"] if job else None}


@app.get("/jobs", dependencies=[Depends(require_session)])
def jobs_audit_log(request: Request, conn=Depends(db.get_db_dependency)):
    return templates.TemplateResponse(request, "jobs.html", {"jobs": db.get_all_jobs(conn)})
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_detail_and_jobs.py -v`
Expected: PASS (all tests, including pre-existing ones)

- [ ] **Step 6: Commit**

```bash
git add backend/app/main.py backend/app/templates/jobs.html backend/tests/test_detail_and_jobs.py
git commit -m "feat(backend): generic job-status endpoint and audit log page"
```

---

## Task 7: Backend — kill switch toggle, nav links, site-wide paused banner

**Files:**
- Modify: `backend/app/main.py`
- Modify: `backend/app/templates/base.html`
- Modify: `backend/app/templates/list.html`
- Modify: `backend/tests/test_list.py`

**Interfaces:**
- Consumes: `db.get_remote_commands_paused`, `db.set_remote_commands_paused` (Task 1).
- Produces: `POST /settings/pause-remote-commands`; every session-authed route from here on should pass `remote_commands_paused` in its template context (base.html renders the banner from it, defaulting to falsy when a route omits it — Jinja2's default `Undefined` is falsy in an `{% if %}`, so this never raises).

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_list.py`:

```python
def test_pause_toggle_shows_banner_on_list_page(logged_in_client):
    before = logged_in_client.get("/")
    assert "Remote-Befehle sind pausiert" not in before.text

    toggle = logged_in_client.post("/settings/pause-remote-commands", data={"paused": "true"})
    assert toggle.status_code == 303

    after = logged_in_client.get("/")
    assert "Remote-Befehle sind pausiert" in after.text

    logged_in_client.post("/settings/pause-remote-commands", data={"paused": "false"})
    resumed = logged_in_client.get("/")
    assert "Remote-Befehle sind pausiert" not in resumed.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/test_list.py -v -k pause`
Expected: FAIL — 404 for `/settings/pause-remote-commands`

- [ ] **Step 3: Update `backend/app/templates/base.html`**

Replace the `<header>` line and add the banner:

```html
<header>
  <a href="/">AI Remote Chats</a>
  <nav>
    <a href="/projects/new">Neue Session</a>
    <a href="/jobs">Audit-Log</a>
  </nav>
</header>
{% if remote_commands_paused %}
<div class="paused-banner">Remote-Befehle sind pausiert.</div>
{% endif %}
```

- [ ] **Step 4: Add the pause toggle to `backend/app/templates/list.html`**

Add just below the existing `<p class="staleness">...</p>` block:

```html
<form method="post" action="/settings/pause-remote-commands" class="pause-toggle">
  <input type="hidden" name="paused" value="{{ 'false' if remote_commands_paused else 'true' }}">
  <button type="submit">{{ 'Remote-Befehle fortsetzen' if remote_commands_paused else 'Remote-Befehle pausieren' }}</button>
</form>
```

- [ ] **Step 5: Add the route and pass `remote_commands_paused` to `list_chats`' context, in `backend/app/main.py`**

```python
@app.post("/settings/pause-remote-commands", dependencies=[Depends(require_session)])
def toggle_pause_remote_commands(paused: bool = Form(...), conn=Depends(db.get_db_dependency)):
    db.set_remote_commands_paused(conn, paused)
    return RedirectResponse("/", status_code=303)
```

In `list_chats`, add one key to the existing `TemplateResponse` context dict:

```python
"remote_commands_paused": db.get_remote_commands_paused(conn),
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_list.py -v`
Expected: PASS (all tests, including pre-existing ones)

- [ ] **Step 7: Run the full backend suite to check for regressions**

Run: `cd backend && .venv/bin/pytest -v`
Expected: PASS (all tests)

- [ ] **Step 8: Commit**

```bash
git add backend/app/main.py backend/app/templates/base.html backend/app/templates/list.html backend/tests/test_list.py
git commit -m "feat(backend): pause-remote-commands kill switch with site-wide banner"
```

---

## Task 8: Frontend — command composer on chat detail, new-session forms, styling

**Files:**
- Modify: `backend/app/main.py` (pass `command_allowed`/`remote_commands_paused` into `chat_detail` and `new_session_form`'s contexts)
- Modify: `backend/app/templates/detail.html`
- Modify: `backend/app/templates/new_session.html`
- Modify: `backend/app/static/app.js`
- Modify: `backend/app/static/style.css`
- Modify: `backend/tests/test_commands.py`

**Interfaces:**
- Consumes: `allowlist.is_allowed`, the `/chats/{id}/command`, `/projects/command`, `/jobs/{id}/status` endpoints (Tasks 2, 4, 5, 6).
- Produces: the end-user-visible composer UI. No new backend interfaces for later tasks to consume.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_commands.py`:

```python
def test_detail_page_shows_composer_for_allowlisted_session(logged_in_client):
    _sync_session(logged_in_client)
    response = logged_in_client.get("/chats/claude-code:abc")
    assert 'id="command-form"' in response.text


def test_detail_page_hides_composer_for_non_allowlisted_session(logged_in_client):
    _sync_session(logged_in_client, project_path="/Users/jan/source/not-allowed", session_id="claude-code:xyz")
    response = logged_in_client.get("/chats/claude-code:xyz")
    assert 'id="command-form"' not in response.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_commands.py -v -k composer`
Expected: FAIL — `assert 'id="command-form"' in response.text` fails (no composer markup yet)

- [ ] **Step 3: Update `chat_detail` in `backend/app/main.py`**

Add these two keys to the existing `TemplateResponse` context dict in `chat_detail` (the `session is None` early-return branch is unaffected):

```python
"command_allowed": allowlist.is_allowed(session["project_path"]),
"remote_commands_paused": db.get_remote_commands_paused(conn),
```

- [ ] **Step 4: Add the composer to `backend/app/templates/detail.html`**

Add just before the final `{% endif %}` that closes the `session is none` check (i.e. as the last thing rendered when a session *was* found):

```html
{% if command_allowed %}
<form id="command-form" data-session-id="{{ session.id }}">
  <textarea name="prompt" placeholder="Befehl senden..." required></textarea>
  <button type="submit">Senden</button>
  <p id="command-status"></p>
</form>
{% endif %}
```

- [ ] **Step 5: Update `new_session_form` in `backend/app/main.py`** to also carry the paused flag

```python
@app.get("/projects/new", dependencies=[Depends(require_session)])
def new_session_form(request: Request, conn=Depends(db.get_db_dependency)):
    return templates.TemplateResponse(
        request,
        "new_session.html",
        {
            "allowed_projects": settings.ALLOWED_PROJECTS,
            "remote_commands_paused": db.get_remote_commands_paused(conn),
        },
    )
```

- [ ] **Step 6: Append the command-composer and new-session JS to `backend/app/static/app.js`**

```js
document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("command-form");
  if (!form) return;
  const sessionId = form.dataset.sessionId;
  const status = document.getElementById("command-status");
  const button = form.querySelector("button[type=submit]");

  const poll = (jobId) => {
    fetch(`/jobs/${jobId}/status`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data) => {
        if (data.status === "done") {
          status.textContent = "Befehl ausgeführt — Chat wird beim nächsten Sync aktualisiert (bis zu 60s).";
        } else if (data.status === "failed") {
          status.textContent = `Fehlgeschlagen: ${data.result_text || "unbekannter Fehler"}`;
          button.disabled = false;
        } else {
          setTimeout(() => poll(jobId), 3000);
        }
      })
      .catch(() => {
        status.textContent = "Verbindung verloren — bitte erneut versuchen.";
        button.disabled = false;
      });
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    button.disabled = true;
    status.textContent = "Wird gesendet...";
    const prompt = form.prompt.value;
    fetch(`/chats/${sessionId}/command`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then(({ job_id }) => poll(job_id))
      .catch(() => {
        status.textContent = "Fehler beim Senden — bitte erneut versuchen.";
        button.disabled = false;
      });
  });
});

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".new-session-form").forEach((form) => {
    const projectPath = form.dataset.projectPath;
    const status = form.querySelector(".command-status");
    const button = form.querySelector("button[type=submit]");

    const poll = (jobId) => {
      fetch(`/jobs/${jobId}/status`)
        .then((res) => {
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          return res.json();
        })
        .then((data) => {
          if (data.status === "done") {
            status.textContent = "Fertig — die neue Session erscheint in der Liste in Kürze.";
          } else if (data.status === "failed") {
            status.textContent = `Fehlgeschlagen: ${data.result_text || "unbekannter Fehler"}`;
            button.disabled = false;
          } else {
            setTimeout(() => poll(jobId), 3000);
          }
        })
        .catch(() => {
          status.textContent = "Verbindung verloren — bitte erneut versuchen.";
          button.disabled = false;
        });
    };

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      button.disabled = true;
      status.textContent = "Wird gesendet...";
      const prompt = form.prompt.value;
      const tool = form.tool.value;
      fetch("/projects/command", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_path: projectPath, tool, prompt }),
      })
        .then((res) => {
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          return res.json();
        })
        .then(({ job_id }) => poll(job_id))
        .catch(() => {
          status.textContent = "Fehler beim Senden — bitte erneut versuchen.";
          button.disabled = false;
        });
    });
  });
});
```

- [ ] **Step 7: Append composer/banner/nav styles to `backend/app/static/style.css`**

```css
header nav { display: inline-flex; gap: 0.75rem; margin-left: 1rem; font-size: 0.85rem; }
header nav a { color: var(--text-muted); }

.paused-banner {
  background: #7a2020;
  color: #fff;
  text-align: center;
  padding: 0.5rem;
  font-size: 0.85rem;
}
@media (prefers-color-scheme: light) {
  .paused-banner { background: #fde2e2; color: #7a2020; }
}

#command-form, .new-session-form {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  margin-top: 1.25rem;
  padding-top: 1rem;
  border-top: 1px solid var(--border);
}
#command-form textarea, .new-session-form textarea {
  min-height: 80px;
  padding: 0.6rem 0.75rem;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--text);
  font-family: inherit;
}

.project-command-list { list-style: none; padding: 0; }
.project-command-list li {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 0.75rem;
  margin-bottom: 0.75rem;
}

.job-list { list-style: none; padding: 0; }
.job-list li {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 0.75rem;
  margin-bottom: 0.75rem;
}
.job-header { display: flex; justify-content: space-between; align-items: center; }
.job-status { font-size: 0.75rem; padding: 0.1rem 0.5rem; border-radius: 999px; background: var(--surface-hover); }
.job-status-done { color: #2a8f4c; }
.job-status-failed { color: #d24040; }
.job-result { white-space: pre-wrap; font-size: 0.8rem; overflow-x: auto; }
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_commands.py -v`
Expected: PASS (all tests)

- [ ] **Step 9: Manually verify in a browser** (no automated JS test framework in this project — consistent with Plan A/frontend-polish)

Run: `cd backend && DATABASE_PATH=/tmp/manual.db API_KEY=devkeydevkeydevkey SECRET_KEY=devsecretdevsecret1 SESSION_COOKIE_HTTPS_ONLY=false AI_REMOTE_ALLOWED_PROJECTS=/tmp .venv/bin/uvicorn app.main:app --reload`
Then log in, open a synced chat, confirm the composer only appears when its project is `/tmp`, submit a prompt, confirm the pause toggle hides the composer's effect (job stays `pending`, never reaches the agent) when paused, and check `/jobs` shows the created job.

- [ ] **Step 10: Commit**

```bash
git add backend/app/main.py backend/app/templates/detail.html backend/app/templates/new_session.html backend/app/static/app.js backend/app/static/style.css backend/tests/test_commands.py
git commit -m "feat(backend): command composer UI, new-session forms, and styling"
```

---

## Task 9: Agent — allow-list config and exact-match check

**Files:**
- Modify: `agent/agent/config.py`
- Create: `agent/agent/allowlist.py`
- Create: `agent/tests/test_config.py`
- Create: `agent/tests/test_allowlist.py`
- Modify: `example.env`

**Interfaces:**
- Produces: `Config.allowed_projects: list[str]` (new field, defaults to `[]` so existing call sites/tests that construct `Config(...)` without it keep working), `allowlist.is_allowed(project_path: str, allowed_projects: list[str]) -> bool`.

- [ ] **Step 1: Write the failing tests**

Create `agent/tests/test_allowlist.py`:

```python
from agent import allowlist


def test_is_allowed_exact_match():
    assert allowlist.is_allowed("/Users/jan/source/foo", ["/Users/jan/source/foo"]) is True


def test_is_allowed_rejects_substring_lookalike():
    assert allowlist.is_allowed("/Users/jan/source/foo-evil", ["/Users/jan/source/foo"]) is False


def test_is_allowed_ignores_trailing_slash():
    assert allowlist.is_allowed("/Users/jan/source/foo", ["/Users/jan/source/foo/"]) is True


def test_is_allowed_rejects_empty_path():
    assert allowlist.is_allowed("", [""]) is False
```

Create `agent/tests/test_config.py`:

```python
import os

from agent.config import load_config


def test_allowed_projects_default_empty(monkeypatch):
    monkeypatch.setenv("AI_REMOTE_BACKEND_URL", "http://example.com")
    monkeypatch.setenv("AI_REMOTE_API_KEY", "key")
    monkeypatch.delenv("AI_REMOTE_ALLOWED_PROJECTS", raising=False)
    config = load_config()
    assert config.allowed_projects == []


def test_allowed_projects_parses_comma_separated_list(monkeypatch):
    monkeypatch.setenv("AI_REMOTE_BACKEND_URL", "http://example.com")
    monkeypatch.setenv("AI_REMOTE_API_KEY", "key")
    monkeypatch.setenv("AI_REMOTE_ALLOWED_PROJECTS", "/Users/jan/source/foo, /Users/jan/source/bar ,")
    config = load_config()
    assert config.allowed_projects == ["/Users/jan/source/foo", "/Users/jan/source/bar"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd agent && .venv/bin/pytest tests/test_allowlist.py tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.allowlist'` / `AttributeError: 'Config' object has no attribute 'allowed_projects'`

- [ ] **Step 3: Write `agent/agent/allowlist.py`**

```python
def is_allowed(project_path: str, allowed_projects: list[str]) -> bool:
    if not project_path:
        return False
    normalized = project_path.rstrip("/")
    return normalized in {p.rstrip("/") for p in allowed_projects}
```

- [ ] **Step 4: Update `agent/agent/config.py`**

```python
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    backend_url: str
    api_key: str
    state_path: Path
    interval_seconds: int = 60
    allowed_projects: list[str] = field(default_factory=list)


def load_config() -> Config:
    return Config(
        backend_url=os.environ["AI_REMOTE_BACKEND_URL"].rstrip("/"),
        api_key=os.environ["AI_REMOTE_API_KEY"],
        state_path=Path(
            os.environ.get("AI_REMOTE_STATE_PATH", str(Path.home() / ".ai-remote-agent" / "sync_state.json"))
        ),
        interval_seconds=int(os.environ.get("AI_REMOTE_INTERVAL_SECONDS", "60")),
        allowed_projects=[
            p.strip() for p in (os.environ.get("AI_REMOTE_ALLOWED_PROJECTS") or "").split(",") if p.strip()
        ],
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd agent && .venv/bin/pytest tests/test_allowlist.py tests/test_config.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Run the full agent suite to check for regressions**

Run: `cd agent && .venv/bin/pytest -v`
Expected: PASS (all tests — `test_main_cycle.py`'s direct `main.Config(...)` constructions omit `allowed_projects` and must still work via the new default)

- [ ] **Step 7: Document the new env var in `example.env`**

Append (if Task 2's Step 6 already added the backend's copy, add the sentence noting this is the agent's independent copy):

```
# The agent's own copy of the allow-list — must match the backend's
# AI_REMOTE_ALLOWED_PROJECTS above exactly. Set in the agent's own .env on
# the Mac (not this file, which configures the backend) — listed here only
# as a reminder that both copies must be kept in sync manually.
```

- [ ] **Step 8: Commit**

```bash
git add agent/agent/config.py agent/agent/allowlist.py agent/tests/test_allowlist.py agent/tests/test_config.py example.env
git commit -m "feat(agent): allow-list config and exact-match check"
```

---

## Task 10: Agent — resolve a session's project path from its id

**Files:**
- Modify: `agent/agent/claude_code_source.py`
- Modify: `agent/agent/cursor_source.py`
- Modify: `agent/tests/test_claude_code_source.py`
- Modify: `agent/tests/test_cursor_source.py`

**Interfaces:**
- Consumes: existing private helpers `_find_session_file`/`_parse_session_file_cached` (claude_code_source), `_read_only_connection`/`_load_workspace_id`/`resolve_workspace_path` (cursor_source).
- Produces: `claude_code_source.get_project_path(raw_session_id: str, projects_dir: Path = CLAUDE_PROJECTS_DIR) -> str | None`, `cursor_source.get_project_path(composer_id: str, state_db_path: Path = STATE_DB, workspace_storage: Path = CURSOR_WORKSPACE_STORAGE) -> str | None`. Task 11's executor uses both to independently re-derive a session's project path (never trusting a path handed to it in the job payload).

- [ ] **Step 1: Write the failing tests**

Append to `agent/tests/test_claude_code_source.py` (reuse whatever fixture-writing helper the existing tests in this file use to create a sample `.jsonl` — follow the same pattern already present for `get_full_messages` tests):

```python
def test_get_project_path_returns_cwd_from_session_file(tmp_path):
    from agent.claude_code_source import get_project_path

    project_dir = tmp_path / "-Users-jan-source-demo"
    project_dir.mkdir()
    session_file = project_dir / "abc123.jsonl"
    session_file.write_text(
        '{"type": "user", "timestamp": "2026-08-01T10:00:00Z", "cwd": "/Users/jan/source/demo", '
        '"message": {"content": "hi"}}\n'
    )

    assert get_project_path("abc123", projects_dir=tmp_path) == "/Users/jan/source/demo"


def test_get_project_path_returns_none_for_unknown_session(tmp_path):
    from agent.claude_code_source import get_project_path

    assert get_project_path("does-not-exist", projects_dir=tmp_path) is None
```

Append to `agent/tests/test_cursor_source.py` (reuse this file's existing SQLite-fixture-building pattern for `composerHeaders`/workspace.json — follow whatever helper `enrich_with_messages`'s tests already use to build a fake `state.vscdb` and `workspace.json`):

```python
def test_get_project_path_resolves_via_workspace_json(tmp_path):
    import json
    import sqlite3

    from agent.cursor_source import get_project_path

    state_db = tmp_path / "state.vscdb"
    conn = sqlite3.connect(state_db)
    conn.execute("CREATE TABLE composerHeaders (composerId TEXT, workspaceId TEXT)")
    conn.execute("INSERT INTO composerHeaders VALUES ('composer-1', 'workspace-1')")
    conn.commit()
    conn.close()

    workspace_storage = tmp_path / "workspaceStorage"
    workspace_dir = workspace_storage / "workspace-1"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "workspace.json").write_text(
        json.dumps({"folder": "file:///Users/jan/source/demo"})
    )

    assert get_project_path("composer-1", state_db_path=state_db, workspace_storage=workspace_storage) == "/Users/jan/source/demo"


def test_get_project_path_returns_none_when_state_db_missing(tmp_path):
    from agent.cursor_source import get_project_path

    assert get_project_path("composer-1", state_db_path=tmp_path / "missing.vscdb") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd agent && .venv/bin/pytest tests/test_claude_code_source.py tests/test_cursor_source.py -v -k get_project_path`
Expected: FAIL — `ImportError: cannot import name 'get_project_path'`

- [ ] **Step 3: Add `get_project_path` to `agent/agent/claude_code_source.py`**

Add after `get_full_messages`:

```python
def get_project_path(raw_session_id: str, projects_dir: Path = CLAUDE_PROJECTS_DIR) -> str | None:
    path = _find_session_file(raw_session_id, projects_dir)
    if path is None:
        return None
    session = _parse_session_file_cached(path)
    return session["project_path"] if session else None
```

- [ ] **Step 4: Add `get_project_path` to `agent/agent/cursor_source.py`**

Add after `get_full_messages`:

```python
def get_project_path(
    composer_id: str,
    state_db_path: Path = STATE_DB,
    workspace_storage: Path = CURSOR_WORKSPACE_STORAGE,
) -> str | None:
    if not state_db_path.exists():
        return None
    conn = _read_only_connection(state_db_path)
    try:
        workspace_id = _load_workspace_id(conn, composer_id)
    finally:
        conn.close()
    if not workspace_id:
        return None
    return resolve_workspace_path(workspace_id, workspace_storage)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd agent && .venv/bin/pytest tests/test_claude_code_source.py tests/test_cursor_source.py -v`
Expected: PASS (all tests, including pre-existing ones)

- [ ] **Step 6: Commit**

```bash
git add agent/agent/claude_code_source.py agent/agent/cursor_source.py agent/tests/test_claude_code_source.py agent/tests/test_cursor_source.py
git commit -m "feat(agent): resolve a session's project path from its id, for allow-list re-checks"
```

---

## Task 11: Agent — command executors with subprocess timeout/kill

**Files:**
- Modify: `agent/agent/executor.py`
- Create: `agent/tests/fixtures/fake_cli.py`
- Create: `agent/tests/test_executor_commands.py`

**Interfaces:**
- Consumes: `allowlist.is_allowed` (Task 9), `claude_code_source.get_project_path`/`cursor_source.get_project_path` (Task 10).
- Produces: `executor.execute_resume_message(job: dict, allowed_projects: list[str]) -> dict`, `executor.execute_new_session(job: dict, allowed_projects: list[str]) -> dict` — same result shape as `execute_fetch_full`: `{"status": "done"|"failed", "result_text": str, "messages": [], "is_complete": False}`. Task 12 dispatches to these.

- [ ] **Step 1: Write the stub CLI fixture**

Create `agent/tests/fixtures/fake_cli.py`:

```python
#!/usr/bin/env python3
"""Stub CLI standing in for `claude`/`cursor-agent` in tests — never invokes the real
binaries. Behavior is selected via the FAKE_CLI_BEHAVIOR env var: 'ok' (default) prints
its argv to stdout and exits 0; 'fail' prints to stderr and exits 1; 'hang' sleeps
forever (used to test the agent's own subprocess timeout/kill, not the CLI's)."""
import os
import sys
import time

behavior = os.environ.get("FAKE_CLI_BEHAVIOR", "ok")

if behavior == "fail":
    print("simulated CLI failure", file=sys.stderr)
    sys.exit(1)
elif behavior == "hang":
    time.sleep(3600)
else:
    print(" ".join(sys.argv[1:]))
    sys.exit(0)
```

Run: `chmod +x agent/tests/fixtures/fake_cli.py`

- [ ] **Step 2: Write the failing tests**

Create `agent/tests/test_executor_commands.py`:

```python
import subprocess
import sys
from pathlib import Path

import pytest

from agent import executor

FAKE_CLI = [sys.executable, str(Path(__file__).parent / "fixtures" / "fake_cli.py")]


@pytest.fixture(autouse=True)
def _patch_subprocess_timeout(monkeypatch):
    # Tests must not wait 30 real minutes for the 'hang' case — shrink the
    # timeout the executor enforces, without touching its production default.
    monkeypatch.setattr(executor, "COMMAND_TIMEOUT_SECONDS", 1)


@pytest.fixture(autouse=True)
def _patch_cli_binaries(monkeypatch):
    monkeypatch.setattr(executor, "CLAUDE_CLI", FAKE_CLI)
    monkeypatch.setattr(executor, "CURSOR_AGENT_CLI", FAKE_CLI)


def test_execute_resume_message_rejects_non_allowlisted_project(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent.claude_code_source.get_project_path", lambda raw_id: "/Users/jan/source/not-allowed"
    )
    job = {"id": "j1", "type": "resume_message", "target": "claude-code:abc", "payload": '{"prompt": "hi"}'}

    result = executor.execute_resume_message(job, allowed_projects=["/Users/jan/source/demo"])

    assert result["status"] == "failed"
    assert "not allow-listed" in result["result_text"]


def test_execute_resume_message_runs_claude_and_reports_success(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.claude_code_source.get_project_path", lambda raw_id: str(tmp_path))
    job = {"id": "j1", "type": "resume_message", "target": "claude-code:abc", "payload": '{"prompt": "keep going"}'}

    result = executor.execute_resume_message(job, allowed_projects=[str(tmp_path)])

    assert result["status"] == "done"
    assert "--resume" in result["result_text"]
    assert "abc" in result["result_text"]
    assert "keep going" in result["result_text"]


def test_execute_resume_message_reports_failure_on_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLI_BEHAVIOR", "fail")
    monkeypatch.setattr("agent.claude_code_source.get_project_path", lambda raw_id: str(tmp_path))
    job = {"id": "j1", "type": "resume_message", "target": "claude-code:abc", "payload": '{"prompt": "hi"}'}

    result = executor.execute_resume_message(job, allowed_projects=[str(tmp_path)])

    assert result["status"] == "failed"
    assert "simulated CLI failure" in result["result_text"]


def test_execute_resume_message_times_out_and_kills_process_group(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLI_BEHAVIOR", "hang")
    monkeypatch.setattr("agent.claude_code_source.get_project_path", lambda raw_id: str(tmp_path))
    job = {"id": "j1", "type": "resume_message", "target": "claude-code:abc", "payload": '{"prompt": "hi"}'}

    result = executor.execute_resume_message(job, allowed_projects=[str(tmp_path)])

    assert result["status"] == "failed"
    assert "timed out" in result["result_text"]


def test_execute_new_session_runs_cursor_agent(tmp_path, monkeypatch):
    job = {
        "id": "j1",
        "type": "new_session",
        "target": str(tmp_path),
        "payload": '{"prompt": "start fresh", "tool": "cursor"}',
    }

    result = executor.execute_new_session(job, allowed_projects=[str(tmp_path)])

    assert result["status"] == "done"
    assert "--workspace" in result["result_text"]
    assert "start fresh" in result["result_text"]


def test_execute_new_session_rejects_non_allowlisted_project(tmp_path):
    job = {
        "id": "j1",
        "type": "new_session",
        "target": "/Users/jan/source/not-allowed",
        "payload": '{"prompt": "hi", "tool": "claude-code"}',
    }

    result = executor.execute_new_session(job, allowed_projects=[str(tmp_path)])

    assert result["status"] == "failed"
    assert "not allow-listed" in result["result_text"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd agent && .venv/bin/pytest tests/test_executor_commands.py -v`
Expected: FAIL — `AttributeError: module 'agent.executor' has no attribute 'execute_resume_message'`

- [ ] **Step 4: Implement in `agent/agent/executor.py`**

Add imports and constants at the top (keep the existing `import json` and `from . import claude_code_source, cursor_source`):

```python
import json
import os
import signal
import subprocess

from . import allowlist, claude_code_source, cursor_source

COMMAND_TIMEOUT_SECONDS = 1800
RESULT_TEXT_TRUNCATE = 4000
CLAUDE_CLI = ["claude"]
CURSOR_AGENT_CLI = ["cursor-agent"]
```

Add after the existing `_extract_count` function:

```python
def _parse_json_payload(payload: str) -> dict:
    if not payload:
        return {}
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _run_subprocess(cmd: list[str], cwd: str) -> tuple[int, str, bool]:
    """Runs cmd in its own process group (start_new_session=True) so that on timeout
    we can kill the whole group, not just the direct child — otherwise anything the
    CLI itself spawns would be left running as an orphan after we give up on it."""
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True
    )
    try:
        output, _ = proc.communicate(timeout=COMMAND_TIMEOUT_SECONDS)
        return proc.returncode, output, False
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        output, _ = proc.communicate()
        return proc.returncode, output, True


def _run_and_report(cmd: list[str], cwd: str) -> dict:
    returncode, output, timed_out = _run_subprocess(cmd, cwd)
    truncated = output[-RESULT_TEXT_TRUNCATE:]
    if timed_out:
        return {
            "status": "failed",
            "result_text": f"timed out after {COMMAND_TIMEOUT_SECONDS}s",
            "messages": [],
            "is_complete": False,
        }
    if returncode != 0:
        return {"status": "failed", "result_text": truncated, "messages": [], "is_complete": False}
    return {"status": "done", "result_text": truncated, "messages": [], "is_complete": False}


def _rejected(reason: str) -> dict:
    return {"status": "failed", "result_text": reason, "messages": [], "is_complete": False}


def execute_resume_message(job: dict, allowed_projects: list[str]) -> dict:
    parts = job["target"].split(":", 1)
    if len(parts) != 2:
        return _rejected(f"malformed job target: {job['target']!r}")
    tool, raw_id = parts

    if tool == "claude-code":
        project_path = claude_code_source.get_project_path(raw_id)
    elif tool == "cursor":
        project_path = cursor_source.get_project_path(raw_id)
    else:
        return _rejected(f"unknown tool: {tool}")

    if not project_path:
        return _rejected("could not resolve project path for session")
    if not allowlist.is_allowed(project_path, allowed_projects):
        return _rejected(f"project path is not allow-listed: {project_path}")

    payload = _parse_json_payload(job.get("payload", ""))
    prompt = payload.get("prompt")
    if not prompt:
        return _rejected("missing prompt in job payload")

    if tool == "claude-code":
        cmd = CLAUDE_CLI + ["-p", "--resume", raw_id, "--permission-mode", "dontAsk", prompt]
    else:
        cmd = CURSOR_AGENT_CLI + ["--resume", raw_id, "-p", "--force", "--workspace", project_path, prompt]

    return _run_and_report(cmd, project_path)


def execute_new_session(job: dict, allowed_projects: list[str]) -> dict:
    project_path = job["target"]
    if not allowlist.is_allowed(project_path, allowed_projects):
        return _rejected(f"project path is not allow-listed: {project_path}")

    payload = _parse_json_payload(job.get("payload", ""))
    prompt = payload.get("prompt")
    tool = payload.get("tool")
    if not prompt or tool not in ("claude-code", "cursor"):
        return _rejected("missing prompt/tool in job payload")

    if tool == "claude-code":
        cmd = CLAUDE_CLI + ["-p", "--permission-mode", "dontAsk", prompt]
    else:
        cmd = CURSOR_AGENT_CLI + ["-p", "--force", "--workspace", project_path, prompt]

    return _run_and_report(cmd, project_path)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd agent && .venv/bin/pytest tests/test_executor_commands.py -v`
Expected: PASS (7 tests). The timeout test takes slightly over 1 real second (the patched `COMMAND_TIMEOUT_SECONDS`), not 30 minutes.

- [ ] **Step 6: Run the full agent suite to check for regressions**

Run: `cd agent && .venv/bin/pytest -v`
Expected: PASS (all tests, including `execute_fetch_full`'s existing tests, untouched by this task)

- [ ] **Step 7: Commit**

```bash
git add agent/agent/executor.py agent/tests/fixtures/fake_cli.py agent/tests/test_executor_commands.py
git commit -m "feat(agent): execute resume_message/new_session jobs with subprocess timeout+kill"
```

---

## Task 12: Agent — dispatch new job types in the main loop

**Files:**
- Modify: `agent/agent/main.py`
- Modify: `agent/tests/test_main_cycle.py`

**Interfaces:**
- Consumes: `executor.execute_resume_message`, `executor.execute_new_session` (Task 11), `config.allowed_projects` (Task 9).
- Produces: updated `run_cycle` dispatch — no new public interface, this is the wiring task.

- [ ] **Step 1: Write the failing test**

Append to `agent/tests/test_main_cycle.py`:

```python
def test_run_cycle_dispatches_resume_message_and_new_session_jobs(tmp_path, monkeypatch):
    calls = []

    monkeypatch.setattr("agent.claude_code_source.list_claude_code_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.list_cursor_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.enrich_with_messages", lambda sessions: None)
    monkeypatch.setattr("agent.uploader.push_sync", lambda base_url, api_key, sessions, client: True)

    def fake_fetch_pending_jobs(base_url, api_key, client):
        return [
            {"id": "j1", "type": "resume_message", "target": "claude-code:abc", "payload": "{}"},
            {"id": "j2", "type": "new_session", "target": "/tmp/demo", "payload": "{}"},
        ]

    def fake_execute_resume_message(job, allowed_projects):
        calls.append(("resume_message", job["id"], allowed_projects))
        return {"status": "done", "result_text": "", "messages": []}

    def fake_execute_new_session(job, allowed_projects):
        calls.append(("new_session", job["id"], allowed_projects))
        return {"status": "done", "result_text": "", "messages": []}

    def fake_report_job_result(base_url, api_key, job_id, status, client, result_text="", messages=None, is_complete=False):
        calls.append(("report", job_id, status))

    monkeypatch.setattr("agent.jobs.fetch_pending_jobs", fake_fetch_pending_jobs)
    monkeypatch.setattr("agent.jobs.report_job_result", fake_report_job_result)
    monkeypatch.setattr("agent.executor.execute_resume_message", fake_execute_resume_message)
    monkeypatch.setattr("agent.executor.execute_new_session", fake_execute_new_session)

    config = main.Config(
        backend_url="http://backend.example",
        api_key="test-key",
        state_path=tmp_path / "sync_state.json",
        interval_seconds=60,
        allowed_projects=["/tmp/demo"],
    )
    main.run_cycle(config, client=None)

    assert ("resume_message", "j1", ["/tmp/demo"]) in calls
    assert ("new_session", "j2", ["/tmp/demo"]) in calls
    assert ("report", "j1", "done") in calls
    assert ("report", "j2", "done") in calls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd agent && .venv/bin/pytest tests/test_main_cycle.py -v -k dispatches_resume`
Expected: FAIL — jobs of type `resume_message`/`new_session` currently fall into the `else` branch and get reported as `failed` with "not supported", so the `fake_execute_*` functions are never called and the assertions fail

- [ ] **Step 3: Update the dispatch in `agent/agent/main.py`**

Replace:

```python
    for job in pending_jobs:
        try:
            if job["type"] == "fetch_full":
                result = executor.execute_fetch_full(job)
            else:
                result = {"status": "failed", "result_text": f"job type {job['type']} not supported in this version"}
```

with:

```python
    for job in pending_jobs:
        try:
            if job["type"] == "fetch_full":
                result = executor.execute_fetch_full(job)
            elif job["type"] == "resume_message":
                result = executor.execute_resume_message(job, config.allowed_projects)
            elif job["type"] == "new_session":
                result = executor.execute_new_session(job, config.allowed_projects)
            else:
                result = {"status": "failed", "result_text": f"job type {job['type']} not supported in this version"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd agent && .venv/bin/pytest tests/test_main_cycle.py -v`
Expected: PASS (all tests, including pre-existing ones)

- [ ] **Step 5: Run the full agent suite to check for regressions**

Run: `cd agent && .venv/bin/pytest -v`
Expected: PASS (all tests)

- [ ] **Step 6: Commit**

```bash
git add agent/agent/main.py agent/tests/test_main_cycle.py
git commit -m "feat(agent): dispatch resume_message/new_session jobs in the poll loop"
```

---

## Task 13: Docs — starter permissions profile, README updates

**Files:**
- Create: `docs/superpowers/reference/remote-agent-permissions.settings.json`
- Modify: `agent/README.md`
- Modify: `backend/README.md`

**Interfaces:**
- Consumes: nothing (documentation only).
- Produces: the artifact referenced by the design spec's Security & Guardrails section.

- [ ] **Step 1: Write the starter permissions profile**

Create `docs/superpowers/reference/remote-agent-permissions.settings.json`:

```json
{
  "permissions": {
    "allow": [
      "Read(**)",
      "Edit(**)",
      "Bash(npm test:*)",
      "Bash(pytest:*)",
      "Bash(python -m pytest:*)"
    ],
    "deny": [
      "Bash(git push:*)",
      "Bash(rm -rf:*)",
      "Bash(sudo:*)",
      "Bash(curl:*)",
      "Bash(wget:*)"
    ]
  }
}
```

- [ ] **Step 2: Verify it's valid JSON**

Run: `python3 -c "import json; json.load(open('docs/superpowers/reference/remote-agent-permissions.settings.json'))"`
Expected: no output, no exception

- [ ] **Step 3: Document the requirement in `agent/README.md`**

Append a new section:

```markdown
## Remote command execution (resume/new-session)

Two things must be configured before the "continue chat" / "new session" features
will do anything:

1. `AI_REMOTE_ALLOWED_PROJECTS` — a comma-separated list of absolute project paths,
   set in this agent's own `.env`. **Must exactly match** the same-named variable set
   in the backend's `.env` on the server — these are two independently-configured
   copies, checked independently by each side (defense in depth), never synced
   between them and never editable via any API route.
2. Each allow-listed project needs a `permissions.allow`/`deny` profile in its own
   `.claude/settings.json` — copy/merge the starter template at
   `docs/superpowers/reference/remote-agent-permissions.settings.json` (adjust the
   `allow` list's test-command patterns to match that specific project). Without
   this, `claude -p --permission-mode dontAsk` has nothing telling it what's safe to
   do unattended. The agent never writes this file for you.

Remote command jobs run with a 30-minute subprocess timeout — if a prompt genuinely
needs longer, it will be killed and reported `failed`.
```

- [ ] **Step 4: Document the pause/audit UI in `backend/README.md`**

Append a new section:

```markdown
## Remote commands: allow-list, kill switch, audit log

- `AI_REMOTE_ALLOWED_PROJECTS` (see `example.env`) controls which projects the
  chat detail page's composer and the `/projects/new` screen will offer at all —
  set it here AND in the agent's own `.env` (two independently-configured copies).
- The list page's "Remote-Befehle pausieren" toggle stops the agent from ever
  receiving new `resume_message`/`new_session` jobs (they stay `pending`); `fetch_full`
  keeps working while paused.
- `/jobs` shows every job ever created — type, target, prompt, status, result — as
  a simple audit trail.
```

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/reference/remote-agent-permissions.settings.json agent/README.md backend/README.md
git commit -m "docs: starter Claude Code permissions profile and remote-command setup notes"
```

---

## Task 14: Manual end-to-end smoke test

**Files:** none (verification only, no code changes).

**Interfaces:** none.

- [ ] **Step 1: Set up a throwaway allow-listed test project**

```bash
mkdir -p /tmp/ai-remote-smoke-test
cp docs/superpowers/reference/remote-agent-permissions.settings.json /tmp/ai-remote-smoke-test/.claude-settings-template.json
mkdir -p /tmp/ai-remote-smoke-test/.claude
cp /tmp/ai-remote-smoke-test/.claude-settings-template.json /tmp/ai-remote-smoke-test/.claude/settings.json
```

- [ ] **Step 2: Run the backend locally with the allow-list set**

Run: `cd backend && DATABASE_PATH=/tmp/smoke.db API_KEY=devkeydevkeydevkey SECRET_KEY=devsecretdevsecret1 SESSION_COOKIE_HTTPS_ONLY=false AI_REMOTE_ALLOWED_PROJECTS=/tmp/ai-remote-smoke-test .venv/bin/uvicorn app.main:app --reload`

- [ ] **Step 3: Run the agent locally with the matching allow-list, pointed at that backend**

Run: `cd agent && AI_REMOTE_BACKEND_URL=http://localhost:8000 AI_REMOTE_API_KEY=devkeydevkeydevkey AI_REMOTE_INTERVAL_SECONDS=10 AI_REMOTE_ALLOWED_PROJECTS=/tmp/ai-remote-smoke-test AI_REMOTE_STATE_PATH=/tmp/smoke-agent-state.json .venv/bin/python -m agent.main`

- [ ] **Step 4: Start a new session via the UI**

Log in at `http://localhost:8000/login`, go to "Neue Session", pick `/tmp/ai-remote-smoke-test`, tool `claude-code`, prompt e.g. "create a file named hello.txt containing the word hello". Confirm the form reports success within ~10s (the shortened poll interval), then check `/tmp/ai-remote-smoke-test/hello.txt` exists on disk.

- [ ] **Step 5: Confirm the new session shows up and continue it**

Within the next sync cycle (~10s), the new session should appear in the chat list. Open it, use the "Befehl senden" composer to send a follow-up prompt (e.g. "now append a second line saying goodbye"), confirm the job completes and the file is updated on disk.

- [ ] **Step 6: Confirm the audit log and kill switch**

Visit `/jobs` — both jobs from Steps 4–5 should be listed with `status: done`. Toggle "Remote-Befehle pausieren" on the list page, submit another command from the composer, confirm it stays visible as `pending` in `/jobs` and the file on disk is *not* further modified. Toggle back on, confirm the same job now gets picked up and completes.

- [ ] **Step 7: Clean up**

```bash
rm -rf /tmp/ai-remote-smoke-test /tmp/smoke.db /tmp/smoke-agent-state.json
```

Stop both the `uvicorn` and agent processes (Ctrl-C).

---
