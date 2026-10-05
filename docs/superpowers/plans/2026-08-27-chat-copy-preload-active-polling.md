# Copy Button, Auto-Preload, Adaptive Poll Interval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-message copy button, auto-preload recent chat history into the periodic sync so chats aren't empty until "Mehr laden" is clicked, and make the background agent's poll interval temporarily speed up (60s → 10s) for 5 minutes after any job-creating user action.

**Architecture:** Three independent slices sharing the same repo, implemented in dependency order: (1) a frontend-only copy button, (2) an agent-side sync payload extension plus a backend guard that only lets it grow — never shrink — stored history, (3) a global `active_until` timestamp in the backend DB that both the poll-interval-reporting `/jobs/pending` endpoint and the ETA estimate read, with the agent's own sleep loop obeying whatever interval the backend hands back each cycle.

**Tech Stack:** FastAPI + Jinja2 + vanilla JS (backend/frontend), Python + httpx (background agent daemon), SQLite, pytest.

## Global Constraints

- Reuse `CHAT_HISTORY_PAGE_SIZE` was the spec's stated intent for the preload count, but that
  setting lives only in the backend's env surface (`backend/app/settings.py`) — the agent process
  (and potentially machine; see `example.env`'s existing `AI_REMOTE_ALLOWED_PROJECTS` split-config
  note) has no access to it. **Deviation from spec, decided during planning:** the preload count is
  instead a local constant `RECENT_MESSAGES_LIMIT = 10` defined independently in
  `agent/agent/claude_code_source.py` and `agent/agent/cursor_source.py` — same default value (10),
  same spirit (no new env knob), but living on the correct side of the process boundary. This
  mirrors the existing `AI_REMOTE_ALLOWED_PROJECTS`/backend `ALLOWED_PROJECTS` precedent of two
  independently-configured copies that happen to need the same value.
- `AI_REMOTE_ACTIVE_INTERVAL_SECONDS` default: `10`. `ACTIVE_INTERVAL_DURATION_MIN` default: `5`.
  Both backend-only settings — do not thread them into the agent's own config, `setup-agent.sh`, or
  the launchd plist (the agent obeys whatever `poll_interval_seconds` the backend returns each
  cycle; it never computes this itself).
- The active-interval trigger is exactly three endpoints: `POST /chats/{id}/fetch-full`,
  `POST /chats/{id}/command`, `POST /projects/command`. Plain page loads never bump it. One global
  window, not per-chat.
- `db.bump_active_interval(...)` must always be called **after** `_compute_eta_seconds` for that
  same request, never before — otherwise the very first action of a session would show a
  10s-instead-of-60s ETA, contradicting both the spec and several pre-existing tests
  (`test_command_response_includes_eta_seconds`, `test_new_session_command_response_includes_eta_seconds`,
  `test_fetch_full_response_includes_eta_seconds_when_agent_never_contacted`) that assert `60`.

---

### Task 1: Copy button per message

**Files:**
- Modify: `backend/app/templates/detail.html:11-18`
- Modify: `backend/app/static/style.css:225` (the `.message time { margin-bottom: 0.35rem; }` rule)
- Modify: `backend/app/static/app.js` (append new block at end of file, after line 361)

**Interfaces:**
- Produces: `.message-content` wrapper div (rendered-markdown HTML) and `.copy-button` element per
  message — no other task depends on these.

This task has no automated test — this repo has no JS test framework (confirmed: `app.js` has zero
existing tests). Verification is manual via `./run.sh`, matching the precedent already set in
`docs/superpowers/specs/2026-08-06-load-more-eta-design.md`'s Testing section for its own JS piece.

- [ ] **Step 1: Update the message markup**

Replace `backend/app/templates/detail.html` lines 11-18:

```html
<div class="messages">
  {% for m in messages %}
  <div class="message message-{{ m.role }}">
    <time>{{ m.timestamp | de_datetime }}</time>
    <div>{{ m.content | markdown | safe }}</div>
  </div>
  {% endfor %}
</div>
```

with:

```html
<div class="messages">
  {% for m in messages %}
  <div class="message message-{{ m.role }}">
    <div class="message-head">
      <time>{{ m.timestamp | de_datetime }}</time>
      <button class="copy-button" type="button" aria-label="Nachricht kopieren">📋</button>
    </div>
    <div class="message-content">{{ m.content | markdown | safe }}</div>
  </div>
  {% endfor %}
</div>
```

- [ ] **Step 2: Add CSS for the new header row and button**

Replace `backend/app/static/style.css` line 225:

```css
.message time { margin-bottom: 0.35rem; }
```

with:

```css
.message-head { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; margin-bottom: 0.35rem; }
.copy-button {
  min-height: auto;
  padding: 0.2rem 0.45rem;
  font-size: 0.85rem;
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-muted);
}
```

- [ ] **Step 3: Add the copy handler**

Append to the end of `backend/app/static/app.js` (after the existing final `});` on line 361):

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
      setTimeout(() => {
        button.textContent = "📋";
      }, 1500);
    });
  });
});
```

- [ ] **Step 4: Manually verify in the browser**

Run: `./run.sh`, log in, open any chat detail page with at least one message (or send a command to
create one).

Expected:
- A small 📋 button appears next to the timestamp on every message.
- Clicking it flips the button to ✓ for ~1.5s, then back to 📋.
- Pasting into a rich-text target (e.g. a Gmail compose window, or `document.execCommand`-editable
  area via DevTools) preserves bold/lists/code formatting from a markdown-formatted message.
- Pasting into a plain `<textarea>` or DevTools console shows the plain text only.

- [ ] **Step 5: Commit**

```bash
git add backend/app/templates/detail.html backend/app/static/style.css backend/app/static/app.js
git commit -m "feat(frontend): add per-message copy button with rich-text clipboard support"
```

---

### Task 2: `claude_code_source.py` — collect recent messages in the existing metadata parse

**Files:**
- Modify: `agent/agent/claude_code_source.py:1-126`
- Test: `agent/tests/test_claude_code_source.py`

**Interfaces:**
- Produces: every dict returned by `list_claude_code_sessions()` / `_parse_session_file()` now
  additionally has a `"recent_messages": list[{"idx": int, "role": str, "timestamp": str,
  "content": str}]` key — the last `RECENT_MESSAGES_LIMIT` (10) text-bearing messages, in original
  file order, with `idx` values matching what `get_full_messages()` would assign (i.e. absolute
  position among text-bearing messages, not renumbered from 0 after slicing).

- [ ] **Step 1: Write the failing tests**

Add to `agent/tests/test_claude_code_source.py` (needs `import json` added to the top-of-file
imports alongside the existing `os`/`shutil`/`Path`/`pytest` imports):

```python
def test_list_claude_code_sessions_includes_recent_messages(projects_dir):
    sessions = claude_code_source.list_claude_code_sessions(projects_dir)
    session = sessions[0]
    assert session["recent_messages"] == [
        {"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00.100Z", "content": "What changed recently?"},
        {"idx": 1, "role": "assistant", "timestamp": "2026-08-01T10:00:03.000Z", "content": "Three commits landed yesterday."},
    ]


