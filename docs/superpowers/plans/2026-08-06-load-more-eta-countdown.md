# "Mehr laden" ETA Countdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the user a rough countdown ETA (instead of a static "Wird geladen...") while waiting for the background agent to pick up and complete a "Mehr laden" / "Gesamte Historie laden" job.

**Architecture:** The backend computes an `eta_seconds` estimate server-side (using its own clock only, comparing `AI_REMOTE_INTERVAL_SECONDS` against how long ago the agent last checked in) and returns it from `POST /chats/{id}/fetch-full`. The frontend runs a purely cosmetic 1-second countdown timer off that number, fully independent of the existing 3-second functional status poll.

**Tech Stack:** FastAPI + Jinja2 + vanilla JS (no build step, no new dependencies).

## Global Constraints

- Setting name: `AI_REMOTE_INTERVAL_SECONDS`, default `60`, parsed as `int(os.environ.get("AI_REMOTE_INTERVAL_SECONDS") or "60")` — the `or` (not a second positional arg to `.get`) is required so an empty-but-present `.env` line falls back to the default instead of crashing (see `CHAT_HISTORY_PAGE_SIZE` for the existing precedent).
- ETA formula: `eta_seconds = clamp(round(interval_seconds - elapsed_since_last_contact), 0, interval_seconds)`; if the agent has never contacted the backend (`last_contact is None`), `eta_seconds = interval_seconds`.
- All ETA arithmetic happens on the server's own clock (`datetime.now(timezone.utc)` compared against a timestamp the same server previously wrote) — never send an absolute timestamp to the browser for it to compare against its own clock.
- Countdown display text (verbatim, German, matches existing tone in `app.js`):
  - Ticking: `` `Wird geladen... (ca. ${remaining}s)` ``
  - At zero: `"Wird geladen — sollte jeden Moment fertig sein..."`
- The existing 3-second `poll(jobId)` loop in `app.js` is functionally unchanged — it still owns detecting `done`/`failed` and reloading the page. The countdown is purely cosmetic and must never block or replace it.
- No template/HTML changes — `eta_seconds` travels only through the `fetch-full` JSON response, not through Jinja context.
- No new JS test framework — this repo's `app.js` has no automated tests today; Task 5 is verified manually per the approved spec.

---

## File Structure

- Modify: `backend/app/settings.py` — add `AI_REMOTE_INTERVAL_SECONDS`.
- Modify: `backend/app/main.py` — add `_compute_eta_seconds` helper; wire it into the `fetch_full` endpoint.
- Modify: `backend/tests/test_settings.py` — default/override/empty-string tests for the new setting.
- Modify: `backend/tests/test_detail_and_jobs.py` — unit tests for `_compute_eta_seconds`; endpoint tests asserting `eta_seconds` in the `fetch-full` response.
- Modify: `docker-compose.yml` — pass `AI_REMOTE_INTERVAL_SECONDS` through to the backend container.
- Modify: `deploy-production-scp.sh` — pass `AI_REMOTE_INTERVAL_SECONDS` through in the production compose heredoc.
- Modify: `example.env` — document that `AI_REMOTE_INTERVAL_SECONDS` now has two consumers.
- Modify: `backend/app/static/app.js` — countdown timer wired into `startFetch`/`poll`.

---

### Task 1: `AI_REMOTE_INTERVAL_SECONDS` setting

**Files:**
- Modify: `backend/app/settings.py`
- Test: `backend/tests/test_settings.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `settings.AI_REMOTE_INTERVAL_SECONDS: int` (default `60`) — consumed by Task 3.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_settings.py`:

