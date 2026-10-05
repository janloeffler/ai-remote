# Chat Rendering: Tables, Code Highlighting, Bare Paths + Dark/Light Toggle — Design

**Date:** 2026-08-06
**Status:** Approved for planning

## Overview

Chat messages (transcripts synced from Claude Code / Cursor sessions) currently render through a
single Jinja filter, `render_markdown` in `backend/app/markdown_filter.py`, on every page that
shows message content — the chat detail page and the "load more" flow (which does a full
`location.reload()`, so there is no separate client-side rendering path to keep in sync).

Three concrete gaps in that pipeline, confirmed against real transcript content in `data/app.db`:

1. **Tables don't render.** The `tables` Markdown extension isn't enabled, so GFM pipe-tables in
   agent output pass through as literal `| a | b |` text.
2. **Code blocks have no color.** `codehilite` (already enabled) emits Pygments `<span class="...">`
   tokens wrapped in `<div class="codehilite">`, but `bleach` doesn't allow `div` today — the
   wrapper (and its CSS hook) is silently stripped, leaving mono-color text in a plain box.
3. **Bare filesystem paths stay plain prose.** Markdown only monospaces text the model already
   wrapped in backticks. Real transcripts contain unquoted paths like
   `/Users/yourname/source/demo-project/.env` in the middle of prose sentences.

Additionally, the app has no manual dark/light toggle — only the OS-level `prefers-color-scheme`
media query. This adds one, and reworks the theme system so it composes correctly with the new
Pygments syntax-highlight CSS.

Everything lives in three existing files. No new endpoints, no DB schema changes, no new backend
routes.

## Rendering pipeline (`markdown_filter.py`)

New pipeline, in order:

```python
import re

import bleach
import markdown as _markdown
from bs4 import BeautifulSoup, NavigableString

_PATH_RE = re.compile(r"(?<![\w`/])(/(?:[\w.\-]+/)*[\w.\-]+|~(?:/[\w.\-]+)*)")
_SKIP_ANCESTORS = {"code", "pre", "a"}


def render_markdown(text: str) -> str:
    html = _markdown.markdown(
        text or "", extensions=["fenced_code", "codehilite", "tables"]
    )
    allowed_tags = {
        "p", "br", "strong", "em", "code", "pre", "blockquote",
        "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "a", "hr", "table", "thead", "tbody", "tr", "th", "td", "span", "div",
    }
    allowed_attrs = {
        "a": ["href", "title"],
        "code": ["class"],
        "span": ["class"],
        "pre": ["class"],
        "div": ["class"],
    }
    clean = bleach.clean(html, tags=allowed_tags, attributes=allowed_attrs, strip=True)
    return _wrap_bare_paths(clean)


