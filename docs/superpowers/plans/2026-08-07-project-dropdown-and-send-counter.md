# Projekt-Dropdown + Sende-Counter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auf `/projects/new` wählt der User zuerst ein Projekt aus einem Dropdown (zuletzt
verwendetes vorselektiert) statt N Formulare untereinander zu sehen; und "Wird gesendet..."
bekommt denselben ETA-Countdown wie "Wird geladen..." — sowohl auf `/projects/new` als auch im
Resume-Command-Formular der Detailseite.

**Architecture:** Backend liefert `eta_seconds` an zwei bestehenden POST-Endpunkten
(`/projects/command`, `/chats/{session_id}/command`) über die bereits vorhandene
`_compute_eta_seconds`-Funktion. `new_session.html` wird von N Formularen auf ein Single-Form mit
`<select>` umgebaut. `app.js` bekommt eine extrahierte `makeCountdown(statusNode, verb)`-Fabrik,
die von drei Stellen genutzt wird; `localStorage` speichert das zuletzt erfolgreich verwendete
Projekt (gleiches Pattern wie die bestehende Theme-Persistenz).

**Tech Stack:** FastAPI + Jinja2 (Server-Rendering), Vanilla JS (`app.js`, kein Framework/Bundler),
pytest + `fastapi.testclient.TestClient`.

## Global Constraints

- Sprache aller UI-Texte: Deutsch (bestehende Konvention in `new_session.html`/`detail.html`).
- Kein neues DB-Feld, kein Server-State für "zuletzt verwendetes Projekt" — ausschließlich
  `localStorage`, analog zu `localStorage.setItem("theme", ...)` in `app.js:323`.
- `_compute_eta_seconds` bleibt unverändert (Signatur `(last_contact: str | None, interval_seconds:
  int) -> int`), nur zusätzlich in zwei weiteren Endpunkten aufgerufen.
- Kein JS-Testframework — Frontend-Verhalten wird manuell via `./run.sh` verifiziert, nicht mit
  neuer Tooling-Infrastruktur.
- Bestehende Tests dürfen nicht brechen: `test_new_session_command_creates_job`,
  `test_new_session_form_lists_allowed_projects`, `test_command_creates_job_for_allowlisted_session`
  bleiben grün.

---

### Task 1: `eta_seconds` in `POST /projects/command`

**Files:**
- Modify: `backend/app/main.py:223-235`
- Test: `backend/tests/test_commands.py`

**Interfaces:**
- Consumes: `_compute_eta_seconds(last_contact: str | None, interval_seconds: int) -> int`
  (`backend/app/main.py:57`, unverändert), `db.get_last_agent_contact(conn)` (bereits importiert
  über `db`).
- Produces: `POST /projects/command` liefert jetzt `{"job_id": str, "eta_seconds": int}` statt nur
  `{"job_id": str}`.

- [ ] **Step 1: Write the failing test**

In `backend/tests/test_commands.py` ergänzen (nach `test_new_session_command_creates_job`):

```python
def test_new_session_command_response_includes_eta_seconds(logged_in_client):
    response = logged_in_client.post(
        "/projects/command",
        json={"project_path": "/Users/jan/source/demo", "tool": "claude-code", "prompt": "hi"},
    )
    assert response.status_code == 200
    assert response.json()["eta_seconds"] == 60  # default AI_REMOTE_INTERVAL_SECONDS, no prior contact


def test_new_session_command_eta_seconds_reflects_recent_agent_contact(logged_in_client):
    import os
    from datetime import datetime, timedelta, timezone
    from app import db

    db_path = os.environ["DATABASE_PATH"]
    conn = db.get_connection(db_path)
    recent = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    conn.execute("UPDATE agent_status SET last_contact_at = ? WHERE id = 1", (recent,))
    conn.commit()
    conn.close()

    response = logged_in_client.post(
        "/projects/command",
        json={"project_path": "/Users/jan/source/demo", "tool": "claude-code", "prompt": "hi"},
    )
    eta = response.json()["eta_seconds"]
    assert 45 <= eta <= 50  # ~50s remaining out of the 60s default interval, allow test jitter
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_commands.py -k eta_seconds -v`
Expected: FAIL with `KeyError: 'eta_seconds'`