```python
def test_ai_remote_interval_seconds_default():
    full_env = {**os.environ, "API_KEY": "a" * 20, "SECRET_KEY": "b" * 20}
    full_env.pop("AI_REMOTE_INTERVAL_SECONDS", None)
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.AI_REMOTE_INTERVAL_SECONDS)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "60"


def test_ai_remote_interval_seconds_override():
    full_env = {**os.environ, "API_KEY": "a" * 20, "SECRET_KEY": "b" * 20, "AI_REMOTE_INTERVAL_SECONDS": "30"}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.AI_REMOTE_INTERVAL_SECONDS)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "30"


def test_ai_remote_interval_seconds_empty_string_falls_back_to_default():
    # example.env ships AI_REMOTE_INTERVAL_SECONDS=60 today, but a user could blank it
    # out the same way LOCAL_HOME_DIR/CHAT_HISTORY_PAGE_SIZE have been in the past —
    # os.environ.get(name, default) only falls back when unset, not when empty.
    full_env = {**os.environ, "API_KEY": "a" * 20, "SECRET_KEY": "b" * 20, "AI_REMOTE_INTERVAL_SECONDS": ""}
    result = subprocess.run(
        [sys.executable, "-c", "from app import settings; print(settings.AI_REMOTE_INTERVAL_SECONDS)"],
        cwd=str(BACKEND_DIR),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "60"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_settings.py -k ai_remote_interval -v`
Expected: FAIL — `AttributeError: module 'app.settings' has no attribute 'AI_REMOTE_INTERVAL_SECONDS'`

- [ ] **Step 3: Implement the setting**

In `backend/app/settings.py`, after the existing `CHAT_HISTORY_PAGE_SIZE` line:

```python
AI_REMOTE_INTERVAL_SECONDS = int(os.environ.get("AI_REMOTE_INTERVAL_SECONDS") or "60")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_settings.py -k ai_remote_interval -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/settings.py backend/tests/test_settings.py
git commit -m "feat(backend): add AI_REMOTE_INTERVAL_SECONDS setting"
```

---

### Task 2: `_compute_eta_seconds` helper

