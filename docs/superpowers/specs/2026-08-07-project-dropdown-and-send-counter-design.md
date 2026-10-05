# Projekt-Dropdown + Sende-Counter — Design

**Date:** 2026-08-07
**Status:** Approved for planning
**Builds on:** `docs/superpowers/specs/2026-08-06-load-more-eta-design.md` (ETA-Countdown-Pattern
und `_compute_eta_seconds`)

## Overview

Zwei unabhängige, aber im selben Umbau zusammengefasste Verbesserungen:

1. **Projekt-Dropdown auf `/projects/new`:** Statt heute ein volles Formular pro Allow-List-Projekt
   untereinander anzuzeigen, wählt der User zuerst ein Projekt aus einem `<select>`-Dropdown; das
   zuletzt erfolgreich verwendete Projekt ist vorselektiert, damit ein wiederkehrender User direkt
   einen Prompt absetzen kann, ohne erst zu suchen.
2. **Sende-Counter:** "Wird gesendet..." bekommt denselben ETA-Countdown wie "Wird geladen..."
   (siehe referenziertes Spec) — an beiden Stellen, an denen der Text heute vorkommt: dem neuen
   Single-Form auf `/projects/new` und dem Resume-Command-Formular auf der Session-Detailseite.

Kein neues Backend-Subsystem; beide Punkte bauen auf bestehenden Mechanismen auf
(`ALLOWED_PROJECTS`, `_compute_eta_seconds`, `localStorage` für Theme als Vorbild für
`lastProject`).

## 1. Projekt-Dropdown

### Template (`backend/app/templates/new_session.html`)

Die aktuelle `<ul class="project-command-list">` mit einem `<form class="new-session-form">` pro
Projekt wird durch **ein** Formular ersetzt:

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

Backend-Route `GET /projects/new` (`main.py:211-220`) bleibt unverändert — liefert weiterhin
`allowed_projects` aus `settings.ALLOWED_PROJECTS`.

### Vorselektion (`app.js`)

Neuer Block, analog zum bestehenden Theme-Pattern (`app.js:312-333`):

```js
document.addEventListener("DOMContentLoaded", () => {
  const select = document.getElementById("project-select");
  if (!select) return;
  const last = localStorage.getItem("lastProject");
  if (last && [...select.options].some((o) => o.value === last)) {
    select.value = last;
  }
});
```

Kein Server-Roundtrip, kein neues DB-Feld — reines Browser-`localStorage`, wie bei der Theme-Wahl.
Ist der gespeicherte Wert nicht (mehr) in der Allow-List (z. B. nach ENV-Änderung), bleibt die
Browser-Default-Auswahl (erste Option) bestehen — kein Fehlerzustand.

### Persistenz

`lastProject` wird **nicht** bei jeder Dropdown-Auswahl geschrieben, sondern erst wenn der
ausgelöste `new_session`-Job tatsächlich erfolgreich abgeschlossen wurde (`status === "done"` im
Poll, siehe Abschnitt 2). Reines Umschalten im Dropdown ohne Senden ändert nichts an der
gespeicherten Präferenz.

### CSS (`backend/app/static/style.css:263-287`)

`.project-command-list`/`li`-Regeln für die Listen-Darstellung entfallen; `.new-session-form`
bzw. das neue `#new-session-form` behält das bestehende Flex/Spacing-Styling für
Select/Textarea/Button, jetzt nur einmal statt pro Listenelement.

## 2. Sende-Counter

### Backend

Beide Endpunkte, die heute nur `{"job_id": ...}` liefern, ergänzen `eta_seconds` — exakt dieselbe
Berechnung wie bei `fetch_full` (`main.py:79`), kein neuer Helper nötig:

```python
# main.py:223-235 — POST /projects/command
job_id = db.create_job(conn, "new_session", body.project_path, payload=...)
eta_seconds = _compute_eta_seconds(db.get_last_agent_contact(conn), settings.AI_REMOTE_INTERVAL_SECONDS)
return {"job_id": job_id, "eta_seconds": eta_seconds}
```

