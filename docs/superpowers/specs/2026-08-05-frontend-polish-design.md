# Frontend Polish — Design

**Date:** 2026-08-05
**Status:** Approved for planning
**Builds on:** `docs/superpowers/specs/2026-08-04-ai-remote-chat-viewer-design.md` (Plan A, merged)

## Overview

Plan A's frontend is functional but visually bare (hardcoded dark colors, plain `<select>`s,
raw ISO timestamps), has no sorting or path-narrowing beyond a plain substring filter, and always
loads a chat's entire history in one shot. This polishes the existing list/detail pages and makes
history loading incremental — no new backend subsystems, no changes to auth, sync, or guardrails;
the existing `fetch_full` job type gains a payload parameter but no new job type is introduced.

## Goals

- A modern, appealing visual design that works well on iPhone (primary use case) and in any
  Chrome browser on a laptop (secondary use case) — responsive, not two separate UIs.
- Sorting of the chat list: by last activity (newest first — default, or oldest first), by
  title (A–Z), by project path (A–Z). Combinable with the existing tool/date-range/search filters.
- A path filter upgraded from a plain text box to a combobox: typeable, with a live-filtered
  dropdown of known project paths (substring match, not just prefix), and `~/` expanding to the
  Mac's home directory.
- All timestamps shown in the UI (chat list, message timestamps, the "Mac last seen" line)
  reformatted to German date/time in the `Europe/Berlin` timezone, with a German relative-time
  suffix (e.g. `Fr, 31.07. 14:55 (vor 6 Tagen)`).
- Chat detail pages load history incrementally (most-recent chunk first, "load more" for
  further back) instead of all-at-once, with a separate button to still load everything in one go.

## Non-Goals

- No new backend subsystems, no changes to auth/sync/guardrails, no new job type.
- No JS build step, no external CDN dependencies — stays consistent with Plan A's
  zero-dependency frontend.
- No hierarchical folder-tree browsing for the path filter — a flat, substring-filtered list of
  known paths is sufficient at this data scale (currently ~150 distinct paths).
- No native app icon design system — one simple inline SVG icon for the browser tab /
  home-screen icon, not a full icon set.

## Visual Redesign

- CSS custom properties (`--bg`, `--surface`, `--text`, `--accent`, ...) with
  `@media (prefers-color-scheme: dark)` / `(light)` — matches the viewer's OS theme automatically
  on both iPhone and desktop Chrome.
- Card-style list items (rounded corners, subtle shadow, generous spacing) replacing the current
  plain `<li>` rows; tool badges colored distinctly per tool.
- Touch-friendly tap targets (~44px minimum) throughout.
- Mobile-first layout: single column full-width below ~480px; on wider viewports (laptop Chrome)
  a centered container at a comfortable reading width (~720–800px, up from the current 640px).
- The filter/sort controls become a single "toolbar" card: wraps on narrow viewports, stays on
  one line on wide ones.
- One simple, hand-drawn inline SVG icon used as favicon and `apple-touch-icon`, so "Add to Home
  Screen" doesn't produce a generic gray icon.

## Sorting

- New `sort` query param on `GET /`: `date_desc` (default), `date_asc`, `title_asc`, `path_asc`.
- Combinable with `tool`, `project`, `group`, `q` exactly like the existing filters — one more
  `<select>` in the toolbar, submitted with the same form.
- `db.get_sessions` maps `sort` to the `ORDER BY` clause: `last_updated_at DESC/ASC`,
  `title COLLATE NOCASE ASC`, `project_path COLLATE NOCASE ASC` respectively.

## Path Combobox

- A new `db.get_distinct_project_paths(conn) -> list[str]` query
  (`SELECT DISTINCT project_path FROM sessions WHERE project_path != '' ORDER BY project_path`),
  called once per `/` page load and embedded into the page as a JSON array (Jinja2 `| tojson`) —
  no separate AJAX endpoint needed at this data scale.