**Files:**
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_detail_and_jobs.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_compute_eta_seconds(last_contact: str | None, interval_seconds: int) -> int` in `backend/app/main.py` — consumed by Task 3.

- [ ] **Step 1: Write the failing tests**

Add near the top of `backend/tests/test_detail_and_jobs.py` (after the existing imports):

```python
def test_compute_eta_seconds_no_prior_contact_returns_full_interval(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key-1234")
    monkeypatch.setenv("SECRET_KEY", "test-secret-value1")
    from app.main import _compute_eta_seconds

    assert _compute_eta_seconds(None, 60) == 60


def test_compute_eta_seconds_recent_contact_returns_remaining_time(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key-1234")
    monkeypatch.setenv("SECRET_KEY", "test-secret-value1")
    from datetime import datetime, timedelta, timezone
    from app.main import _compute_eta_seconds

    last_contact = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    eta = _compute_eta_seconds(last_contact, 60)
    assert 48 <= eta <= 50  # ~50s of a 60s interval remain; allow a little test jitter


def test_compute_eta_seconds_overdue_contact_clamps_to_zero(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key-1234")
    monkeypatch.setenv("SECRET_KEY", "test-secret-value1")
    from datetime import datetime, timedelta, timezone
    from app.main import _compute_eta_seconds

    last_contact = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    assert _compute_eta_seconds(last_contact, 60) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_detail_and_jobs.py -k compute_eta_seconds -v`
Expected: FAIL — `ImportError: cannot import name '_compute_eta_seconds' from 'app.main'`

- [ ] **Step 3: Implement the helper**

In `backend/app/main.py`, add the import at the top (alongside the existing `import` block):

```python
from datetime import datetime, timezone
```

Then add the helper function above the `fetch_full` endpoint (after the `jobs_complete` function, before `@app.post("/chats/{session_id}/fetch-full"...)`):

```python
def _compute_eta_seconds(last_contact: str | None, interval_seconds: int) -> int:
    """Rough ETA until the agent's next check-in, clamped to [0, interval_seconds].

    Computed entirely from this process's own clock — `last_contact` is a
    timestamp this same backend wrote in `db.record_agent_contact`, so there's
    no client-clock skew to worry about.
    """
    if last_contact is None:
        return interval_seconds
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_contact)).total_seconds()
    return max(0, min(interval_seconds, round(interval_seconds - elapsed)))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_detail_and_jobs.py -k compute_eta_seconds -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/main.py backend/tests/test_detail_and_jobs.py
git commit -m "feat(backend): add _compute_eta_seconds helper"
```

---

### Task 3: Wire `eta_seconds` into the `fetch-full` endpoint

**Files:**
- Modify: `backend/app/main.py:56-65` (the `fetch_full` function)
- Test: `backend/tests/test_detail_and_jobs.py`

**Interfaces:**
- Consumes: `_compute_eta_seconds` (Task 2), `settings.AI_REMOTE_INTERVAL_SECONDS` (Task 1), existing `db.get_last_agent_contact(conn) -> str | None`.
- Produces: `POST /chats/{session_id}/fetch-full` JSON response now has an `eta_seconds: int` key alongside the existing `job_id` — consumed by Task 5's frontend code.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_detail_and_jobs.py`:

```python
def test_fetch_full_response_includes_eta_seconds_when_agent_never_contacted(logged_in_client):
    _sync_one(logged_in_client)

    enqueue = logged_in_client.post("/chats/claude-code:abc/fetch-full")
    assert enqueue.json()["eta_seconds"] == 60  # default AI_REMOTE_INTERVAL_SECONDS, no prior contact


def test_fetch_full_response_eta_seconds_reflects_recent_agent_contact(logged_in_client):
    import os
    from datetime import datetime, timedelta, timezone

    _sync_one(logged_in_client)
    db_path = os.environ["DATABASE_PATH"]
    conn = db.get_connection(db_path)
    recent = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    conn.execute("UPDATE agent_status SET last_contact_at = ? WHERE id = 1", (recent,))
    conn.commit()
    conn.close()

    enqueue = logged_in_client.post("/chats/claude-code:abc/fetch-full")
    eta = enqueue.json()["eta_seconds"]
    assert 45 <= eta <= 50  # ~50s remaining out of the 60s default interval, allow test jitter


def test_fetch_full_response_eta_seconds_clamps_to_zero_when_agent_overdue(logged_in_client):
    import os
    from datetime import datetime, timedelta, timezone

    _sync_one(logged_in_client)
    db_path = os.environ["DATABASE_PATH"]
    conn = db.get_connection(db_path)
    overdue = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    conn.execute("UPDATE agent_status SET last_contact_at = ? WHERE id = 1", (overdue,))
    conn.commit()
    conn.close()

    enqueue = logged_in_client.post("/chats/claude-code:abc/fetch-full")
    assert enqueue.json()["eta_seconds"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_detail_and_jobs.py -k eta_seconds -v`
Expected: FAIL — `KeyError: 'eta_seconds'` on the three new endpoint tests (the `_compute_eta_seconds` unit tests from Task 2 still pass).

- [ ] **Step 3: Wire the helper into the endpoint**

Replace the `fetch_full` function in `backend/app/main.py`:

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

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_detail_and_jobs.py -v`
Expected: PASS (all tests in the file, including the pre-existing ones — confirms no regression)

- [ ] **Step 5: Commit**

```bash
git add backend/app/main.py backend/tests/test_detail_and_jobs.py
git commit -m "feat(backend): return eta_seconds from POST /chats/{id}/fetch-full"
```

---

### Task 4: Deploy plumbing and `.env` documentation

**Files:**
- Modify: `docker-compose.yml`
- Modify: `deploy-production-scp.sh`
- Modify: `example.env`

**Interfaces:**
- Consumes: `AI_REMOTE_INTERVAL_SECONDS` env var (already read by `settings.py` from Task 1).
- Produces: the env var now reaches the backend container in both the local (`docker-compose.yml`, used by `run.sh`) and production (`deploy-production-scp.sh`) deploy paths.

- [ ] **Step 1: Add the pass-through to `docker-compose.yml`**

In `docker-compose.yml`, add a line to the `backend` service's `environment` block, right after `CHAT_HISTORY_PAGE_SIZE`:

```yaml
    environment:
      - API_KEY=${API_KEY}
      - SECRET_KEY=${SECRET_KEY}
      - DATABASE_PATH=/data/app.db
      - SESSION_COOKIE_HTTPS_ONLY=${SESSION_COOKIE_HTTPS_ONLY:-true}
      - LOCAL_HOME_DIR=${LOCAL_HOME_DIR:-/Users/yourname}
      - CHAT_HISTORY_PAGE_SIZE=${CHAT_HISTORY_PAGE_SIZE:-10}
      - AI_REMOTE_INTERVAL_SECONDS=${AI_REMOTE_INTERVAL_SECONDS:-60}
```

- [ ] **Step 2: Add the pass-through to `deploy-production-scp.sh`**

In `deploy-production-scp.sh`, add the matching line to the heredoc's `environment` block, right after `CHAT_HISTORY_PAGE_SIZE`:

```
    environment:
      - API_KEY=\${API_KEY}
      - SECRET_KEY=\${SECRET_KEY}
      - DATABASE_PATH=/data/app.db
      - LOCAL_HOME_DIR=\${LOCAL_HOME_DIR:-/Users/yourname}
      - CHAT_HISTORY_PAGE_SIZE=\${CHAT_HISTORY_PAGE_SIZE:-10}
      - AI_REMOTE_INTERVAL_SECONDS=\${AI_REMOTE_INTERVAL_SECONDS:-60}
```

(Note the `\$` escaping — this block is inside a `cat > ... <<COMPOSE` heredoc, same as the existing lines.)

- [ ] **Step 3: Document the variable in `example.env`**

In `example.env`, replace the bare line:

```
AI_REMOTE_INTERVAL_SECONDS=60
```

with a commented version, matching the style of the other documented variables in that file:

```
# How often the local background agent checks the backend for pending jobs
# (sync, "Mehr laden" / "Gesamte Historie laden"), in seconds. Read by two
# places: the agent's own poll loop (agent/agent/config.py), and the backend
# (used to estimate the "Wird geladen... (ca. Xs)" countdown shown while a
# fetch job is in flight). Keep both in sync — they share this one value.
AI_REMOTE_INTERVAL_SECONDS=60
```

- [ ] **Step 4: Verify the compose files are still valid**

Run: `docker compose config --quiet`
Expected: exits with code 0 and no output (confirms `docker-compose.yml` still parses and interpolates correctly)

Run: `grep -c "AI_REMOTE_INTERVAL_SECONDS" docker-compose.yml deploy-production-scp.sh example.env`
Expected: `docker-compose.yml:1`, `deploy-production-scp.sh:1`, `example.env:1`

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml deploy-production-scp.sh example.env
git commit -m "chore(deploy): pass AI_REMOTE_INTERVAL_SECONDS through to the backend container"
```

---

### Task 5: Frontend countdown

**Files:**
- Modify: `backend/app/static/app.js:1-56`

**Interfaces:**
- Consumes: `eta_seconds` field from the `fetch-full` response (Task 3).
- Produces: countdown UI behavior — no new interface for other tasks to consume.

- [ ] **Step 1: Implement the countdown**

Replace the first `document.addEventListener("DOMContentLoaded", ...)` block in `backend/app/static/app.js` (lines 1-56) with:

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

  let countdownId = null;

  const stopCountdown = () => {
    if (countdownId) {
      clearInterval(countdownId);
      countdownId = null;
    }
  };

  const startCountdown = (remaining) => {
    stopCountdown();
    const tick = () => {
      if (remaining <= 0) {
        status.textContent = "Wird geladen — sollte jeden Moment fertig sein...";
        stopCountdown();
        return;
      }
      status.textContent = `Wird geladen... (ca. ${remaining}s)`;
      remaining -= 1;
    };
    tick();
    countdownId = setInterval(tick, 1000);
  };

  const poll = async (jobId) => {
    try {
      const statusRes = await fetch(`/chats/${sessionId}/status?job_id=${jobId}`);
      if (!statusRes.ok) {
        throw new Error(`HTTP ${statusRes.status}`);
      }
      const data = await statusRes.json();
      if (data.status === "done" || data.status === "failed") {
        stopCountdown();
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
      stopCountdown();
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
      const { job_id, eta_seconds } = await res.json();
      startCountdown(eta_seconds);
      poll(job_id);
    } catch (error) {
      stopCountdown();
      status.textContent = "Fehler beim Starten — bitte erneut versuchen.";
      setButtonsDisabled(false);
    }
  };

  if (loadMoreButton) loadMoreButton.addEventListener("click", () => startFetch(false));
  if (loadAllButton) loadAllButton.addEventListener("click", () => startFetch(true));
});
```

(The two other `DOMContentLoaded` blocks further down in `app.js` — project-path autocomplete and the filter toolbar — are untouched.)

- [ ] **Step 2: Manually verify in the browser**

Run: `./run.sh` (starts the backend at `http://localhost:8000` and runs one local-agent sync cycle)