```python
# main.py:196-208 — POST /chats/{session_id}/command
job_id = db.create_job(conn, "resume_message", session_id, payload=...)
eta_seconds = _compute_eta_seconds(db.get_last_agent_contact(conn), settings.AI_REMOTE_INTERVAL_SECONDS)
return {"job_id": job_id, "eta_seconds": eta_seconds}
```

`_compute_eta_seconds` bleibt in `main.py` wie heute definiert — keine Signaturänderung.

### Frontend (`app.js`)

Der bestehende Countdown (`startCountdown`/`stopCountdown`, aktuell nur im `fetch-status`-Block,
`app.js:14-36`) wird in eine wiederverwendbare Hilfsfunktion extrahiert, die einen Status-Node und
ein Verb-Label ("geladen" vs. "gesendet") nimmt:

```js
function makeCountdown(statusNode, verb) {
  let countdownId = null;
  const stop = () => {
    if (countdownId) { clearInterval(countdownId); countdownId = null; }
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

Verdrahtung an drei Stellen:

1. **`fetch-status`-Block** (`app.js:1-84`): nutzt `makeCountdown(status, "geladen")` statt der
   inline definierten Funktionen — Verhalten unverändert.
2. **Neues Single-Form auf `/projects/new`**: nach erfolgreichem POST auf `/projects/command` →
   `countdown.start(eta_seconds)`; beim Poll-Erfolg (`status === "done"`) wird zusätzlich
   `localStorage.setItem("lastProject", projectPath)` gesetzt (siehe Abschnitt 1), bevor der
   bestehende Erfolgstext gesetzt wird.
3. **`#command-form` auf der Detailseite** (`app.js:171-229`): gleiche Verdrahtung mit
   `makeCountdown(status, "gesendet")`.

`countdown.stop()` wird in jedem Poll-Endzustand aufgerufen (`done`, `failed`, Netzwerkfehler im
`catch`) — an denselben Stellen, an denen heute bereits `button.disabled = false` gesetzt wird.

`eta_seconds === 0` zeigt sofort den indeterminate Text, kein `(ca. 0s)`-Flackern — wie beim
bestehenden Load-More-Countdown.

## Testing

- `backend/tests/test_commands.py`: bestehende Tests (`test_new_session_command_creates_job` etc.)
  bleiben grün, da nur ein zusätzliches Response-Feld hinzukommt. Neuer Test prüft
  `response.json()["eta_seconds"]` ist vorhanden und ein `int`.
- `backend/tests/test_detail_and_jobs.py`: analog neue Tests für `POST
  /chats/{session_id}/command` — `eta_seconds` bei nie kontaktiertem Agent (= volles Intervall),
  bei kürzlichem Kontakt (reduziert) und bei überfälligem Kontakt (auf 0 geklammert) — spiegelt die
  bestehenden `test_fetch_full_response_eta_seconds_*`-Tests.
- `test_new_session_form_lists_allowed_projects`: Assertion bleibt gültig (`path in response.text`
  matcht weiterhin den `<option>`-Text).
- Kein JS-Testframework im Repo — Dropdown-Vorselektion, Persistenz-Timing und Countdown-Optik
  werden manuell via `./run.sh` im Browser verifiziert (Dropdown wählen → Senden → Countdown
  beobachten → Reload → Vorselektion prüfen).

## Non-Goals

- Keine durchsuchbare Combobox (wie beim Projekt-Filter auf der Session-Liste) — bei der kleinen,
  statischen Allow-List reicht ein einfaches `<select>`.
- Keine serverseitige Persistenz des zuletzt verwendeten Projekts (kein DB-Feld, kein
  User-Account-Konzept) — rein `localStorage`, pro Browser/Gerät.
- Kein Multi-Projekt-Senden in einem Schritt — das neue Single-Form deckt weiterhin nur ein Projekt
  pro Submit ab, wie die bisherigen Einzelformulare.
- Keine Änderung an der Poll-Frequenz (3s) oder an der Job-Ausführung selbst — der Countdown ist
  weiterhin rein kosmetisch und unabhängig vom tatsächlichen Fertigwerden.
