# Toolbar filter triggers & path dropdown fix

**Date:** 2026-08-28
**Status:** Approved

## Problem

The session-list toolbar (`backend/app/templates/list.html`) has two text inputs — free-text search (`q`) and project path filter (`project`) — that both submit the form (full-page GET reload) via a 400ms debounce on every keystroke (`backend/app/static/app.js:320-338`). In practice this means the page reloads while the user is still composing a search term or path, because:

- The debounce fires a **full page navigation**, not a partial/AJAX update, so it's jarring even when the timing is "correct".
- The debounce listener is attached to *all* `input[type="text"]` in the form, which includes `#project-input` — so typing a path also triggers a reload, fighting the field's own autocomplete dropdown.
- On mobile (the primary usage pattern — iPhone, on-screen keyboard, no arrow keys), there is no reliable way to finish typing before the 400ms window elapses between characters.

Separately, the path-filter dropdown (`#project-input` / `#project-listbox`) never shows its suggestion list when the field is empty — `currentMatches()` (`app.js:106-110`) returns `[]` for an empty query — so there's no way to browse all available project paths without typing something first, and ArrowDown does nothing on an empty field either (it reuses the same empty-returning function).

## Goals

- Typing in `q` or `project` never triggers a reload by itself.
- Reload triggers only on an explicit action: Enter/Go key, tapping away from the field (blur), the visible "Filtern" button, or selecting a dropdown suggestion.
- Works well on both desktop (keyboard) and mobile/iPhone (touch, on-screen keyboard, no arrow keys).
- The path dropdown shows the full list of available paths on focus (no typing required) and via ArrowDown, matching the feel of a native `<select>`.
- No regression to existing real-time client-side filtering of the path dropdown while typing.

## Non-goals

- Switching the toolbar from full-page GET navigation to an AJAX/partial-update architecture. Explicitly rejected: the existing SSR pattern is simple, this app has no other AJAX-list-rendering precedent, and the actual complaint is *when* the reload fires, not *how* it renders. Full-page reload is kept.
- Client-side caching (`localStorage`/`sessionStorage`) of the project-paths list. The list is already embedded server-side into every page render (`list.html:35`, `db.get_distinct_project_paths`), it's a small dataset (single unindexed `SELECT DISTINCT`, a handful of rows per the configured `AI_REMOTE_ALLOWED_PROJECTS`), and this design still causes a full reload on every filter action anyway — so the data is always fresh at zero extra request cost. Caching would add code with no measurable benefit.

## Design

### 1. Trigger behavior (`app.js`, toolbar block at lines 320-338)

Remove the debounce-on-`input` block entirely — no code replaces it; typing alone never submits.

Remove `submitButton.hidden = true` — the "Filtern" button becomes a permanently visible, explicit trigger (helps mobile users with no keyboard "Enter").

Add a `blur` handler per text input:

```js
form.querySelectorAll('input[type="text"]').forEach((input) => {
  input.setAttribute("enterkeyhint", "search");
  input.addEventListener("blur", (event) => {
    if (event.relatedTarget && event.relatedTarget.closest("a")) return;
    if (input.value !== input.defaultValue) form.requestSubmit();
  });
});
```

Notes:
- `input.defaultValue` reflects the value the server rendered on last page load (`value="{{ q or '' }}"` / `value="{{ project or '' }}"`). Since this is a full-reload SSR app (no client-side state persists across submissions), comparing against `defaultValue` is a correct and free way to detect "did the user actually change anything since the last load" — no extra bookkeeping needed.
- **Edge case:** clicking a session result link also blurs the focused input. Without a guard, a typed-but-not-yet-submitted value would trigger `requestSubmit()` in a race against the link's own navigation, potentially sending the user to the reloaded search page instead of the chat they clicked. Guarded by skipping submit when `event.relatedTarget` is inside an `<a>` — let the link navigation win.
- Enter-to-submit requires **no new code**: pressing Enter in a text input inside a `<form>` already natively submits it, and nothing currently calls `preventDefault()` on that path except when a dropdown suggestion is highlighted (existing combobox behavior, unchanged). This already works for both desktop Enter and the iOS/Android keyboard's "Go"/"Search" action key.
- `enterkeyhint="search"` is added purely for mobile keyboard labeling (shows a search icon/label instead of a generic return arrow on iOS/Android). Cosmetic, additive, no behavior change.

Selecting a dropdown suggestion keeps submitting immediately (unchanged — `select()` at `app.js:112-118` already calls `input.form.requestSubmit()` directly), since that's an explicit, unambiguous user action.

### 2. Path dropdown full list on focus (`app.js`, combobox block at lines 91-174)

`currentMatches()` currently returns `[]` for an empty query. Change the empty-needle branch to return the full `paths` array instead:

```js
const currentMatches = () => {
  const needle = expandHome(input.value).toLowerCase();
  if (!needle) return paths;
  return paths.filter((p) => p.toLowerCase().includes(needle));
};
```

Add a `focus` listener that opens the list immediately:

```js
input.addEventListener("focus", () => {
  render(currentMatches());
});
```

ArrowDown-when-hidden (`app.js:144-147`) needs no change — it already calls `render(matches)` via the same `currentMatches()`, so it starts working correctly for an empty field once the function above stops returning `[]`.

Real-time filtering while typing (`app.js:137-140`, the combobox's own `input` listener) is untouched — it's a separate listener from the toolbar's debounce block being removed, so live narrowing of suggestions as you type continues to work exactly as today.

## Testing

- Manual: type in `q` without pausing 5+ seconds or blurring — page must not reload. Press Enter — reloads with typed query.
- Manual: type in `project`, verify dropdown narrows live, verify no reload until a suggestion is picked, Enter is pressed, or field is blurred with a changed value.
- Manual: click into empty `project` field — full path list appears. Press ArrowDown after Escape — list reopens.
- Manual: type a query, then click a session result link directly (no Enter/blur elsewhere first) — must navigate to the chat, not reload the filtered list.
- Manual: mobile (iPhone Safari) — verify the keyboard shows a search/go key, verify tapping away from the field (dismissing keyboard) triggers the reload, verify the "Filtern" button is visible and works.
- No automated frontend test suite exists in this repo (`backend/app/static/app.js` has no associated JS tests) — changes are verified manually per above.