1. Open `http://localhost:8000`, log in, open any chat that still shows "Mehr laden" (one whose `full_content_synced` is false — if none exist, pick any synced session and re-run `./run.sh` after editing a message count, or just proceed with whichever session has the buttons visible).
2. Open the browser's DevTools console, run `AI_REMOTE_INTERVAL_SECONDS` isn't exposed client-side by design — instead confirm the ETA came from the server: `fetch(location.pathname + '/fetch-full', {method: 'POST'}).then(r => r.json()).then(console.log)` and note the `eta_seconds` value returned (should be `60` on a fresh local DB, since the local agent hasn't been recorded as contacting yet unless `run.sh` already ran a cycle — if it has, `eta_seconds` should be close to `60` minus however many seconds have passed since that cycle).
3. Click "Mehr laden". Confirm the status text reads `Wird geladen... (ca. Ns)` and `N` visibly decrements once per second.
4. Let it run down to 0. Confirm the text switches to `Wird geladen — sollte jeden Moment fertig sein...` and does not go negative or keep animating.
5. Wait for the local agent's next cycle (or manually trigger one per `run.sh`'s printed "Re-sync now" instructions) to complete the job. Confirm the page reloads once the job finishes, exactly as it did before this change.
6. Reload the chat page, click "Mehr laden" again, then immediately kill the backend (`docker compose stop backend`) to force a connection error mid-poll. Confirm the countdown stops immediately (no lingering timer) and the text switches to `Verbindung verloren — bitte Seite neu laden oder erneut versuchen.`. Restart the backend afterward (`docker compose start backend`).