def test_recent_messages_capped_at_limit_with_correct_original_idx(tmp_path):
    project_dir = tmp_path / "-Users-test-project"
    project_dir.mkdir()
    lines = []
    for i in range(15):
        lines.append(
            json.dumps(
                {
                    "type": "user" if i % 2 == 0 else "assistant",
                    "message": {"content": [{"type": "text", "text": f"msg{i}"}]},
                    "timestamp": f"2026-08-01T10:00:{i:02d}.000Z",
                }
            )
        )
    (project_dir / "many-messages.jsonl").write_text("\n".join(lines) + "\n")

    sessions = claude_code_source.list_claude_code_sessions(tmp_path)
    recent = sessions[0]["recent_messages"]
    assert len(recent) == 10
    assert [m["idx"] for m in recent] == list(range(5, 15))
    assert [m["content"] for m in recent] == [f"msg{i}" for i in range(5, 15)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd agent && .venv/bin/pytest tests/test_claude_code_source.py -v -k "recent_messages"`
Expected: FAIL — `KeyError: 'recent_messages'`

- [ ] **Step 3: Implement — merge message collection into the existing single-pass parse**

Replace `_parse_session_file` in `agent/agent/claude_code_source.py` (currently lines 57-126) with:

```python
RECENT_MESSAGES_LIMIT = 10


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
    text_messages = []
    text_message_count = 0

    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            event_type = event.get("type")
            timestamp = event.get("timestamp")

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
                    text_messages.append(
                        {"idx": text_message_count, "role": "user", "timestamp": timestamp or "", "content": text}
                    )
                    text_message_count += 1
                if timestamp:
                    if first_timestamp is None:
                        first_timestamp = timestamp
                    last_timestamp = timestamp
            elif event_type == "assistant":
                message_count += 1
                text = _extract_text(event.get("message", {}))
                if text:
                    last_assistant_text = text
                    text_messages.append(
                        {"idx": text_message_count, "role": "assistant", "timestamp": timestamp or "", "content": text}
                    )
                    text_message_count += 1
                if timestamp:
                    if first_timestamp is None:
                        first_timestamp = timestamp
                    last_timestamp = timestamp

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
        "recent_messages": text_messages[-RECENT_MESSAGES_LIMIT:],
    }
```

`get_full_messages` (used by the on-demand `fetch_full` job executor) is unchanged — it still does
its own independent full read, since a `fetch_full` job can request the entire history
(`count=None`), which this capped preload doesn't cover.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd agent && .venv/bin/pytest tests/test_claude_code_source.py -v`
Expected: PASS (all tests, including the two new ones and all pre-existing ones — `message_count`,
`last_message_preview`, etc. are unchanged)

- [ ] **Step 5: Commit**

```bash
git add agent/agent/claude_code_source.py agent/tests/test_claude_code_source.py
git commit -m "feat(agent): collect recent text messages during the existing session-file parse"
```

---

### Task 3: `cursor_source.py` — attach recent messages from the already-loaded bubbles

**Files:**
- Modify: `agent/agent/cursor_source.py:118-133` (`enrich_with_messages`)
- Test: `agent/tests/test_cursor_source.py`

**Interfaces:**
- Produces: `enrich_with_messages` now also sets `session["recent_messages"]` (same shape as
  Task 2) on every session dict it enriches, for free — `_load_bubbles` already loads every bubble
  for a changed session to compute `message_count`/`last_message_preview`; this just also keeps the
  tail instead of discarding it.

- [ ] **Step 1: Write the failing tests**

Add to `agent/tests/test_cursor_source.py`:

```python
def test_enrich_with_messages_fills_recent_messages(search_db, state_db, workspace_storage, monkeypatch):
    sessions = cursor_source.list_cursor_sessions(search_db)
    monkeypatch.setattr(cursor_source, "CURSOR_WORKSPACE_STORAGE", workspace_storage)
    cursor_source.enrich_with_messages(sessions, state_db)

    assert sessions[0]["recent_messages"] == [
        {"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00.000Z", "content": "Hi there"},
        {"idx": 1, "role": "assistant", "timestamp": "2026-08-01T10:00:10.000Z", "content": "Sure, here you go."},
    ]


def test_enrich_with_messages_caps_recent_messages_at_limit(tmp_path):
    search_path = tmp_path / "conversation-search.db"
    conn = sqlite3.connect(search_path)
    conn.execute(
        "CREATE TABLE conversations (fts_rowid INTEGER PRIMARY KEY, source TEXT, scope TEXT, "
        "id TEXT, title TEXT, updated_at INTEGER, is_archived INTEGER)"
    )
    conn.execute("INSERT INTO conversations VALUES (1, 'local', '', 'composer-many', 'Many', 1754040000000, 0)")
    conn.commit()
    conn.close()

    state_path = tmp_path / "state.vscdb"
    state_conn = sqlite3.connect(state_path)
    state_conn.execute("CREATE TABLE composerHeaders (composerId TEXT PRIMARY KEY, workspaceId TEXT)")
    state_conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    for i in range(15):
        state_conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (
                f"bubbleId:composer-many:b{i:02d}",
                json.dumps(
                    {
                        "type": 1 if i % 2 == 0 else 2,
                        "createdAt": f"2026-08-01T10:00:{i:02d}.000Z",
                        "text": f"msg{i}",
                    }
                ),
            ),
        )
    state_conn.commit()
    state_conn.close()

    sessions = cursor_source.list_cursor_sessions(search_path)
    cursor_source.enrich_with_messages(sessions, state_path)

    recent = sessions[0]["recent_messages"]
    assert len(recent) == 10
    assert [m["idx"] for m in recent] == list(range(5, 15))
    assert [m["content"] for m in recent] == [f"msg{i}" for i in range(5, 15)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd agent && .venv/bin/pytest tests/test_cursor_source.py -v -k "recent_messages"`
Expected: FAIL — `KeyError: 'recent_messages'`

- [ ] **Step 3: Implement**

Replace `enrich_with_messages` in `agent/agent/cursor_source.py` (currently lines 118-133) with:

```python
RECENT_MESSAGES_LIMIT = 10


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
                indexed = [
                    {"idx": i, "role": b["role"], "timestamp": b["timestamp"], "content": b["content"]}
                    for i, b in enumerate(bubbles)
                ]
                session["recent_messages"] = indexed[-RECENT_MESSAGES_LIMIT:]
            workspace_id = _load_workspace_id(conn, composer_id)
            if workspace_id:
                session["project_path"] = resolve_workspace_path(workspace_id, CURSOR_WORKSPACE_STORAGE) or ""
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd agent && .venv/bin/pytest tests/test_cursor_source.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/agent/cursor_source.py agent/tests/test_cursor_source.py
git commit -m "feat(agent): attach recent messages from already-loaded cursor bubbles"
```