- [ ] **Step 3: Implement**

In `backend/app/main.py`, Zeile 223-235 ersetzen:

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

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_commands.py -v`
Expected: alle PASS (inkl. der beiden neuen und der drei bestehenden `test_new_session_command_*`).

- [ ] **Step 5: Commit**

```bash
git add backend/app/main.py backend/tests/test_commands.py
git commit -m "feat(backend): return eta_seconds from POST /projects/command

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01AEtvEMAUNPMprfKjH61X7z"
```

---

### Task 2: `eta_seconds` in `POST /chats/{session_id}/command`

**Files:**
- Modify: `backend/app/main.py:196-208`
- Test: `backend/tests/test_commands.py`

**Interfaces:**
- Consumes: dieselbe `_compute_eta_seconds`/`db.get_last_agent_contact` wie Task 1.
- Produces: `POST /chats/{session_id}/command` liefert jetzt `{"job_id": str, "eta_seconds": int}`.

- [ ] **Step 1: Write the failing test**

In `backend/tests/test_commands.py` ergänzen (nach `test_command_creates_job_for_allowlisted_session`):

```python
def test_command_response_includes_eta_seconds(logged_in_client):
    _sync_session(logged_in_client)
    response = logged_in_client.post("/chats/claude-code:abc/command", json={"prompt": "keep going"})
    assert response.status_code == 200
    assert response.json()["eta_seconds"] == 60  # default AI_REMOTE_INTERVAL_SECONDS, no prior contact
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/test_commands.py::test_command_response_includes_eta_seconds -v`
Expected: FAIL with `KeyError: 'eta_seconds'`

- [ ] **Step 3: Implement**

In `backend/app/main.py`, Zeile 196-208 (Rückgabe der Funktion) anpassen — die aktuelle Funktion
endet mit:

```python
    job_id = db.create_job(
        conn, "resume_message", session_id, payload=json.dumps({"prompt": body.prompt})
    )
    return {"job_id": job_id}
```

wird zu:

```python
    job_id = db.create_job(
        conn, "resume_message", session_id, payload=json.dumps({"prompt": body.prompt})
    )
    eta_seconds = _compute_eta_seconds(db.get_last_agent_contact(conn), settings.AI_REMOTE_INTERVAL_SECONDS)
    return {"job_id": job_id, "eta_seconds": eta_seconds}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_commands.py -v`
Expected: alle PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/main.py backend/tests/test_commands.py
git commit -m "feat(backend): return eta_seconds from POST /chats/{session_id}/command

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01AEtvEMAUNPMprfKjH61X7z"
```

---

### Task 3: Single-Form + Projekt-Dropdown auf `/projects/new`

**Files:**
- Modify: `backend/app/templates/new_session.html`
- Modify: `backend/app/static/style.css:263-288`
- Test: `backend/tests/test_commands.py`

**Interfaces:**
- Consumes: `allowed_projects` (Liste von Strings, unverändert von `main.py:217` geliefert).
- Produces: DOM-Struktur, auf die Task 5 (JS) sich stützt: `#new-session-form` (Form-Element,
  besitzt kein `data-project-path` mehr), `#project-select` (`<select name="project_path">`),
  `select[name=tool]`, `textarea[name=prompt]`, `#command-status` (`<p>` für Status-Text).

- [ ] **Step 1: Write the failing test**

In `backend/tests/test_commands.py` ergänzen (nach `test_new_session_form_lists_allowed_projects`):

```python
def test_new_session_form_renders_single_dropdown_form(logged_in_client):
    response = logged_in_client.get("/projects/new")
    assert response.status_code == 200
    assert response.text.count('id="new-session-form"') == 1
    assert '<select id="project-select" name="project_path">' in response.text
    assert '<option value="/Users/jan/source/demo">' in response.text
    # no more one <form> per project, no more per-project data-project-path forms
    assert "data-project-path" not in response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/test_commands.py::test_new_session_form_renders_single_dropdown_form -v`