Expected: every step above matches its stated outcome; no JavaScript console errors during any step.

- [ ] **Step 3: Commit**

```bash
git add backend/app/static/app.js
git commit -m "feat(frontend): show a countdown ETA while a fetch job is in flight"
```

---

## Self-Review

**Spec coverage:**
- New `AI_REMOTE_INTERVAL_SECONDS` setting with `or "60"` empty-string handling → Task 1.
- `_compute_eta_seconds` helper, clamped `[0, interval]`, `None` → full interval → Task 2.
- `eta_seconds` returned from `fetch-full` → Task 3.
- Deploy plumbing in both `docker-compose.yml` and `deploy-production-scp.sh`, `example.env` comment → Task 4.
- Countdown timer independent of the 3s poll, indeterminate message at zero, cleanup on all exit paths, no reload special-casing → Task 5.
- Testing section (settings tests, helper unit tests, endpoint test, manual JS verification, no new JS framework) → covered across Tasks 1, 2, 3, 5.
- Non-Goals (no change to agent polling/execution, no completion-time precision, no idle-state hint) → nothing in this plan touches those areas.

**Placeholder scan:** No TBD/TODO; every step has literal code or literal shell commands.

**Type consistency:** `_compute_eta_seconds(last_contact: str | None, interval_seconds: int) -> int` is defined once in Task 2 and used identically (same name, same argument order) in Task 3. `eta_seconds` is the field name used consistently in Task 3's response and Task 5's destructuring (`const { job_id, eta_seconds } = ...`).