---

### Task 4: Backend — accept and apply `recent_messages` from sync, without ever shrinking loaded history

**Files:**
- Modify: `backend/app/models.py` (whole file — reorder `MessageIn` above `SessionIn`, add field)
- Modify: `backend/app/db.py` (add `apply_recent_messages`, after `replace_messages` at line 199)
- Modify: `backend/app/main.py:89-94` (`sync_index`)
- Test: `backend/tests/test_db.py`
- Test: `backend/tests/test_auth_and_sync.py`

**Interfaces:**
- Consumes: `db.get_session(conn, session_id) -> dict | None` (existing), `db.replace_messages(conn,
  session_id, messages, is_complete=None) -> None` (existing).
- Produces: `db.apply_recent_messages(conn: sqlite3.Connection, session_id: str, messages:
  list[dict]) -> None` — later tasks do not depend on this, it's called only from `sync_index`.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_db.py`:

```python
def test_apply_recent_messages_populates_when_nothing_loaded(conn):
    db.upsert_session(conn, _sample_session())
    db.apply_recent_messages(
        conn,
        "claude-code:abc",
        [
            {"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": "hi"},
            {"idx": 1, "role": "assistant", "timestamp": "2026-08-01T10:01:00Z", "content": "hello"},
        ],
    )
    session = db.get_session(conn, "claude-code:abc")
    assert session["loaded_message_count"] == 2
    assert db.get_messages(conn, "claude-code:abc")[0]["content"] == "hi"


def test_apply_recent_messages_does_not_shrink_already_loaded_history(conn):
    db.upsert_session(conn, _sample_session() | {"message_count": 200})
    full_history = [
        {"idx": i, "role": "user", "timestamp": f"2026-08-01T10:{i:02d}:00Z", "content": str(i)}
        for i in range(200)
    ]
    db.replace_messages(conn, "claude-code:abc", full_history, is_complete=True)

    db.apply_recent_messages(
        conn,
        "claude-code:abc",
        [{"idx": 199, "role": "user", "timestamp": "2026-08-01T13:19:00Z", "content": "199"}],
    )
    session = db.get_session(conn, "claude-code:abc")
    assert session["loaded_message_count"] == 200  # untouched — 1 < 200, guard skips


def test_apply_recent_messages_is_noop_when_empty(conn):
    db.upsert_session(conn, _sample_session())
    db.apply_recent_messages(conn, "claude-code:abc", [])
    session = db.get_session(conn, "claude-code:abc")
    assert session["loaded_message_count"] == 0
```

Add to `backend/tests/test_auth_and_sync.py`:

```python
def test_sync_index_applies_recent_messages(client):
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
                "message_count": 2,
                "last_message_preview": "hi",
                "status": "idle",
                "recent_messages": [
                    {"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": "hi"},
                    {"idx": 1, "role": "assistant", "timestamp": "2026-08-01T10:01:00Z", "content": "hello"},
                ],
            }
        ]
    }
    response = client.post(
        "/sync/index", json=payload, headers={"Authorization": "Bearer test-api-key-1234"}
    )
    assert response.status_code == 200

    from app import db as db_module
    import os

    conn = db_module.get_connection(os.environ["DATABASE_PATH"])
    session = db_module.get_session(conn, "claude-code:abc")
    messages = db_module.get_messages(conn, "claude-code:abc")
    conn.close()
    assert session["loaded_message_count"] == 2
    assert [m["content"] for m in messages] == ["hi", "hello"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_db.py tests/test_auth_and_sync.py -v -k "recent_messages"`