- The path `<input>` becomes a small hand-built combobox (ARIA combobox pattern): as the user
  types, a dropdown below the input shows only paths containing the typed substring
  (case-insensitive), narrowing live; arrow keys move a highlighted option, Enter/click selects
  it into the input, Escape closes the dropdown. Free text that matches nothing is still
  accepted and submitted — the existing substring `LIKE` filter in `db.get_sessions` already
  handles that, unchanged.
- `~/` expansion: a new `LOCAL_HOME_DIR` setting (env var, default `/Users/yourname`) in
  `backend/app/settings.py`. Expansion happens in two places that must agree:
  - Server-side, when the `project` query param is parsed (so a shared link or manually-typed
    URL with `~/...` works even without JS).
  - Client-side, in the combobox's live-filtering JS (the home directory value is embedded in
    the same JSON blob as the path list), so the dropdown narrows correctly as the user types
    `~/` before the form is even submitted.

## Timestamp Formatting

- New Jinja2 filter `de_datetime`, registered in `backend/app/main.py` alongside the existing
  `markdown` filter, implemented in a new `backend/app/datetime_filter.py`.
- Input: an ISO8601 UTC timestamp string (as stored everywhere in the DB) or an empty
  string/`None` (known to occur for some Cursor messages — see Plan A's Cursor `createdAt`
  null-handling fix). Empty/`None` input renders a neutral placeholder, never raises.
- Conversion: UTC → `Europe/Berlin` via `zoneinfo.ZoneInfo` (stdlib, handles CET/CEST correctly).
- Date part: `{weekday-abbrev}, {DD.MM.}` — weekday abbreviations `Mo/Di/Mi/Do/Fr/Sa/So`; if the
  timestamp's year differs from the current year, the year is appended directly after the
  trailing dot (`05.08.2025`), otherwise the trailing dot alone marks the truncated year
  (`31.07.`). Time part: always `HH:MM` (24h).
- Relative suffix in German with correct grammar, in ascending buckets: under a minute → "gerade
  eben"; minutes → "vor 1 Minute" / "vor N Minuten"; hours → "vor 1 Stunde" / "vor N Stunden";
  days (up to 6) → "vor 1 Tag" / "vor N Tagen"; weeks (up to ~4) → "vor 1 Woche" / "vor N Wochen";
  months (up to ~11) → "vor 1 Monat" / "vor N Monaten"; years → "vor 1 Jahr" / "vor N Jahren".
  Approximate day-based bucketing (no calendar-exact month math) — acceptable for a personal
  chat viewer.
- Applied everywhere a timestamp is shown: the chat list's last-activity time, every message
  timestamp on the detail page, and the "Mac last seen" line (which switches from its current
  relative-only `_format_last_contact` helper to showing the full absolute-plus-relative format
  via the same filter — that helper is removed).

## Chat History Pagination

Replaces the single "Vollständige Historie laden" button with two distinct actions on the chat
detail page, so opening a long chat doesn't force loading everything at once:

- **"Mehr laden"** — loads the most recent `CHAT_HISTORY_PAGE_SIZE` messages (env var, default
  10) the first time; each subsequent click loads `PAGE_SIZE` further, older messages. Stays
  visible as long as not everything is loaded yet.
- **"Gesamte Historie laden"** — unchanged existing behavior, loads everything in one job.

**Mechanism (reuses the existing `fetch_full` job — no new job type):**

- `sessions` gains a new column `loaded_message_count INTEGER NOT NULL DEFAULT 0`.
  **Superseded during implementation** (real-data testing found `message_count` — the raw
  indexer's count of all JSONL events — is unreliable for this purpose, since tool-only turns
  have no text and are never returned by `get_full_messages`; a session can genuinely finish
  loading everything available while `loaded_message_count` never reaches `message_count`):
  the agent reports ground-truth completion (`is_complete`) directly from the job's payload
  `count` vs. the actual total available messages, and the backend trusts that flag for
  `full_content_synced` instead of comparing `loaded_message_count` to `message_count`.