Expected: FAIL (aktuelles Template hat kein `#new-session-form`/`#project-select`).

- [ ] **Step 3: Implement**

`backend/app/templates/new_session.html` komplett ersetzen durch:

```html
{% extends "base.html" %}
{% block content %}
<h1>Neue Session starten</h1>
{% if not allowed_projects %}
<p>Keine Projekte in der Allow-List konfiguriert.</p>
{% else %}
<form id="new-session-form">
  <label for="project-select">Projekt</label>
  <select id="project-select" name="project_path">
    {% for path in allowed_projects %}
    <option value="{{ path }}">{{ path }}</option>
    {% endfor %}
  </select>
  <select name="tool">
    <option value="claude-code">Claude Code</option>
    <option value="cursor">Cursor</option>
  </select>
  <textarea name="prompt" placeholder="Prompt..." required></textarea>
  <button type="submit">Senden</button>
  <p id="command-status" class="command-status"></p>
</form>
{% endif %}
{% endblock %}
```

`backend/app/static/style.css:263-288` — `.project-command-list`-Regeln (Zeilen 281-288) entfernen,
da die Liste entfällt; `#command-form, .new-session-form`-Block (Zeilen 263-270) um den Selektor
`#new-session-form` ergänzen, damit das neue Formular dasselbe Flex/Spacing-Styling bekommt:

```css
#command-form, .new-session-form, #new-session-form {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  margin-top: 1.25rem;
  padding-top: 1rem;
  border-top: 1px solid var(--border);
}
#command-form textarea, .new-session-form textarea, #new-session-form textarea {
  min-height: 80px;
  padding: 0.6rem 0.75rem;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--text);
  font-family: inherit;
}
```