def _wrap_bare_paths(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(string=True):
        if any(p.name in _SKIP_ANCESTORS for p in node.parents):
            continue
        if not _PATH_RE.search(node):
            continue
        pieces = []
        last = 0
        for m in _PATH_RE.finditer(node):
            pieces.append(node[last:m.start()])
            code = soup.new_tag("code")
            code.string = m.group(0)
            pieces.append(code)
            last = m.end()
        pieces.append(node[last:])
        node.replace_with(*[
            NavigableString(p) if isinstance(p, str) else p for p in pieces
        ])
    return str(soup)
```

- **`tables`** — trivial addition; `table/thead/tbody/tr/th/td` are already in the `bleach`
  allowlist, so GFM pipe-tables just work once the extension is on.
- **`div` allowed** — restores the `codehilite` wrapper `bleach` currently strips. `class` is the
  only attribute allowed on it, same pattern as `code`/`span`/`pre`; the values are fixed Pygments
  token-class strings, not attacker-controllable markup, so this adds no XSS surface.
- **`_wrap_bare_paths`** runs *after* `bleach.clean`, on already-sanitized HTML, and only ever
  inserts one known-safe, attribute-less tag (`<code>`) around text it found in trusted output — it
  cannot introduce a new injection vector.
  - Walks text nodes via `BeautifulSoup`/`html.parser` (stdlib parser, no new parsing dependency
    beyond `beautifulsoup4` itself) rather than regexing the raw HTML string, so matches inside tag
    attributes (e.g. an `href`) are structurally impossible.
  - Skips any text node with a `code`, `pre`, or `a` ancestor — never double-wraps an
    already-backticked path, never touches a fenced code block's contents, never mangles link text.
  - Regex `(?<![\w\`/])(/(?:[\w.\-]+/)*[\w.\-]+|~(?:/[\w.\-]+)*)` — scope is absolute Unix paths and
    `~/...` only (confirmed as the dominant real-world pattern). The negative lookbehind (word
    character, backtick, **or another `/`**) is what keeps it from misfiring on:
    - a URL's trailing path segment (`https://your-domain.example.com/chats/123` — the `/` is
      preceded by the letter `e`, blocked);
    - the domain-plus-path right after a `://` (`https://your-domain.example.com/chats/123` —
      without the `/` case in the lookbehind, the regex would restart its match at the *second*
      slash of `://`, where the next character is a legitimate word character, and wrongly consume
      `/your-domain.example.com/chats/123` as if it were an absolute path; excluding a preceding
      `/` blocks that restart point);
    - prose like "and/or", "true/false", "10/5" (slash preceded by a word character, blocked);
    - spaced division (`a / b` — no word/dot/dash character immediately follows the `/`, so the
      required `[\w.\-]+` fails to match).
- New dependency: **`beautifulsoup4`**, added to `backend/requirements.txt`.

## Theme system

CSS keeps its current "dark is the unconditional default, light is a `prefers-color-scheme`
override" structure, and gains an explicit-override tier on top that a manual toggle can set:

```css
:root { /* existing dark values, unchanged */ }

@media (prefers-color-scheme: light) {
  :root { /* existing light values, unchanged — system light, no explicit preference */ }
}

:root[data-theme="dark"] { /* same values as base :root */ }
:root[data-theme="light"] { /* same values as the light media-query block */ }
```

`:root[data-theme="light"]` (pseudo-class + attribute selector) has higher specificity than a bare
`:root` inside `@media (...)`, so it wins in both directions — forced dark overrides system light,
forced light overrides system dark — regardless of source order.

**Pygments syntax-highlight CSS follows the identical four-tier structure**, scoped to
`.codehilite` instead of custom properties, using the **Gruvbox** pair (`gruvbox-dark` /
`gruvbox-light` — Pygments' most popular purpose-matched light/dark syntax theme; its warm palette
also reads well against the app's existing warm `--accent-claude` orange):

```python
# one-off generation (not run at request time — paste output into style.css with a
# comment noting the command, so it's regenerated by hand if the theme ever changes):
from pygments.formatters import HtmlFormatter
HtmlFormatter(style="gruvbox-dark").get_style_defs(".codehilite")
HtmlFormatter(style="gruvbox-light").get_style_defs(':root[data-theme="light"] .codehilite')
HtmlFormatter(style="gruvbox-dark").get_style_defs(':root[data-theme="dark"] .codehilite')
```

The plain `gruvbox-dark` block is the default (unconditional) rule; the `gruvbox-light` block is
additionally wrapped in `@media (prefers-color-scheme: light) { ... }` for the system-light,
no-override case; the two `[data-theme=...]` blocks are the explicit overrides. Generated once,
pasted as static CSS into `style.css` — no runtime Pygments CSS generation.

### Toggle UI

- An icon button (🌙 / ☀️, 44px touch target — matching the existing `button`/`input` sizing
  convention) in `base.html`'s `<header>`, alongside the "Neue Session" / "Audit-Log" nav links.
- `app.js` click handler: reads the *effective* current theme (`document.documentElement.dataset.theme`
  if set, else `matchMedia('(prefers-color-scheme: dark)').matches`), flips it, writes the new value
  to both `document.documentElement.dataset.theme` and `localStorage.theme`.
- **Anti-flash-of-wrong-theme**: a small synchronous inline `<script>` in `base.html`'s `<head>`,
  placed before the `style.css` `<link>`, reads `localStorage.theme` and stamps `data-theme` on
  `<html>` before first paint:

  ```html
  <script>
    (function () {
      var t = localStorage.getItem("theme");
      if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    })();
  </script>
  ```

- **Persistence is per-device**, not synced: Mac Chrome and iPhone Safari each have their own
  `localStorage`, so they can independently end up on different explicit themes (or both stay on
  "auto"/system). This is the expected, standard behavior for a `localStorage`-based toggle — no
  account-level sync is in scope.
- First visit on any device: no stored preference → "auto" → follows system
  `prefers-color-scheme`, same as today.

## Mobile / overflow handling

Long code lines or wide tables must scroll *inside* their own block, never force the whole page to
scroll horizontally (this matters on the iPhone Safari viewport specifically):

```css
.message pre, .message table { overflow-x: auto; max-width: 100%; }
.message table { display: block; } /* table scrolls as a unit; rows/cells keep table layout */
```

## Testing

- New/extended `pytest` cases for `render_markdown` (in `backend/tests/`):
  - a GFM pipe-table renders as an actual `<table>` with `<thead>`/`<tbody>`;
  - a fenced code block gets `codehilite` `<span>` token classes inside a `<div class="codehilite">`;
  - a bare `/Users/yourname/...` path in prose gets wrapped in `<code>`;
  - a path already inside backticks, or inside a fenced code block, is **not** double-wrapped;
  - a URL's trailing path segment (`https://.../chats/123`) is **not** wrongly wrapped;
  - prose containing `and/or`, `true/false`, spaced division (`a / b`) is left untouched.
- Theme toggle and Pygments colors: **manual visual verification only**, on Mac Chrome and iPhone
  Safari, in all four states (system-dark/system-light × auto/explicit-override). No JS test
  framework exists in this repo today (confirmed — `app.js` has none), and introducing one solely
  for a CSS/`localStorage` toggle isn't warranted.

## Non-Goals

- No cross-device sync of the manual theme preference — each browser/device remembers its own.
- No detection/highlighting scope beyond absolute Unix paths and `~/...` (no relative paths like
  `src/app.py`, no bare URLs without a scheme) — confirmed against real transcript content as the
  dominant pattern; broader detection can be a later iteration if it turns out to be missed.
- No change to *when* or *how* messages are fetched/synced — purely a rendering/presentation change
  applied at display time to already-stored message content.