- The `fetch_full` job's `payload` becomes a small JSON object: `{"count": N}` means "give me the
  N most recent messages total"; empty/absent means "give me everything" (unchanged behavior for
  the "Gesamte Historie laden" button). A "Mehr laden" click computes
  `N = current loaded_message_count + CHAT_HISTORY_PAGE_SIZE` server-side when creating the job.
- No new agent capability needed: `get_full_messages` (both Claude Code and Cursor) already
  builds the complete in-memory message list every time; the executor slices `messages[-N:]`
  before uploading when a `count` is present, and separately reports whether that count already
  covered everything available (see above). Sliced messages keep their original chronological
  `idx` values (no renumbering), so ordering stays correct as more gets loaded over time.
- `replace_messages` (unchanged) already replaces "whatever is currently stored" for a session
  with the newly-uploaded set, and already rebuilds the message search-index rows from exactly
  that set — so incremental search-as-you-load (confirmed requirement) falls out for free, no
  logic change needed there.
- Detail page: `loaded_message_count == 0` → preview only, plus both buttons.
  `full_content_synced` false → the messages loaded so far, plus both buttons still visible.
  `full_content_synced` true → all available messages, no buttons.

## Testing

- Backend (additional to the above): tests for the `fetch_full` payload's `count` semantics
  (job creation computes the right `N`; `replace_messages` correctly updates
  `loaded_message_count`); a test proving a second "Mehr laden" job's `N` accounts for what's
  already loaded, not a fixed re-fetch of the same page.
- Agent (additional): a test proving `execute_fetch_full` slices to the last `N` messages when
  the job payload carries a `count`, and returns everything when it doesn't (existing behavior,
  regression-tested).

- Backend: parametrized tests for `get_sessions` across all four `sort` values; a test for
  `get_distinct_project_paths`; a test for `~/` expansion in the `project` query param
  (server-side); tests for `de_datetime` using a **fixed reference timestamp** (not
  `datetime.now()`, to avoid the time-of-day flake pattern already known from Plan A's
  `date_group` test) covering: minutes/hours/days/weeks/months/years buckets, singular vs.
  plural grammar, same-year vs. different-year date formatting, and empty/`None` input.
- Frontend/JS: no test framework in this project (a deliberate Plan A decision) — the combobox's
  interactive behavior (typing, arrow-key navigation, selection, `~/` expansion) is verified
  manually in a browser, consistent with how `app.js` was verified in Plan A.
- Manual smoke test against real data at the end: confirm each sort order visually, confirm the
  path combobox narrows correctly against real project folders (including a `~/`-prefixed
  entry), confirm timestamp formatting looks right for both a very recent chat and one from a
  previous year.

## Known Limitations

- The path combobox loads the full distinct-path list up front (embedded in the page); fine at
  today's scale (~150 paths) but would need a live-search endpoint if that grows by an order of
  magnitude or more.
- Relative-time bucketing uses fixed day-based approximations for weeks/months/years rather than
  calendar-exact arithmetic — a chat from "11 months ago" right at a year boundary could
  occasionally read as "vor 1 Jahr" a few days early or late. Acceptable for a personal tool.
- "Mehr laden" re-sends the full growing tail-window each time (count-based, not true
  offset/cursor pagination) — the agent re-parses/re-slices the whole session on every click
  rather than fetching only the delta. Cheap at this scale (local file/DB reads, not network),
  simpler than true pagination, and reuses the existing job/upload plumbing unchanged.

## Assumptions

- Single Mac, single user (unchanged from Plan A) — `LOCAL_HOME_DIR` and
  `CHAT_HISTORY_PAGE_SIZE` are single configurable values, not per-user.
- Continues Plan A's no-build-step, no-external-dependency frontend approach.