(Die Zeilen 281-288 mit `.project-command-list`/`.project-command-list li` werden komplett
gelöscht — keine Liste mehr im neuen Template.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_commands.py -v`
Expected: alle PASS, inkl. `test_new_session_form_lists_allowed_projects` (matcht weiterhin den
Pfad-Text, jetzt im `<option>`) und der neue Test.

- [ ] **Step 5: Commit**

```bash
git add backend/app/templates/new_session.html backend/app/static/style.css backend/tests/test_commands.py
git commit -m "feat(frontend): replace per-project forms with single dropdown form on /projects/new

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01AEtvEMAUNPMprfKjH61X7z"
```

---

### Task 4: `makeCountdown`-Fabrik extrahieren + im Load-More-Flow verwenden

**Files:**
- Modify: `backend/app/static/app.js:1-84`

**Interfaces:**
- Produces: Modul-Top-Level-Funktion `makeCountdown(statusNode, verb)` →
  `{ start(remainingSeconds: number): void, stop(): void }`. Text-Format:
  `` `Wird ${verb}... (ca. ${remaining}s)` `` während des Countdowns,
  `` `Wird ${verb} — sollte jeden Moment fertig sein...` `` bei `remaining <= 0`.
  Wird von Task 6 und Task 7 konsumiert.

Kein neuer Test nötig — dieser Task ist eine reine Verhaltens-erhaltende Extraktion des
bestehenden, bereits manuell verifizierten Load-More-Countdowns (kein JS-Testframework im Repo,
siehe Global Constraints). Verifikation erfolgt manuell in Step 3.

- [ ] **Step 1: Extrahiere die Fabrik**

Ganz oben in `backend/app/static/app.js`, vor dem ersten `document.addEventListener(...)`
(also vor der aktuellen Zeile 1), einfügen:

```js
function makeCountdown(statusNode, verb) {
  let countdownId = null;
  const stop = () => {
    if (countdownId) {
      clearInterval(countdownId);
      countdownId = null;
    }
  };
  const start = (remaining) => {
    stop();
    const tick = () => {
      if (remaining <= 0) {
        statusNode.textContent = `Wird ${verb} — sollte jeden Moment fertig sein...`;
        stop();
        return;
      }
      statusNode.textContent = `Wird ${verb}... (ca. ${remaining}s)`;
      remaining -= 1;
    };
    tick();
    countdownId = setInterval(tick, 1000);
  };
  return { start, stop };
}
```

- [ ] **Step 2: Ersetze den inline Countdown im Load-More-Block**

In `backend/app/static/app.js`, der bestehende Block (aktuell Zeile 14-36):

```js
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
```

wird ersetzt durch:

```js
  const { start: startCountdown, stop: stopCountdown } = makeCountdown(status, "geladen");
```

Alle übrigen Aufrufe von `startCountdown(...)` und `stopCountdown()` im selben
`DOMContentLoaded`-Block (`poll`, `startFetch`) bleiben unverändert — sie rufen jetzt die aus
`makeCountdown` zurückgegebenen Funktionen auf.

- [ ] **Step 3: Manuelle Verifikation**

Run: `./run.sh` (Backend starten), im Browser eine Session mit unvollständiger Historie öffnen,
"Mehr laden" klicken. Erwartet: identisches Verhalten wie vorher — "Wird geladen..." zählt
sekundenweise runter, wechselt bei 0 auf den "sollte jeden Moment fertig sein"-Text, kein
JS-Fehler in der Konsole (`mcp__claude-in-chrome__read_console_messages` oder Browser-DevTools).

- [ ] **Step 4: Commit**

```bash
git add backend/app/static/app.js
git commit -m "refactor(frontend): extract makeCountdown factory from load-more flow

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01AEtvEMAUNPMprfKjH61X7z"
```

---

### Task 5: Vorselektion des zuletzt verwendeten Projekts

**Files:**
- Modify: `backend/app/static/app.js` (neuer `DOMContentLoaded`-Block)

**Interfaces:**
- Consumes: DOM-Element `#project-select` aus Task 3.
- Produces: `localStorage`-Key `"lastProject"` (String) — wird in Task 6 geschrieben, hier nur
  gelesen.

Kein Backend-Test — reines Browser-`localStorage`-Verhalten, manuell verifiziert (Global
Constraints: kein JS-Testframework).

- [ ] **Step 1: Implementiere die Vorselektion**

In `backend/app/static/app.js`, direkt vor dem `.new-session-form`-Block (vor der bisherigen
Zeile 231, die jetzt durch Task 6 ersetzt wird) einen neuen Block einfügen:

```js
document.addEventListener("DOMContentLoaded", () => {
  const select = document.getElementById("project-select");
  if (!select) return;
  const last = localStorage.getItem("lastProject");
  if (last && [...select.options].some((option) => option.value === last)) {
    select.value = last;
  }
});
```

- [ ] **Step 2: Manuelle Verifikation**

Run: `./run.sh`, in der Browser-Konsole auf `/projects/new` ausführen:
`localStorage.setItem("lastProject", "/Users/jan/source/demo")`, Seite neu laden. Erwartet:
`#project-select` zeigt `/Users/jan/source/demo` als ausgewählte Option (sofern in
`AI_REMOTE_ALLOWED_PROJECTS` enthalten). Danach `localStorage.setItem("lastProject",
"/nicht/vorhanden")` setzen, neu laden — erwartet: Fallback auf die erste Option, kein JS-Fehler.

- [ ] **Step 3: Commit**

```bash
git add backend/app/static/app.js
git commit -m "feat(frontend): preselect last-used project in new-session dropdown

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01AEtvEMAUNPMprfKjH61X7z"
```

---

### Task 6: Single-Form-Submit mit Countdown + Persistenz auf `/projects/new`

**Files:**
- Modify: `backend/app/static/app.js` (ersetzt den bisherigen `.new-session-form`-Block,
  aktuell Zeile 231-290)

**Interfaces:**
- Consumes: `makeCountdown` (Task 4), `#project-select`/`#new-session-form`/`#command-status`
  (Task 3), Response-Feld `eta_seconds` von `POST /projects/command` (Task 1).
- Produces: `localStorage.setItem("lastProject", projectPath)` beim erfolgreichen Jobabschluss —
  Vertrag, den Task 5 beim Lesen voraussetzt.

- [ ] **Step 1: Ersetze den bestehenden Block**

Der aktuelle `document.addEventListener("DOMContentLoaded", () => { document.querySelectorAll(".new-session-form")...` Block (Zeile 231-290) wird komplett ersetzt durch:

```js
document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("new-session-form");
  if (!form) return;
  const status = document.getElementById("command-status");
  const button = form.querySelector("button[type=submit]");
  const countdown = makeCountdown(status, "gesendet");

  const poll = (jobId, projectPath) => {
    fetch(`/jobs/${jobId}/status`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data) => {
        if (data.status === "done") {
          countdown.stop();
          localStorage.setItem("lastProject", projectPath);
          status.textContent = "Fertig — die neue Session erscheint in der Liste in Kürze.";
        } else if (data.status === "failed") {
          countdown.stop();
          status.textContent = `Fehlgeschlagen: ${data.result_text || "unbekannter Fehler"}`;
          button.disabled = false;
        } else {
          setTimeout(() => poll(jobId, projectPath), 3000);
        }
      })
      .catch(() => {
        countdown.stop();
        status.textContent = "Verbindung verloren — bitte erneut versuchen.";
        button.disabled = false;
      });
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    button.disabled = true;
    status.textContent = "Wird gesendet...";
    const projectPath = form.project_path.value;
    const prompt = form.prompt.value;
    const tool = form.tool.value;
    fetch("/projects/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_path: projectPath, tool, prompt }),
    })
      .then((res) =>
        res
          .json()
          .catch(() => ({}))
          .then((data) => ({ ok: res.ok, data }))
      )
      .then(({ ok, data }) => {
        if (!ok) {
          status.textContent = data && data.detail ? data.detail : "Fehler beim Senden — bitte erneut versuchen.";
          button.disabled = false;
          return;
        }
        countdown.start(data.eta_seconds);
        poll(data.job_id, projectPath);
      })
      .catch(() => {
        countdown.stop();
        status.textContent = "Fehler beim Senden — bitte erneut versuchen.";
        button.disabled = false;
      });
  });
});
```

- [ ] **Step 2: Manuelle Verifikation**

Run: `./run.sh`, auf `/projects/new` ein Projekt wählen, Tool + Prompt ausfüllen, "Senden"
klicken. Erwartet: Status-Text zeigt sofort "Wird gesendet...", danach den Countdown
(`Wird gesendet... (ca. Xs)`), zählt sekundenweise runter. Nach Job-Abschluss (Agent-Poll
abwarten oder Job manuell über `/jobs/pending` + `/jobs/{id}/complete` mit Test-API-Key
abschließen): Erfolgstext erscheint, `localStorage.getItem("lastProject")` in der Browser-Konsole
zeigt den gesendeten Projektpfad.

- [ ] **Step 3: Commit**

```bash
git add backend/app/static/app.js
git commit -m "feat(frontend): send-counter + last-project persistence on /projects/new submit

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01AEtvEMAUNPMprfKjH61X7z"
```

---

### Task 7: Sende-Counter im Resume-Command-Formular der Detailseite

**Files:**
- Modify: `backend/app/static/app.js:171-229`

**Interfaces:**
- Consumes: `makeCountdown` (Task 4), Response-Feld `eta_seconds` von `POST
  /chats/{session_id}/command` (Task 2), bestehende DOM-Elemente `#command-form`,
  `#command-status` (`backend/app/templates/detail.html:28-32`, unverändert).

- [ ] **Step 1: Ergänze den Countdown**

In `backend/app/static/app.js`, der bestehende Block (Zeile 171-229) — direkt nach der Zeile
`const button = form.querySelector("button[type=submit]");` (Zeile 176) einfügen:

```js
  const countdown = makeCountdown(status, "gesendet");
```

Danach in derselben Funktion:
- In `poll` (Zeile 178-198): vor `status.textContent = "Befehl ausgeführt..."` (Zeile 186) und vor
  dem `Fehlgeschlagen`-Zweig (Zeile 187-189) jeweils `countdown.stop();` einfügen; im `.catch`
  (Zeile 194-197) ebenfalls `countdown.stop();` vor der bestehenden `status.textContent`-Zeile
  einfügen.
- Im Submit-Handler (Zeile 200-228): nach dem bestehenden `poll(data.job_id);` (Zeile 222) davor
  `countdown.start(data.eta_seconds);` einfügen; im `.catch` (Zeile 224-227) `countdown.stop();`
  vor der bestehenden `status.textContent`-Zeile einfügen.

Der vollständige Block sieht danach so aus:

```js
document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("command-form");
  if (!form) return;
  const sessionId = form.dataset.sessionId;
  const status = document.getElementById("command-status");
  const button = form.querySelector("button[type=submit]");
  const countdown = makeCountdown(status, "gesendet");

  const poll = (jobId) => {
    fetch(`/jobs/${jobId}/status`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data) => {
        if (data.status === "done") {
          countdown.stop();
          status.textContent = "Befehl ausgeführt — Chat wird beim nächsten Sync aktualisiert (bis zu 60s).";
        } else if (data.status === "failed") {
          countdown.stop();
          status.textContent = `Fehlgeschlagen: ${data.result_text || "unbekannter Fehler"}`;
          button.disabled = false;
        } else {
          setTimeout(() => poll(jobId), 3000);
        }
      })
      .catch(() => {
        countdown.stop();
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
      .then((res) =>
        res
          .json()
          .catch(() => ({}))
          .then((data) => ({ ok: res.ok, data }))
      )
      .then(({ ok, data }) => {
        if (!ok) {
          status.textContent = data && data.detail ? data.detail : "Fehler beim Senden — bitte erneut versuchen.";
          button.disabled = false;
          return;
        }
        countdown.start(data.eta_seconds);
        poll(data.job_id);
      })
      .catch(() => {
        countdown.stop();
        status.textContent = "Fehler beim Senden — bitte erneut versuchen.";
        button.disabled = false;
      });
  });
});
```

- [ ] **Step 2: Manuelle Verifikation**

Run: `./run.sh`, eine erlaubte Session öffnen (`command_allowed` true), im Composer einen Prompt
absenden. Erwartet: "Wird gesendet..." wechselt sofort in den Countdown
(`Wird gesendet... (ca. Xs)`), zählt runter, zeigt bei 0 den indeterminate Text, kein JS-Fehler in
der Konsole.

- [ ] **Step 3: Commit**

```bash
git add backend/app/static/app.js
git commit -m "feat(frontend): send-counter on detail-page resume command form

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01AEtvEMAUNPMprfKjH61X7z"
```

---

## Self-Review Notes

- **Spec coverage:** Dropdown (Task 3, 5, 6), Vorselektion via `localStorage` (Task 5),
  Persistenz erst bei erfolgreichem Senden (Task 6, `poll`-Zweig `status === "done"`),
  `eta_seconds` an beiden Endpunkten (Task 1, 2), Countdown-Helper + Verdrahtung an drei Stellen
  (Task 4, 6, 7) — alle Spec-Abschnitte sind durch genau einen Task abgedeckt.
- **Placeholder scan:** keine TBD/TODO, jeder Step enthält vollständigen Code.
- **Type/Namens-Konsistenz:** `makeCountdown(statusNode, verb) -> {start, stop}` wird in Task 4
  definiert und in Task 6/7 identisch destrukturiert (`const countdown = makeCountdown(...)`,
  `countdown.start(...)`, `countdown.stop()`); `#project-select`/`#new-session-form`/
  `#command-status` aus Task 3 werden in Task 5/6 mit denselben IDs referenziert; `eta_seconds`
  wird in Task 1/2 (Backend) und Task 6/7 (Frontend, `data.eta_seconds`) konsistent benannt.