Expected: FAIL — `AttributeError: module 'app.db' has no attribute 'apply_recent_messages'` (the
`test_auth_and_sync.py` one fails with `422` since `recent_messages` isn't yet a valid field)

- [ ] **Step 3: Implement — models.py**

Rewrite `backend/app/models.py` in full (reordering `MessageIn` above `SessionIn` so `SessionIn` can
reference it directly, and adding the new field):

```python
from typing import Literal

from pydantic import BaseModel


class MessageIn(BaseModel):
    idx: int
    role: str
    timestamp: str
    content: str


class SessionIn(BaseModel):
    id: str
    tool: Literal["claude-code", "cursor"]
    entrypoint: str = ""
    project_path: str = ""
    title: str
    created_at: str
    last_updated_at: str
    message_count: int = 0
    last_message_preview: str = ""
    status: str = "idle"
    recent_messages: list[MessageIn] = []


class SyncIndexRequest(BaseModel):
    sessions: list[SessionIn]


class JobCompleteRequest(BaseModel):
    status: str
    result_text: str = ""
    messages: list[MessageIn] = []
    is_complete: bool | None = None


class CommandRequest(BaseModel):
    prompt: str


class NewSessionCommandRequest(BaseModel):
    project_path: str
    tool: Literal["claude-code", "cursor"]
    prompt: str
```

- [ ] **Step 4: Implement — db.py**

Add to `backend/app/db.py`, immediately after `replace_messages` (currently ends at line 199, right
before `def create_job`):

```python
def apply_recent_messages(conn: sqlite3.Connection, session_id: str, messages: list[dict]) -> None:
    if not messages:
        return
    session = get_session(conn, session_id)
    if session is None or len(messages) <= session["loaded_message_count"]:
        return
    replace_messages(conn, session_id, messages)
```

- [ ] **Step 5: Implement — wire into `sync_index`**

Replace `backend/app/main.py` lines 89-94:

```python
@app.post("/sync/index", dependencies=[Depends(require_api_key)])
def sync_index(body: SyncIndexRequest, conn=Depends(db.get_db_dependency)):
    for session in body.sessions:
        db.upsert_session(conn, session.model_dump())
    db.record_agent_contact(conn)
    return {"received": len(body.sessions)}
```

with:

```python
@app.post("/sync/index", dependencies=[Depends(require_api_key)])
def sync_index(body: SyncIndexRequest, conn=Depends(db.get_db_dependency)):
    for session in body.sessions:
        db.upsert_session(conn, session.model_dump())
        db.apply_recent_messages(conn, session.id, [m.model_dump() for m in session.recent_messages])
    db.record_agent_contact(conn)
    return {"received": len(body.sessions)}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/ -v`
Expected: PASS — full backend suite, including all pre-existing tests (proves the `SessionIn`
reorder and new optional field don't break any existing sync payload).

- [ ] **Step 7: Commit**

```bash
git add backend/app/models.py backend/app/db.py backend/app/main.py backend/tests/test_db.py backend/tests/test_auth_and_sync.py
git commit -m "feat(backend): auto-preload recent messages from sync without shrinking loaded history"
```

---

### Task 5: Settings + deploy plumbing for the active poll interval

**Files:**
- Modify: `backend/app/settings.py:15` (after `AI_REMOTE_INTERVAL_SECONDS`)
- Modify: `docker-compose.yml` (`backend.environment` block)
- Modify: `deploy-production-scp.sh` (compose heredoc `environment` block)
- Modify: `example.env` (documentation, after the `AI_REMOTE_INTERVAL_SECONDS` comment block)
- Test: `backend/tests/test_settings.py`

**Interfaces:**
- Produces: `settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS: int` (default 10),
  `settings.ACTIVE_INTERVAL_DURATION_MIN: int` (default 5) — consumed by Task 6's DB functions
  (as caller-supplied params, not imported directly) and Task 7/9's endpoints.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_settings.py` (mirroring the existing `AI_REMOTE_INTERVAL_SECONDS` tests
at lines 203-246):

```python
def test_ai_remote_active_interval_seconds_default():
    full_env = {**os.environ, "API_KEY": "a" * 20, "SECRET_KEY": "b" * 20}
    full_env.pop("AI_REMOTE_ACTIVE_INTERVAL_SECONDS", None)
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "10"


def test_ai_remote_active_interval_seconds_override():
    full_env = {**os.environ, "API_KEY": "a" * 20, "SECRET_KEY": "b" * 20, "AI_REMOTE_ACTIVE_INTERVAL_SECONDS": "5"}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "5"


def test_ai_remote_active_interval_seconds_empty_string_falls_back_to_default():
    full_env = {**os.environ, "API_KEY": "a" * 20, "SECRET_KEY": "b" * 20, "AI_REMOTE_ACTIVE_INTERVAL_SECONDS": ""}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "10"


def test_active_interval_duration_min_default():
    full_env = {**os.environ, "API_KEY": "a" * 20, "SECRET_KEY": "b" * 20}
    full_env.pop("ACTIVE_INTERVAL_DURATION_MIN", None)
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.ACTIVE_INTERVAL_DURATION_MIN)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "5"


def test_active_interval_duration_min_override():
    full_env = {**os.environ, "API_KEY": "a" * 20, "SECRET_KEY": "b" * 20, "ACTIVE_INTERVAL_DURATION_MIN": "2"}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.ACTIVE_INTERVAL_DURATION_MIN)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2"


def test_active_interval_duration_min_empty_string_falls_back_to_default():
    full_env = {**os.environ, "API_KEY": "a" * 20, "SECRET_KEY": "b" * 20, "ACTIVE_INTERVAL_DURATION_MIN": ""}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.ACTIVE_INTERVAL_DURATION_MIN)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "5"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_settings.py -v -k "active_interval"`
Expected: FAIL — `AttributeError: module 'app.settings' has no attribute 'AI_REMOTE_ACTIVE_INTERVAL_SECONDS'`

- [ ] **Step 3: Implement — settings.py**

Add to `backend/app/settings.py`, immediately after line 15
(`AI_REMOTE_INTERVAL_SECONDS = int(os.environ.get("AI_REMOTE_INTERVAL_SECONDS") or "60")`):

```python
AI_REMOTE_ACTIVE_INTERVAL_SECONDS = int(os.environ.get("AI_REMOTE_ACTIVE_INTERVAL_SECONDS") or "10")
ACTIVE_INTERVAL_DURATION_MIN = int(os.environ.get("ACTIVE_INTERVAL_DURATION_MIN") or "5")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_settings.py -v`
Expected: PASS

- [ ] **Step 5: Deploy plumbing (no automated test — verified by inspection)**

In `docker-compose.yml`, add after the existing
`- AI_REMOTE_INTERVAL_SECONDS=${AI_REMOTE_INTERVAL_SECONDS:-60}` line inside `backend.environment`:

```yaml
      - AI_REMOTE_ACTIVE_INTERVAL_SECONDS=${AI_REMOTE_ACTIVE_INTERVAL_SECONDS:-10}
      - ACTIVE_INTERVAL_DURATION_MIN=${ACTIVE_INTERVAL_DURATION_MIN:-5}
```

In `deploy-production-scp.sh`, add the same two lines (with the `\$` escaping already used by that
heredoc) right after its existing `- AI_REMOTE_INTERVAL_SECONDS=\${AI_REMOTE_INTERVAL_SECONDS:-60}`
line:

```yaml
      - AI_REMOTE_ACTIVE_INTERVAL_SECONDS=\${AI_REMOTE_ACTIVE_INTERVAL_SECONDS:-10}
      - ACTIVE_INTERVAL_DURATION_MIN=\${ACTIVE_INTERVAL_DURATION_MIN:-5}
```

In `example.env`, add a new comment block after the existing `AI_REMOTE_INTERVAL_SECONDS=60` line:

```bash
# How fast the agent polls while "active" — i.e. for ACTIVE_INTERVAL_DURATION_MIN minutes after
# any job-creating action (a command sent, "Mehr laden"/"Gesamte Historie laden", a new session
# started). Backend-only: unlike AI_REMOTE_INTERVAL_SECONDS, the agent never reads this itself —
# it just obeys whatever poll_interval_seconds the backend's /jobs/pending response tells it to
# use next.
AI_REMOTE_ACTIVE_INTERVAL_SECONDS=10

# How many minutes the faster active interval above stays in effect after the most recent
# job-creating action, before falling back to AI_REMOTE_INTERVAL_SECONDS. One global window
# across all chats, not per-chat.
ACTIVE_INTERVAL_DURATION_MIN=5
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/settings.py backend/tests/test_settings.py docker-compose.yml deploy-production-scp.sh example.env
git commit -m "feat(backend): add active-poll-interval settings and deploy plumbing"
```

---

### Task 6: DB — global active-interval window (schema, migration, read/write helpers)

**Files:**
- Modify: `backend/app/schema.sql:42-46` (`agent_status` table)
- Modify: `backend/app/db.py` (migration in `init_db`, two new functions after `get_last_agent_contact`)
- Test: `backend/tests/test_db.py`

**Interfaces:**
- Produces: `db.bump_active_interval(conn: sqlite3.Connection, duration_min: int) -> None` and
  `db.current_poll_interval_seconds(conn: sqlite3.Connection, default_seconds: int, active_seconds:
  int) -> int` — both consumed by Task 7 (endpoints) and Task 9 (UI indicator). Task 8 (agent loop)
  does not call these directly; it only consumes the JSON shape Task 7 produces.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_db.py`:

```python
def test_current_poll_interval_seconds_defaults_when_never_bumped(conn):
    assert db.current_poll_interval_seconds(conn, 60, 10) == 60


def test_current_poll_interval_seconds_returns_active_within_window(conn):
    db.bump_active_interval(conn, 5)
    assert db.current_poll_interval_seconds(conn, 60, 10) == 10


def test_current_poll_interval_seconds_reverts_after_window_expires(conn):
    conn.execute(
        "UPDATE agent_status SET active_until = ? WHERE id = 1",
        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
    )
    conn.commit()
    assert db.current_poll_interval_seconds(conn, 60, 10) == 60


def test_migration_adds_active_until_to_pre_existing_db(tmp_path):
    db_path = str(tmp_path / "legacy3.db")
    legacy_conn = db.get_connection(db_path)
    legacy_conn.execute(
        "CREATE TABLE agent_status (id INTEGER PRIMARY KEY CHECK (id = 1), last_contact_at TEXT)"
    )
    legacy_conn.execute("INSERT INTO agent_status (id, last_contact_at) VALUES (1, NULL)")
    legacy_conn.commit()
    legacy_conn.close()

    migrated_conn = db.get_connection(db_path)
    db.init_db(migrated_conn)  # must not fail on a table that already exists without the new column

    columns = {row["name"] for row in migrated_conn.execute("PRAGMA table_info(agent_status)").fetchall()}
    assert "active_until" in columns
    migrated_conn.close()
```

(`datetime`, `timedelta`, `timezone` are already imported at the top of `test_db.py`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_db.py -v -k "poll_interval or active_until"`
Expected: FAIL — `AttributeError: module 'app.db' has no attribute 'current_poll_interval_seconds'`

- [ ] **Step 3: Implement — schema.sql**

Replace `backend/app/schema.sql` lines 42-46:

```sql
CREATE TABLE IF NOT EXISTS agent_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_contact_at TEXT
);
INSERT OR IGNORE INTO agent_status (id, last_contact_at) VALUES (1, NULL);
```

with:

```sql
CREATE TABLE IF NOT EXISTS agent_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_contact_at TEXT,
    active_until TEXT
);
INSERT OR IGNORE INTO agent_status (id, last_contact_at) VALUES (1, NULL);
```

- [ ] **Step 4: Implement — db.py migration**

Replace `init_db` in `backend/app/db.py` (currently lines 32-35):

```python
def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    _migrate_add_loaded_message_count(conn)
```

with:

```python
def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    _migrate_add_loaded_message_count(conn)
    _migrate_add_active_until(conn)
```

Add a new migration function right after `_migrate_add_loaded_message_count` (currently ends at
line 46):

```python
def _migrate_add_active_until(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(agent_status)").fetchall()}
    if "active_until" not in columns:
        conn.execute("ALTER TABLE agent_status ADD COLUMN active_until TEXT")
        conn.commit()
```

- [ ] **Step 5: Implement — db.py read/write helpers**

Add to `backend/app/db.py`, immediately after `get_last_agent_contact` (currently ends at line 282,
right before `def get_remote_commands_paused`):

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

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_db.py -v`
Expected: PASS — full file, including all pre-existing tests (proves the schema/migration change
doesn't disturb `loaded_message_count`'s own migration or any other `agent_status` usage).

- [ ] **Step 7: Commit**

```bash
git add backend/app/schema.sql backend/app/db.py backend/tests/test_db.py
git commit -m "feat(backend): add global active-poll-interval window to agent_status"
```

---

### Task 7: Backend endpoints — bump the active window, report the live interval, use it for ETA

**Files:**
- Modify: `backend/app/main.py` — `jobs_pending` (lines 42-47), `fetch_full` (lines 70-80),
  `send_command` (lines 196-209), `send_new_session_command` (lines 224-237)
- Test: `backend/tests/test_commands.py`
- Test: `backend/tests/test_detail_and_jobs.py`

**Interfaces:**
- Consumes: `db.current_poll_interval_seconds` and `db.bump_active_interval` from Task 6;
  `settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS` / `settings.ACTIVE_INTERVAL_DURATION_MIN` from Task 5.
- Produces: `GET /jobs/pending` response gains `poll_interval_seconds: int` — Task 8 (agent loop)
  consumes this field by name.

**Reminder from Global Constraints:** compute `eta_seconds` from the interval **before** calling
`bump_active_interval` in each of the three job-creating endpoints — bumping first would make even
the very first action of a session report a 10s ETA instead of 60s, breaking existing tests.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_commands.py`:

```python
def test_command_second_call_reflects_active_interval(logged_in_client):
    _sync_session(logged_in_client)
    first = logged_in_client.post("/chats/claude-code:abc/command", json={"prompt": "keep going"})
    assert first.json()["eta_seconds"] == 60  # first action ever: pre-bump default interval still applies

    second = logged_in_client.post("/chats/claude-code:abc/command", json={"prompt": "again"})
    assert second.json()["eta_seconds"] == 10  # inside the window the first action just opened


def test_jobs_pending_reports_default_poll_interval_when_idle(logged_in_client):
    pending = logged_in_client.get(
        "/jobs/pending", headers={"Authorization": "Bearer test-api-key-1234"}
    ).json()
    assert pending["poll_interval_seconds"] == 60


def test_jobs_pending_reports_active_poll_interval_after_action(logged_in_client):
    _sync_session(logged_in_client)
    logged_in_client.post("/chats/claude-code:abc/command", json={"prompt": "go"})

    pending = logged_in_client.get(
        "/jobs/pending", headers={"Authorization": "Bearer test-api-key-1234"}
    ).json()
    assert pending["poll_interval_seconds"] == 10
```

Add to `backend/tests/test_detail_and_jobs.py`:

```python
def test_fetch_full_second_call_reflects_active_interval(logged_in_client):
    _sync_one(logged_in_client)
    first = logged_in_client.post("/chats/claude-code:abc/fetch-full")
    assert first.json()["eta_seconds"] == 60

    second = logged_in_client.post("/chats/claude-code:abc/fetch-full")
    assert second.json()["eta_seconds"] == 10
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_commands.py tests/test_detail_and_jobs.py -v -k "active_interval or poll_interval"`
Expected: FAIL — `KeyError: 'poll_interval_seconds'` / second-call `eta_seconds` still `60`

- [ ] **Step 3: Implement — `jobs_pending`**

Replace `backend/app/main.py` lines 42-47:

```python
@app.get("/jobs/pending", dependencies=[Depends(require_api_key)])
def jobs_pending(conn=Depends(db.get_db_dependency)):
    db.fail_stale_jobs(conn)
    jobs = db.claim_pending_jobs(conn)
    db.record_agent_contact(conn)
    return {"jobs": jobs}
```

with:

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

- [ ] **Step 4: Implement — `fetch_full`**

Replace `backend/app/main.py` lines 70-80:

```python
@app.post("/chats/{session_id}/fetch-full", dependencies=[Depends(require_session)])
def fetch_full(session_id: str, full: bool = False, conn=Depends(db.get_db_dependency)):
    if full:
        job_id = db.create_job(conn, "fetch_full", session_id)
    else:
        session = db.get_session(conn, session_id)
        current = session["loaded_message_count"] if session else 0
        next_count = current + settings.CHAT_HISTORY_PAGE_SIZE
        job_id = db.create_job(conn, "fetch_full", session_id, payload=json.dumps({"count": next_count}))
    eta_seconds = _compute_eta_seconds(db.get_last_agent_contact(conn), settings.AI_REMOTE_INTERVAL_SECONDS)
    return {"job_id": job_id, "eta_seconds": eta_seconds}
```

with:

```python
@app.post("/chats/{session_id}/fetch-full", dependencies=[Depends(require_session)])
def fetch_full(session_id: str, full: bool = False, conn=Depends(db.get_db_dependency)):
    if full:
        job_id = db.create_job(conn, "fetch_full", session_id)
    else:
        session = db.get_session(conn, session_id)
        current = session["loaded_message_count"] if session else 0
        next_count = current + settings.CHAT_HISTORY_PAGE_SIZE
        job_id = db.create_job(conn, "fetch_full", session_id, payload=json.dumps({"count": next_count}))
    interval = db.current_poll_interval_seconds(
        conn, settings.AI_REMOTE_INTERVAL_SECONDS, settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS
    )
    eta_seconds = _compute_eta_seconds(db.get_last_agent_contact(conn), interval)
    db.bump_active_interval(conn, settings.ACTIVE_INTERVAL_DURATION_MIN)
    return {"job_id": job_id, "eta_seconds": eta_seconds}
```

- [ ] **Step 5: Implement — `send_command`**

Replace `backend/app/main.py` lines 196-209:

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
    eta_seconds = _compute_eta_seconds(db.get_last_agent_contact(conn), settings.AI_REMOTE_INTERVAL_SECONDS)
    return {"job_id": job_id, "eta_seconds": eta_seconds}
```

with:

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
    interval = db.current_poll_interval_seconds(
        conn, settings.AI_REMOTE_INTERVAL_SECONDS, settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS
    )
    eta_seconds = _compute_eta_seconds(db.get_last_agent_contact(conn), interval)
    db.bump_active_interval(conn, settings.ACTIVE_INTERVAL_DURATION_MIN)
    return {"job_id": job_id, "eta_seconds": eta_seconds}
```

- [ ] **Step 6: Implement — `send_new_session_command`**

Replace `backend/app/main.py` lines 224-237:

```python
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
    eta_seconds = _compute_eta_seconds(db.get_last_agent_contact(conn), settings.AI_REMOTE_INTERVAL_SECONDS)
    return {"job_id": job_id, "eta_seconds": eta_seconds}
```

with:

```python
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
    interval = db.current_poll_interval_seconds(
        conn, settings.AI_REMOTE_INTERVAL_SECONDS, settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS
    )
    eta_seconds = _compute_eta_seconds(db.get_last_agent_contact(conn), interval)
    db.bump_active_interval(conn, settings.ACTIVE_INTERVAL_DURATION_MIN)
    return {"job_id": job_id, "eta_seconds": eta_seconds}
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/ -v`
Expected: PASS — full backend suite. In particular, confirm the pre-existing
`test_command_response_includes_eta_seconds`, `test_new_session_command_response_includes_eta_seconds`,
and `test_fetch_full_response_includes_eta_seconds_when_agent_never_contacted` (all asserting `60`
on the first-ever call) still pass — this is the regression check for the ordering constraint.

- [ ] **Step 8: Commit**

```bash
git add backend/app/main.py backend/tests/test_commands.py backend/tests/test_detail_and_jobs.py
git commit -m "feat(backend): bump active poll interval on job-creating actions, report it to the agent"
```

---

### Task 8: Agent loop — obey the backend's reported poll interval

**Files:**
- Modify: `agent/agent/jobs.py:1-8` (`fetch_pending_jobs`)
- Modify: `agent/agent/main.py` (`run_cycle`, `main`)
- Modify: `agent/tests/test_main_cycle.py` (update 4 existing fakes that currently return a bare list)
- Modify: `agent/tests/test_uploader_and_jobs.py:37-44` (`test_fetch_pending_jobs_parses_response`)
- Test: `agent/tests/test_main_cycle.py` (new tests)

**Interfaces:**
- Consumes: `GET /jobs/pending` now returns `{"jobs": [...], "poll_interval_seconds": int}` (Task 7).
- Produces: `run_cycle(config, client) -> int | None` (previously returned `None` implicitly) —
  `main()` uses this return value for its next `time.sleep()`, falling back to
  `config.interval_seconds` when `None`.

**This task changes an existing interface**: `agent.jobs.fetch_pending_jobs` used to return a bare
`list[dict]`; it now returns the full response `dict`. Every existing test that monkeypatches it
with a fake returning a plain list must be updated in this same task, or `run_cycle` will crash with
`TypeError: list indices must be integers or slices, not str` — not merely fail an assertion.

- [ ] **Step 1: Update the four pre-existing fakes in `agent/tests/test_main_cycle.py`**

In `test_run_cycle_pushes_deltas_and_executes_pending_jobs` (line ~20):

```python
    def fake_fetch_pending_jobs(base_url, api_key, client):
        return [{"id": "j1", "type": "fetch_full", "target": "claude-code:abc"}]
```

becomes:

```python
    def fake_fetch_pending_jobs(base_url, api_key, client):
        return {"jobs": [{"id": "j1", "type": "fetch_full", "target": "claude-code:abc"}], "poll_interval_seconds": 60}
```

In `test_run_cycle_enriches_only_changed_cursor_sessions` (line ~71):

```python
    monkeypatch.setattr("agent.jobs.fetch_pending_jobs", lambda base_url, api_key, client: [])
```

becomes:

```python
    monkeypatch.setattr(
        "agent.jobs.fetch_pending_jobs", lambda base_url, api_key, client: {"jobs": [], "poll_interval_seconds": 60}
    )
```

In `test_run_cycle_continues_after_job_failure` (line ~99):

```python
    def fake_fetch_pending_jobs(base_url, api_key, client):
        return [
            {"id": "j1", "type": "fetch_full", "target": "claude-code:abc"},
            {"id": "j2", "type": "fetch_full", "target": "claude-code:def"},
        ]
```

becomes:

```python
    def fake_fetch_pending_jobs(base_url, api_key, client):
        return {
            "jobs": [
                {"id": "j1", "type": "fetch_full", "target": "claude-code:abc"},
                {"id": "j2", "type": "fetch_full", "target": "claude-code:def"},
            ],
            "poll_interval_seconds": 60,
        }
```

In `test_run_cycle_dispatches_resume_message_and_new_session_jobs` (line ~141):

```python
    def fake_fetch_pending_jobs(base_url, api_key, client):
        return [
            {"id": "j1", "type": "resume_message", "target": "claude-code:abc", "payload": "{}"},
            {"id": "j2", "type": "new_session", "target": "/tmp/demo", "payload": "{}"},
        ]
```

becomes:

```python
    def fake_fetch_pending_jobs(base_url, api_key, client):
        return {
            "jobs": [
                {"id": "j1", "type": "resume_message", "target": "claude-code:abc", "payload": "{}"},
                {"id": "j2", "type": "new_session", "target": "/tmp/demo", "payload": "{}"},
            ],
            "poll_interval_seconds": 60,
        }
```

- [ ] **Step 2: Update `agent/tests/test_uploader_and_jobs.py:37-44`**

Replace:

```python
def test_fetch_pending_jobs_parses_response():
    def handler(request):
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"jobs": [{"id": "j1", "type": "fetch_full", "target": "claude-code:abc"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = jobs.fetch_pending_jobs("http://backend.example", "test-key", client)
    assert result == [{"id": "j1", "type": "fetch_full", "target": "claude-code:abc"}]
```

with:

```python
def test_fetch_pending_jobs_parses_response():
    def handler(request):
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "jobs": [{"id": "j1", "type": "fetch_full", "target": "claude-code:abc"}],
                "poll_interval_seconds": 60,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = jobs.fetch_pending_jobs("http://backend.example", "test-key", client)
    assert result == {
        "jobs": [{"id": "j1", "type": "fetch_full", "target": "claude-code:abc"}],
        "poll_interval_seconds": 60,
    }
```

- [ ] **Step 3: Write new failing tests for the adaptive sleep behavior**

Add to `agent/tests/test_main_cycle.py`:

```python
def test_run_cycle_returns_poll_interval_from_response(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.claude_code_source.list_claude_code_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.list_cursor_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.enrich_with_messages", lambda sessions: None)
    monkeypatch.setattr("agent.uploader.push_sync", lambda base_url, api_key, sessions, client: True)
    monkeypatch.setattr(
        "agent.jobs.fetch_pending_jobs",
        lambda base_url, api_key, client: {"jobs": [], "poll_interval_seconds": 10},
    )

    config = main.Config(
        backend_url="http://backend.example",
        api_key="test-key",
        state_path=tmp_path / "sync_state.json",
        interval_seconds=60,
    )
    result = main.run_cycle(config, client=None)
    assert result == 10


def test_run_cycle_returns_none_when_fetch_pending_jobs_fails(tmp_path, monkeypatch):
    import httpx as httpx_module

    monkeypatch.setattr("agent.claude_code_source.list_claude_code_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.list_cursor_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.enrich_with_messages", lambda sessions: None)
    monkeypatch.setattr("agent.uploader.push_sync", lambda base_url, api_key, sessions, client: True)

    def raise_http_error(base_url, api_key, client):
        raise httpx_module.HTTPError("boom")

    monkeypatch.setattr("agent.jobs.fetch_pending_jobs", raise_http_error)

    config = main.Config(
        backend_url="http://backend.example",
        api_key="test-key",
        state_path=tmp_path / "sync_state.json",
        interval_seconds=60,
    )
    result = main.run_cycle(config, client=None)
    assert result is None


def test_main_falls_back_to_config_interval_when_run_cycle_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_REMOTE_BACKEND_URL", "http://backend.example")
    monkeypatch.setenv("AI_REMOTE_API_KEY", "test-key")
    monkeypatch.setenv("AI_REMOTE_STATE_PATH", str(tmp_path / "sync_state.json"))
    monkeypatch.setenv("AI_REMOTE_INTERVAL_SECONDS", "60")

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise SystemExit  # stop the infinite loop after the first iteration

    monkeypatch.setattr("agent.main.time.sleep", fake_sleep)
    monkeypatch.setattr("agent.main.run_cycle", lambda config, client: None)

    with pytest.raises(SystemExit):
        main.main()

    assert sleep_calls == [60]


def test_main_uses_returned_interval_when_run_cycle_succeeds(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_REMOTE_BACKEND_URL", "http://backend.example")
    monkeypatch.setenv("AI_REMOTE_API_KEY", "test-key")
    monkeypatch.setenv("AI_REMOTE_STATE_PATH", str(tmp_path / "sync_state.json"))
    monkeypatch.setenv("AI_REMOTE_INTERVAL_SECONDS", "60")

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise SystemExit

    monkeypatch.setattr("agent.main.time.sleep", fake_sleep)
    monkeypatch.setattr("agent.main.run_cycle", lambda config, client: 10)

    with pytest.raises(SystemExit):
        main.main()

    assert sleep_calls == [10]
```

- [ ] **Step 4: Run tests to verify they fail (or crash) as expected**

Run: `cd agent && .venv/bin/pytest tests/test_main_cycle.py tests/test_uploader_and_jobs.py -v`
Expected: the four pre-existing tests now FAIL with `TypeError: list indices must be integers or
slices, not str` (proving the interface change is real and these fakes needed updating — already
fixed in Steps 1-2 above, so re-run after those edits); the four new tests FAIL with
`AttributeError`/assertion mismatches before Step 5's implementation.

- [ ] **Step 5: Implement — `agent/agent/jobs.py`**

Replace `fetch_pending_jobs` (currently lines 1-7):

```python
def fetch_pending_jobs(base_url: str, api_key: str, client: httpx.Client) -> list[dict]:
    response = client.get(f"{base_url}/jobs/pending", headers={"Authorization": f"Bearer {api_key}"})
    response.raise_for_status()
    return response.json()["jobs"]
```

with:

```python
def fetch_pending_jobs(base_url: str, api_key: str, client: httpx.Client) -> dict:
    response = client.get(f"{base_url}/jobs/pending", headers={"Authorization": f"Bearer {api_key}"})
    response.raise_for_status()
    return response.json()
```

- [ ] **Step 6: Implement — `agent/agent/main.py`**

Replace the whole file:

```python
import sys
import time

import httpx

from . import claude_code_source, cursor_source, executor, jobs, state, uploader
from .config import Config, load_config


def run_cycle(config: Config, client: httpx.Client) -> int | None:
    claude_sessions = claude_code_source.list_claude_code_sessions()
    cursor_sessions = cursor_source.list_cursor_sessions()

    synced = state.load_synced_ids(config.state_path)

    changed_cursor_sessions = state.compute_deltas(cursor_sessions, synced)
    cursor_source.enrich_with_messages(changed_cursor_sessions)

    all_sessions = claude_sessions + changed_cursor_sessions
    deltas = state.compute_deltas(all_sessions, synced)
    if uploader.push_sync(config.backend_url, config.api_key, deltas, client=client):
        for s in deltas:
            synced[s["id"]] = s["last_updated_at"]
        state.save_synced_ids(synced, config.state_path)

    next_interval = None
    try:
        response = jobs.fetch_pending_jobs(config.backend_url, config.api_key, client)
        pending_jobs = response["jobs"]
        next_interval = response.get("poll_interval_seconds")
    except httpx.HTTPError as exc:
        print(f"fetch_pending_jobs failed: {exc}", file=sys.stderr)
        pending_jobs = []

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
            jobs.report_job_result(
                config.backend_url,
                config.api_key,
                job["id"],
                result["status"],
                client,
                result.get("result_text", ""),
                result.get("messages"),
                is_complete=result.get("is_complete", False),
            )
        except Exception as exc:
            print(f"job {job.get('id')} failed: {exc}", file=sys.stderr)
            continue

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


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd agent && .venv/bin/pytest tests/ -v`
Expected: PASS — full agent suite.

- [ ] **Step 8: Commit**

```bash
git add agent/agent/jobs.py agent/agent/main.py agent/tests/test_main_cycle.py agent/tests/test_uploader_and_jobs.py
git commit -m "feat(agent): obey the backend's reported poll interval instead of a fixed sleep"
```

---

### Task 9: UI — active/default poll-mode indicator in the header

**Files:**
- Modify: `backend/app/main.py` — add a `_poll_mode` helper, use it in `list_chats` (lines 130-160),
  `chat_detail` (lines 169-193, both render calls), `new_session_form` (lines 212-221),
  `jobs_audit_log` (lines 246-255)
- Modify: `backend/app/templates/base.html:19-32` (header)
- Modify: `backend/app/static/style.css` (after the `#pause-toggle` rule, currently ending line 240)
- Test: `backend/tests/test_commands.py`

**Interfaces:**
- Consumes: `db.current_poll_interval_seconds` (Task 6), `settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS`
  (Task 5).
- Produces: every template-rendering route now passes `poll_mode: "active" | "default"` and
  `poll_interval_seconds: int` into its context — no other task depends on these.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_commands.py`:

```python
def test_list_page_shows_default_poll_mode_when_idle(logged_in_client):
    response = logged_in_client.get("/")
    assert "Standard (60s)" in response.text


def test_list_page_shows_active_poll_mode_after_action(logged_in_client):
    _sync_session(logged_in_client)
    logged_in_client.post("/chats/claude-code:abc/command", json={"prompt": "go"})

    response = logged_in_client.get("/")
    assert "Aktiv (10s)" in response.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_commands.py -v -k "poll_mode"`
Expected: FAIL — `assert "Standard (60s)" in response.text` is `False` (badge not yet rendered)

- [ ] **Step 3: Implement — `_poll_mode` helper and the four endpoints**

Add to `backend/app/main.py`, right after `_compute_eta_seconds` (currently ends at line 67, before
the `fetch_full` endpoint):

```python
def _poll_mode(conn) -> tuple[str, int]:
    interval = db.current_poll_interval_seconds(
        conn, settings.AI_REMOTE_INTERVAL_SECONDS, settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS
    )
    mode = "active" if interval == settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS else "default"
    return mode, interval
```

In `list_chats` (currently lines 130-160), add before the `return templates.TemplateResponse(...)`:

```python
    poll_mode, poll_interval_seconds = _poll_mode(conn)
```

and add two keys to the returned context dict:

```python
            "poll_mode": poll_mode,
            "poll_interval_seconds": poll_interval_seconds,
```

In `chat_detail` (currently lines 169-193), add `poll_mode, poll_interval_seconds = _poll_mode(conn)`
right after `session = db.get_session(conn, session_id)`, and add the same two keys to **both**
`TemplateResponse` context dicts (the 404 branch and the normal branch).

In `new_session_form` (currently lines 212-221), add the same helper call and the same two context
keys.

In `jobs_audit_log` (currently lines 246-255), add the same helper call and the same two context
keys.

- [ ] **Step 4: Implement — header badge**

Replace `backend/app/templates/base.html` lines 24-30:

```html
      {% if remote_commands_paused is defined %}
      <form method="post" action="/settings/pause-remote-commands" class="header-pause-toggle">
        <input type="hidden" name="paused" value="{{ 'false' if remote_commands_paused else 'true' }}">
        <button id="pause-toggle" type="submit">{{ 'Fortsetzen' if remote_commands_paused else 'Pausieren' }}</button>
      </form>
      {% endif %}
      <button id="theme-toggle" type="button" aria-label="Hell/Dunkel umschalten">🌙</button>
```

with:

```html
      {% if remote_commands_paused is defined %}
      <form method="post" action="/settings/pause-remote-commands" class="header-pause-toggle">
        <input type="hidden" name="paused" value="{{ 'false' if remote_commands_paused else 'true' }}">
        <button id="pause-toggle" type="submit">{{ 'Fortsetzen' if remote_commands_paused else 'Pausieren' }}</button>
      </form>
      {% endif %}
      {% if poll_mode is defined %}
      <span class="poll-mode-badge poll-mode-{{ poll_mode }}">{{ '⚡ Aktiv' if poll_mode == 'active' else 'Standard' }} ({{ poll_interval_seconds }}s)</span>
      {% endif %}
      <button id="theme-toggle" type="button" aria-label="Hell/Dunkel umschalten">🌙</button>
```

- [ ] **Step 5: Implement — CSS**

Add to `backend/app/static/style.css`, right after the `#pause-toggle { ... }` rule (currently ends
at line 240):

