# "Mehr laden" ETA Countdown — Design

**Date:** 2026-08-06
**Status:** Approved for planning
**Builds on:** `docs/superpowers/specs/2026-08-05-frontend-polish-design.md` (incremental history loading)

## Overview

Clicking "Mehr laden" or "Gesamte Historie laden" immediately shows "Wird geladen...", but the
job it triggers is only picked up by the background agent on its next check-in — which happens
at most once every `AI_REMOTE_INTERVAL_SECONDS` (configurable, default 60s). The user has no idea
whether the wait will be 2 seconds or a minute. This adds a countdown so they know roughly how
long to expect, using the backend's existing knowledge of when the agent last checked in for a
tighter estimate than a flat worst-case countdown.

No new backend subsystems, no changes to auth/sync/job execution — purely an ETA estimate
surfaced alongside the existing `fetch-full` job flow.

## Backend

### New setting

`backend/app/settings.py` gains:

```python
AI_REMOTE_INTERVAL_SECONDS = int(os.environ.get("AI_REMOTE_INTERVAL_SECONDS") or "60")
```

Follows the existing `CHAT_HISTORY_PAGE_SIZE` pattern: `or "60"` rather than `os.environ.get(name,
"60")`, so an empty-but-present `.env` line (a plausible copy-paste of `example.env`) falls back
to the default instead of crashing at import with `ValueError`.

### ETA computation

`POST /chats/{session_id}/fetch-full` additionally returns `eta_seconds` in its JSON response,
computed entirely from the server's own clock (never comparing a server timestamp against the
client's clock, which would be subject to skew):

```python
def _compute_eta_seconds(last_contact: str | None, interval_seconds: int) -> int:
    if last_contact is None:
        return interval_seconds  # agent never seen — worst case
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_contact)).total_seconds()
    return max(0, min(interval_seconds, round(interval_seconds - elapsed)))
```

- `last_contact` comes from the existing `db.get_last_agent_contact(conn)` (already used on the
  chat list page for "Mac zuletzt erreichbar").
- Clamped to `[0, interval_seconds]`: an overdue/offline agent (`elapsed > interval`) reports
  `eta_seconds = 0` rather than a misleading full countdown; clock anomalies can't produce a
  negative or oversized number either.
- Lives as a private helper in `main.py` (not `db.py`) so it stays unit-testable without a DB
  connection — `db.py` only stores/retrieves the raw timestamp; the ETA policy is endpoint logic.

### Deploy plumbing

`AI_REMOTE_INTERVAL_SECONDS` currently only reaches the local agent process (via `setup-agent.sh`
sourcing `.env` directly). The backend container needs it explicitly passed through in both
deploy paths, matching the existing `LOCAL_HOME_DIR`/`CHAT_HISTORY_PAGE_SIZE` pattern:

- `docker-compose.yml`: add `AI_REMOTE_INTERVAL_SECONDS=${AI_REMOTE_INTERVAL_SECONDS:-60}` to the
  `backend` service's `environment` block.
- `deploy-production-scp.sh`: add the same line to its inline compose heredoc.

### `example.env` documentation

Add a comment above `AI_REMOTE_INTERVAL_SECONDS` explaining it now has two consumers: the local
agent's own poll loop, and the backend's ETA estimate shown in the "Mehr laden" UI.

## Frontend

`app.js`'s existing 3-second `poll(jobId)` loop — the thing that actually detects `done`/`failed`
— is unchanged. A second, purely cosmetic 1-second countdown timer drives the status text:

```js
let countdownId = null;

const stopCountdown = () => {
  if (countdownId) { clearInterval(countdownId); countdownId = null; }
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

- `startFetch` reads `eta_seconds` from the `fetch-full` response and calls
  `startCountdown(eta_seconds)` instead of setting a static "Wird geladen..." string.
- `eta_seconds === 0` shows the indeterminate message on the very first tick — no flash of
  "(ca. 0s)".
- `stopCountdown()` is called from every exit path in `poll()` (`done`, `failed`, and the `catch`
  block) so no stray timer survives a page reload or re-enabled buttons.
- If the job completes before the countdown reaches 0 (agent was already mid-cycle), the existing
  `location.reload()` fires as today; the countdown timer is cleared along with everything else —
  no special-casing needed.
- No persistent hint is shown before the buttons are clicked — the countdown only appears once a
  fetch is in progress, keeping the change scoped to the actual reported problem.

## Testing

- `backend/tests/test_settings.py`: add `AI_REMOTE_INTERVAL_SECONDS` default / override /
  empty-string-falls-back cases, mirroring the existing `CHAT_HISTORY_PAGE_SIZE` tests.
- New unit tests for `_compute_eta_seconds`: no prior contact (→ full interval), recent contact
  (e.g. 10s ago with a 60s interval → ~50s), overdue contact (e.g. 90s ago with a 60s interval →
  0).
- Endpoint-level test asserting `fetch-full`'s JSON response includes `eta_seconds`.
- No JS test framework exists in this repo (`app.js` has none today either) — the countdown will
  be verified manually via `./run.sh` in the browser rather than adding new tooling.

## Non-Goals

- No change to how often the agent actually polls, nor to job execution/reporting.
- No precise ETA for job *completion* — only for the agent's next check-in. Execution time after
  pickup and the frontend's own 3s poll latency are not modeled; the indeterminate fallback
  message covers that tail honestly instead of guessing.
- No persistent/idle-state hint about the sync interval before a fetch is started.