```css
.poll-mode-badge { font-size: 0.75rem; color: var(--text-muted); white-space: nowrap; }
.poll-mode-active { color: var(--accent); font-weight: 600; }
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/ -v`
Expected: PASS — full backend suite.

- [ ] **Step 7: Manually verify in the browser**

Run: `./run.sh`, log in. Header should show "Standard (60s)" while idle. Send a command or click
"Mehr laden" on a chat, then navigate back to the list page — header should now show "⚡ Aktiv (10s)"
for the next 5 minutes.

- [ ] **Step 8: Commit**

```bash
git add backend/app/main.py backend/app/templates/base.html backend/app/static/style.css backend/tests/test_commands.py
git commit -m "feat(frontend): show active/default poll-mode indicator in the header"
```

---

## Self-Review Notes

- **Spec coverage:** All three spec sections have a task (Feature 1 → Task 1; Feature 2 → Tasks 2-4;
  Feature 3 → Tasks 5-9). The spec's "Known limitation" (full-history-loaded sessions that keep
  growing) is intentionally not solved — matches the spec's own Non-Goals.
- **Deviation flagged inline:** the `CHAT_HISTORY_PAGE_SIZE`-reuse detail from the spec turned out to
  be unreachable across the agent/backend process boundary; resolved as a local constant per Global
  Constraints above, called out again at Task 2/3.
- **Type/interface consistency checked:** `db.apply_recent_messages`, `db.bump_active_interval`,
  `db.current_poll_interval_seconds` are named and typed identically everywhere they're declared
  (Tasks 4/6) and consumed (Tasks 4/7/9). `fetch_pending_jobs`'s return-type change from `list[dict]`
  to `dict` is threaded through every caller and every test that mocks it (Task 8) — no stale
  bare-list fakes left behind.
- **Ordering constraint is testable, not just documented:** Task 7's Step 7 explicitly re-runs the
  three pre-existing "first call is 60s" tests as a regression check, and Task 7's own new tests
  assert the *second* call is 10s — proving the before/after `bump_active_interval` ordering both
  ways.
