# Chat Rendering (Tables/Code/Paths) + Dark/Light Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make chat message rendering (`render_markdown` in `backend/app/markdown_filter.py`) render GFM tables, color fenced code blocks, and monospace bare filesystem paths — and add a manual dark/light theme toggle that composes correctly with the new syntax-highlight CSS.

**Architecture:** Backend-only rendering pipeline change (Markdown → bleach sanitize → bare-path wrap, all in one filter function used by every page that shows message content) plus a CSS/JS-only theme toggle (an explicit `data-theme` attribute on `<html>`, set by a header button and an anti-flash inline script, layered on top of the existing `prefers-color-scheme` CSS).

**Tech Stack:** Python 3.14, FastAPI, Jinja2, `markdown` (with `fenced_code`/`codehilite`/`tables` extensions), `bleach`, `pygments`, new dependency `beautifulsoup4`; vanilla JS (no framework), plain CSS (no preprocessor).

**Design doc:** `docs/superpowers/specs/2026-08-06-chat-formatting-and-theme-toggle-design.md`

## Global Constraints

- No new backend endpoints, no DB schema changes — this is a rendering/presentation-layer change over already-stored message content.
- New dependency: `beautifulsoup4`, added to `backend/requirements.txt`.
- Bare-path detection scope is **absolute Unix paths and `~/...` only** — no relative paths (`src/app.py`), no bare URLs without a scheme.
- Pygments syntax-highlight theme pair: **`gruvbox-dark` / `gruvbox-light`**.
- Theme preference persists **per-device** via `localStorage` — no cross-device sync.
- No JS test framework exists in this repo (confirmed) — CSS/JS-only changes are verified manually via `./run.sh` in a browser, not via new automated tooling.
- `bleach`'s `div` allowlist entry is `class`-attribute-only, matching the existing pattern for `code`/`span`/`pre`.

---

### Task 1: Enable GFM tables + let the `codehilite` wrapper through sanitization

**Files:**
- Modify: `backend/app/markdown_filter.py` (currently 19 lines, full file)
- Test: `backend/tests/test_markdown_filter.py` (new file)

**Interfaces:**
- Produces: `render_markdown(text: str) -> str` — unchanged signature, already consumed by `backend/app/main.py:29` (`templates.env.filters["markdown"] = render_markdown`) and both templates that use the `| markdown | safe` filter (`detail.html`).

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_markdown_filter.py`:

```python
from app.markdown_filter import render_markdown


def test_table_renders_as_html_table():
    md = "| A | B |\n|---|---|\n| 1 | 2 |\n"
    html = render_markdown(md)
    assert "<table>" in html
    assert "<thead>" in html
    assert "<th>A</th>" in html
    assert "<td>1</td>" in html


def test_fenced_code_block_survives_sanitization_with_highlight_spans():
    md = "```python\nx = 1\n```\n"
    html = render_markdown(md)
    assert '<div class="codehilite">' in html
    assert "<span class=" in html


def test_plain_text_without_markdown_features_still_renders():
    html = render_markdown("just a sentence, no special formatting")
    assert "just a sentence, no special formatting" in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_markdown_filter.py -v`
Expected: `test_table_renders_as_html_table` and
`test_fenced_code_block_survives_sanitization_with_highlight_spans` FAIL (no `<table>`/`<thead>`
in output; no `<div class="codehilite">` since `bleach` currently strips it). The plain-text test
passes already — that's fine, it's here as a regression guard for later steps.

- [ ] **Step 3: Implement**

Replace `backend/app/markdown_filter.py` in full:

```python
import markdown as _markdown
import bleach


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
    return bleach.clean(html, tags=allowed_tags, attributes=allowed_attrs, strip=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_markdown_filter.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Run the full existing suite to confirm no regressions**

Run: `cd backend && .venv/bin/pytest -v`
Expected: all tests PASS (nothing else touches `markdown_filter.py`'s output shape in a way the
existing suite asserts on — `detail.html` tests, if any, only check for presence of message text,
not exact HTML tags).

- [ ] **Step 6: Commit**

```bash
git add backend/app/markdown_filter.py backend/tests/test_markdown_filter.py
git commit -m "feat(backend): render GFM tables and let codehilite's div wrapper through sanitization"
```

---

### Task 2: Auto-wrap bare absolute paths / `~/...` in `<code>`

**Files:**
- Modify: `backend/app/markdown_filter.py` (from Task 1's version)
- Modify: `backend/requirements.txt:10-11` (add `beautifulsoup4` after `bleach>=6.0`)
- Test: `backend/tests/test_markdown_filter.py` (extend)

**Interfaces:**
- Consumes: Task 1's `render_markdown` structure (Markdown → `bleach.clean` → return).
- Produces: `render_markdown` now additionally bare-path-wraps its output before returning; no
  signature change. Internal helper `_wrap_bare_paths(html: str) -> str` and module-level
  `_PATH_RE` / `_SKIP_ANCESTORS` — not consumed outside this file, but named here since a later
  task's tests reference the same regex behavior.

- [ ] **Step 1: Add the dependency**

`backend/requirements.txt` — add `beautifulsoup4>=4.12` as a new line after `bleach>=6.0`:

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
bleach>=6.0
beautifulsoup4>=4.12
```

Install it: `cd backend && .venv/bin/pip install -r requirements.txt`

- [ ] **Step 2: Write the failing tests**

Append to `backend/tests/test_markdown_filter.py`:

```python
def test_bare_absolute_path_gets_wrapped_in_code():
    html = render_markdown("Open /Users/yourname/source/demo-project/.env please")
    assert "<code>/Users/yourname/source/demo-project/.env</code>" in html


def test_bare_home_relative_path_gets_wrapped():
    html = render_markdown("Check ~/source/ris for details")
    assert "<code>~/source/ris</code>" in html


def test_path_already_in_backticks_is_not_double_wrapped():
    html = render_markdown("See `/Users/yourname/source/ris`")
    assert html.count("<code>") == 1
    assert "<code><code>" not in html


def test_path_inside_fenced_code_block_is_not_touched():
    md = "```bash\ncd /Users/yourname/source/ris\n```\n"
    html = render_markdown(md)
    assert html.count("/Users/yourname/source/ris") == 1
    assert "<code>/Users/yourname/source/ris</code>" not in html


def test_url_with_domain_and_path_is_not_wrongly_wrapped():
    html = render_markdown("See https://your-domain.example.com/chats/123 for the chat")
    assert "<code>" not in html


def test_prose_with_slashes_is_left_alone():
    html = render_markdown("Use and/or, true/false, or compute a / b carefully")
    assert "<code>" not in html


def test_multiple_paths_in_one_message_all_wrapped():
    html = render_markdown("Compare /etc/hosts and /etc/passwd now")
    assert html.count("<code>") == 2
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_markdown_filter.py -v`
Expected: all 7 new tests FAIL (`render_markdown` doesn't wrap bare paths at all yet).

- [ ] **Step 4: Implement**

Replace `backend/app/markdown_filter.py` in full:

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
            code_tag = soup.new_tag("code")
            code_tag.string = m.group(0)
            pieces.append(code_tag)
            last = m.end()
        pieces.append(node[last:])
        node.replace_with(*[
            NavigableString(p) if isinstance(p, str) else p for p in pieces
        ])
    return str(soup)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_markdown_filter.py -v`
Expected: all 10 tests PASS (3 from Task 1 + 7 new).

- [ ] **Step 6: Run the full existing suite to confirm no regressions**

Run: `cd backend && .venv/bin/pytest -v`
Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/markdown_filter.py backend/requirements.txt backend/tests/test_markdown_filter.py
git commit -m "feat(backend): auto-wrap bare absolute/~ paths in <code>, add beautifulsoup4"
```

---

### Task 3: Explicit `data-theme` CSS-variable overrides

**Files:**
- Modify: `backend/app/static/style.css:1-27` (right after the existing `@media (prefers-color-scheme: light)` block)

**Interfaces:**
- Produces: `:root[data-theme="dark"]` / `:root[data-theme="light"]` selectors — Task 5's toggle
  button sets the `data-theme` attribute these selectors key off of.

- [ ] **Step 1: Add the explicit override blocks**

In `backend/app/static/style.css`, immediately after the existing block that ends `}` on line 26
(the `@media (prefers-color-scheme: light) { :root { ... } }` block) and before `* { box-sizing:
border-box; }`, insert:

```css
:root[data-theme="dark"] {
  --bg: #0f1115;
  --surface: #1a1d23;
  --surface-hover: #21252c;
  --text: #eaecef;
  --text-muted: #9aa0a8;
  --border: #2a2e35;
  --accent: #5b9dff;
  --shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
}

:root[data-theme="light"] {
  --bg: #f5f6f8;
  --surface: #ffffff;
  --surface-hover: #f0f1f3;
  --text: #1a1d23;
  --text-muted: #6b7280;
  --border: #e2e4e8;
  --accent: #2563eb;
  --shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
}
```

(Values copied verbatim from the base `:root` block and the `prefers-color-scheme: light` block
respectively — `--accent-claude` and `--accent-cursor` aren't theme-dependent, so they're
intentionally omitted here, same as the existing light-mode override omits them.)

- [ ] **Step 2: Manual verification**

Run `./run.sh` (repo root), open the app in a browser, open DevTools → Elements, select the
`<html>` tag, and manually add `data-theme="light"` as an attribute. Confirm the page switches to
light colors immediately, regardless of the OS appearance setting. Change it to `data-theme="dark"`
and confirm it switches to dark colors regardless of OS appearance. Remove the attribute entirely
and confirm it falls back to following OS appearance (toggle System Settings → Appearance while
watching the page, or use DevTools' "Emulate CSS media feature prefers-color-scheme").

- [ ] **Step 3: Commit**

```bash
git add backend/app/static/style.css
git commit -m "feat(frontend): add explicit data-theme CSS-variable overrides"
```

---

### Task 4: Gruvbox syntax-highlight CSS + mobile overflow handling

**Files:**
- Modify: `backend/app/static/style.css` (append near end of file, and extend the existing
  `.message` block around line 185)

**Interfaces:**
- Consumes: Task 3's `:root[data-theme="dark"]`/`:root[data-theme="light"]` selector pattern (the
  explicit-override tier here follows the identical structure, scoped to `.codehilite` instead of
  custom properties).
- Consumes: Task 1's `<div class="codehilite">` / Pygments `<span class="...">` output — this is
  the CSS that gives those classes actual color.

- [ ] **Step 1: Add mobile overflow handling**

In `backend/app/static/style.css`, in the existing `.messages`/`.message` block (currently lines
177–185), add after `.message time { margin-bottom: 0.35rem; }`:

```css
.message pre, .message table { overflow-x: auto; max-width: 100%; }
.message table { display: block; }
```

- [ ] **Step 2: Append the Gruvbox codehilite CSS**

Append to the end of `backend/app/static/style.css` (this exact block was generated via
`.venv/bin/python3 -c "from pygments.formatters import HtmlFormatter as H; print(H(style='gruvbox-dark').get_style_defs('.codehilite'))"`
and the `gruvbox-light` / `:root[data-theme=...]` equivalents documented in the design doc —
regenerate the same way if the theme pair ever changes):

```css
/* Syntax highlighting (Pygments codehilite) — Gruvbox pair, matching the
   four-tier theme structure above: unconditional dark default, system-light
   override, then explicit data-theme overrides that win regardless of
   system preference. */
.codehilite .hll { background-color: #ebdbb2 }
.codehilite { background: #282828; color: #DDD }
.codehilite .c { color: #928374; font-style: italic } /* Comment */
.codehilite .err { color: #282828; background-color: #FB4934 } /* Error */
.codehilite .esc { color: #DDD } /* Escape */
.codehilite .g { color: #DDD } /* Generic */
.codehilite .k { color: #FB4934 } /* Keyword */
.codehilite .l { color: #DDD } /* Literal */
.codehilite .n { color: #DDD } /* Name */
.codehilite .o { color: #DDD } /* Operator */
.codehilite .x { color: #DDD } /* Other */
.codehilite .p { color: #DDD } /* Punctuation */
.codehilite .ch { color: #928374; font-style: italic } /* Comment.Hashbang */
.codehilite .cm { color: #928374; font-style: italic } /* Comment.Multiline */
.codehilite .c-PreProc { color: #8EC07C; font-style: italic } /* Comment.PreProc */
.codehilite .cp { color: #928374; font-style: italic } /* Comment.Preproc */
.codehilite .cpf { color: #928374; font-style: italic } /* Comment.PreprocFile */
.codehilite .c1 { color: #928374; font-style: italic } /* Comment.Single */
.codehilite .cs { color: #EBDBB2; font-weight: bold; font-style: italic } /* Comment.Special */
.codehilite .gd { color: #282828; background-color: #FB4934 } /* Generic.Deleted */
.codehilite .ge { color: #DDD; font-style: italic } /* Generic.Emph */
.codehilite .ges { color: #DDD; font-weight: bold; font-style: italic } /* Generic.EmphStrong */
.codehilite .gr { color: #FB4934 } /* Generic.Error */
.codehilite .gh { color: #EBDBB2; font-weight: bold } /* Generic.Heading */
.codehilite .gi { color: #282828; background-color: #B8BB26 } /* Generic.Inserted */
.codehilite .go { color: #F2E5BC } /* Generic.Output */
.codehilite .gp { color: #A89984 } /* Generic.Prompt */
.codehilite .gs { color: #DDD; font-weight: bold } /* Generic.Strong */
.codehilite .gu { color: #EBDBB2; text-decoration: underline } /* Generic.Subheading */
.codehilite .gt { color: #FB4934 } /* Generic.Traceback */
.codehilite .kc { color: #FB4934 } /* Keyword.Constant */
.codehilite .kd { color: #FB4934 } /* Keyword.Declaration */
.codehilite .kn { color: #FB4934 } /* Keyword.Namespace */
.codehilite .kp { color: #FB4934 } /* Keyword.Pseudo */
.codehilite .kr { color: #FB4934 } /* Keyword.Reserved */
.codehilite .kt { color: #FB4934 } /* Keyword.Type */
.codehilite .ld { color: #DDD } /* Literal.Date */
.codehilite .m { color: #D3869B } /* Literal.Number */
.codehilite .s { color: #B8BB26 } /* Literal.String */
.codehilite .na { color: #FABD2F } /* Name.Attribute */
.codehilite .nb { color: #FE8019 } /* Name.Builtin */
.codehilite .nc { color: #8EC07C } /* Name.Class */
.codehilite .no { color: #D3869B } /* Name.Constant */
.codehilite .nd { color: #FB4934 } /* Name.Decorator */
.codehilite .ni { color: #DDD } /* Name.Entity */
.codehilite .ne { color: #FB4934 } /* Name.Exception */
.codehilite .nf { color: #8EC07C } /* Name.Function */
.codehilite .nl { color: #DDD } /* Name.Label */
.codehilite .nn { color: #8EC07C } /* Name.Namespace */
.codehilite .nx { color: #DDD } /* Name.Other */
.codehilite .py { color: #DDD } /* Name.Property */
.codehilite .nt { color: #8EC07C } /* Name.Tag */
.codehilite .nv { color: #83A598 } /* Name.Variable */
.codehilite .ow { color: #FB4934 } /* Operator.Word */
.codehilite .pm { color: #DDD } /* Punctuation.Marker */
.codehilite .w { color: #DDD } /* Text.Whitespace */
.codehilite .mb { color: #D3869B } /* Literal.Number.Bin */
.codehilite .mf { color: #D3869B } /* Literal.Number.Float */
.codehilite .mh { color: #D3869B } /* Literal.Number.Hex */
.codehilite .mi { color: #D3869B } /* Literal.Number.Integer */
.codehilite .mo { color: #D3869B } /* Literal.Number.Oct */
.codehilite .sa { color: #B8BB26 } /* Literal.String.Affix */
.codehilite .sb { color: #B8BB26 } /* Literal.String.Backtick */
.codehilite .sc { color: #B8BB26 } /* Literal.String.Char */
.codehilite .dl { color: #B8BB26 } /* Literal.String.Delimiter */
.codehilite .sd { color: #B8BB26 } /* Literal.String.Doc */
.codehilite .s2 { color: #B8BB26 } /* Literal.String.Double */
.codehilite .se { color: #FE8019 } /* Literal.String.Escape */
.codehilite .sh { color: #B8BB26 } /* Literal.String.Heredoc */
.codehilite .si { color: #B8BB26 } /* Literal.String.Interpol */
.codehilite .sx { color: #B8BB26 } /* Literal.String.Other */
.codehilite .sr { color: #B8BB26 } /* Literal.String.Regex */
.codehilite .s1 { color: #B8BB26 } /* Literal.String.Single */
.codehilite .ss { color: #B8BB26 } /* Literal.String.Symbol */
.codehilite .bp { color: #FE8019 } /* Name.Builtin.Pseudo */
.codehilite .fm { color: #8EC07C } /* Name.Function.Magic */
.codehilite .vc { color: #83A598 } /* Name.Variable.Class */
.codehilite .vg { color: #83A598 } /* Name.Variable.Global */
.codehilite .vi { color: #83A598 } /* Name.Variable.Instance */
.codehilite .vm { color: #83A598 } /* Name.Variable.Magic */
.codehilite .il { color: #D3869B } /* Literal.Number.Integer.Long */

@media (prefers-color-scheme: light) {
  .codehilite .hll { background-color: #3c3836 }
  .codehilite { background: #fbf1c7; }
  .codehilite .c { color: #928374; font-style: italic } /* Comment */
  .codehilite .err { color: #FBF1C7; background-color: #9D0006 } /* Error */
  .codehilite .k { color: #9D0006 } /* Keyword */
  .codehilite .ch { color: #928374; font-style: italic } /* Comment.Hashbang */
  .codehilite .cm { color: #928374; font-style: italic } /* Comment.Multiline */
  .codehilite .c-PreProc { color: #427B58; font-style: italic } /* Comment.PreProc */
  .codehilite .cp { color: #928374; font-style: italic } /* Comment.Preproc */
  .codehilite .cpf { color: #928374; font-style: italic } /* Comment.PreprocFile */
  .codehilite .c1 { color: #928374; font-style: italic } /* Comment.Single */
  .codehilite .cs { color: #3C3836; font-weight: bold; font-style: italic } /* Comment.Special */
  .codehilite .gd { color: #FBF1C7; background-color: #9D0006 } /* Generic.Deleted */
  .codehilite .ge { font-style: italic } /* Generic.Emph */
  .codehilite .gr { color: #9D0006 } /* Generic.Error */
  .codehilite .gh { color: #3C3836; font-weight: bold } /* Generic.Heading */
  .codehilite .gi { color: #FBF1C7; background-color: #79740E } /* Generic.Inserted */
  .codehilite .go { color: #32302F } /* Generic.Output */
  .codehilite .gp { color: #7C6F64 } /* Generic.Prompt */
  .codehilite .gs { font-weight: bold } /* Generic.Strong */
  .codehilite .gu { color: #3C3836; text-decoration: underline } /* Generic.Subheading */
  .codehilite .gt { color: #9D0006 } /* Generic.Traceback */
  .codehilite .kc { color: #9D0006 } /* Keyword.Constant */
  .codehilite .kd { color: #9D0006 } /* Keyword.Declaration */
  .codehilite .kn { color: #9D0006 } /* Keyword.Namespace */
  .codehilite .kp { color: #9D0006 } /* Keyword.Pseudo */
  .codehilite .kr { color: #9D0006 } /* Keyword.Reserved */
  .codehilite .kt { color: #9D0006 } /* Keyword.Type */
  .codehilite .m { color: #8F3F71 } /* Literal.Number */
  .codehilite .s { color: #79740E } /* Literal.String */
  .codehilite .na { color: #B57614 } /* Name.Attribute */
  .codehilite .nb { color: #AF3A03 } /* Name.Builtin */
  .codehilite .nc { color: #427B58 } /* Name.Class */
  .codehilite .no { color: #8F3F71 } /* Name.Constant */
  .codehilite .nd { color: #9D0006 } /* Name.Decorator */
  .codehilite .ne { color: #9D0006 } /* Name.Exception */
  .codehilite .nf { color: #427B58 } /* Name.Function */
  .codehilite .nn { color: #427B58 } /* Name.Namespace */
  .codehilite .nt { color: #427B58 } /* Name.Tag */
  .codehilite .nv { color: #076678 } /* Name.Variable */
  .codehilite .ow { color: #9D0006 } /* Operator.Word */
  .codehilite .mb { color: #8F3F71 } /* Literal.Number.Bin */
  .codehilite .mf { color: #8F3F71 } /* Literal.Number.Float */
  .codehilite .mh { color: #8F3F71 } /* Literal.Number.Hex */
  .codehilite .mi { color: #8F3F71 } /* Literal.Number.Integer */
  .codehilite .mo { color: #8F3F71 } /* Literal.Number.Oct */
  .codehilite .sa { color: #79740E } /* Literal.String.Affix */
  .codehilite .sb { color: #79740E } /* Literal.String.Backtick */
  .codehilite .sc { color: #79740E } /* Literal.String.Char */
  .codehilite .dl { color: #79740E } /* Literal.String.Delimiter */
  .codehilite .sd { color: #79740E } /* Literal.String.Doc */
  .codehilite .s2 { color: #79740E } /* Literal.String.Double */
  .codehilite .se { color: #AF3A03 } /* Literal.String.Escape */
  .codehilite .sh { color: #79740E } /* Literal.String.Heredoc */
  .codehilite .si { color: #79740E } /* Literal.String.Interpol */
  .codehilite .sx { color: #79740E } /* Literal.String.Other */
  .codehilite .sr { color: #79740E } /* Literal.String.Regex */
  .codehilite .s1 { color: #79740E } /* Literal.String.Single */
  .codehilite .ss { color: #79740E } /* Literal.String.Symbol */
  .codehilite .bp { color: #AF3A03 } /* Name.Builtin.Pseudo */
  .codehilite .fm { color: #427B58 } /* Name.Function.Magic */
  .codehilite .vc { color: #076678 } /* Name.Variable.Class */
  .codehilite .vg { color: #076678 } /* Name.Variable.Global */
  .codehilite .vi { color: #076678 } /* Name.Variable.Instance */
  .codehilite .vm { color: #076678 } /* Name.Variable.Magic */
  .codehilite .il { color: #8F3F71 } /* Literal.Number.Integer.Long */
}

:root[data-theme="dark"] .codehilite .hll { background-color: #ebdbb2 }
:root[data-theme="dark"] .codehilite { background: #282828; color: #DDD }
:root[data-theme="dark"] .codehilite .c { color: #928374; font-style: italic } /* Comment */
:root[data-theme="dark"] .codehilite .err { color: #282828; background-color: #FB4934 } /* Error */
:root[data-theme="dark"] .codehilite .esc { color: #DDD } /* Escape */
:root[data-theme="dark"] .codehilite .g { color: #DDD } /* Generic */
:root[data-theme="dark"] .codehilite .k { color: #FB4934 } /* Keyword */
:root[data-theme="dark"] .codehilite .l { color: #DDD } /* Literal */
:root[data-theme="dark"] .codehilite .n { color: #DDD } /* Name */
:root[data-theme="dark"] .codehilite .o { color: #DDD } /* Operator */
:root[data-theme="dark"] .codehilite .x { color: #DDD } /* Other */
:root[data-theme="dark"] .codehilite .p { color: #DDD } /* Punctuation */
:root[data-theme="dark"] .codehilite .ch { color: #928374; font-style: italic } /* Comment.Hashbang */
:root[data-theme="dark"] .codehilite .cm { color: #928374; font-style: italic } /* Comment.Multiline */
:root[data-theme="dark"] .codehilite .c-PreProc { color: #8EC07C; font-style: italic } /* Comment.PreProc */
:root[data-theme="dark"] .codehilite .cp { color: #928374; font-style: italic } /* Comment.Preproc */
:root[data-theme="dark"] .codehilite .cpf { color: #928374; font-style: italic } /* Comment.PreprocFile */
:root[data-theme="dark"] .codehilite .c1 { color: #928374; font-style: italic } /* Comment.Single */
:root[data-theme="dark"] .codehilite .cs { color: #EBDBB2; font-weight: bold; font-style: italic } /* Comment.Special */
:root[data-theme="dark"] .codehilite .gd { color: #282828; background-color: #FB4934 } /* Generic.Deleted */
:root[data-theme="dark"] .codehilite .ge { color: #DDD; font-style: italic } /* Generic.Emph */
:root[data-theme="dark"] .codehilite .ges { color: #DDD; font-weight: bold; font-style: italic } /* Generic.EmphStrong */
:root[data-theme="dark"] .codehilite .gr { color: #FB4934 } /* Generic.Error */
:root[data-theme="dark"] .codehilite .gh { color: #EBDBB2; font-weight: bold } /* Generic.Heading */
:root[data-theme="dark"] .codehilite .gi { color: #282828; background-color: #B8BB26 } /* Generic.Inserted */
:root[data-theme="dark"] .codehilite .go { color: #F2E5BC } /* Generic.Output */
:root[data-theme="dark"] .codehilite .gp { color: #A89984 } /* Generic.Prompt */
:root[data-theme="dark"] .codehilite .gs { color: #DDD; font-weight: bold } /* Generic.Strong */
:root[data-theme="dark"] .codehilite .gu { color: #EBDBB2; text-decoration: underline } /* Generic.Subheading */
:root[data-theme="dark"] .codehilite .gt { color: #FB4934 } /* Generic.Traceback */
:root[data-theme="dark"] .codehilite .kc { color: #FB4934 } /* Keyword.Constant */
:root[data-theme="dark"] .codehilite .kd { color: #FB4934 } /* Keyword.Declaration */
:root[data-theme="dark"] .codehilite .kn { color: #FB4934 } /* Keyword.Namespace */
:root[data-theme="dark"] .codehilite .kp { color: #FB4934 } /* Keyword.Pseudo */
:root[data-theme="dark"] .codehilite .kr { color: #FB4934 } /* Keyword.Reserved */
:root[data-theme="dark"] .codehilite .kt { color: #FB4934 } /* Keyword.Type */
:root[data-theme="dark"] .codehilite .ld { color: #DDD } /* Literal.Date */
:root[data-theme="dark"] .codehilite .m { color: #D3869B } /* Literal.Number */
:root[data-theme="dark"] .codehilite .s { color: #B8BB26 } /* Literal.String */
:root[data-theme="dark"] .codehilite .na { color: #FABD2F } /* Name.Attribute */
:root[data-theme="dark"] .codehilite .nb { color: #FE8019 } /* Name.Builtin */
:root[data-theme="dark"] .codehilite .nc { color: #8EC07C } /* Name.Class */
:root[data-theme="dark"] .codehilite .no { color: #D3869B } /* Name.Constant */
:root[data-theme="dark"] .codehilite .nd { color: #FB4934 } /* Name.Decorator */
:root[data-theme="dark"] .codehilite .ni { color: #DDD } /* Name.Entity */
:root[data-theme="dark"] .codehilite .ne { color: #FB4934 } /* Name.Exception */
:root[data-theme="dark"] .codehilite .nf { color: #8EC07C } /* Name.Function */
:root[data-theme="dark"] .codehilite .nl { color: #DDD } /* Name.Label */
:root[data-theme="dark"] .codehilite .nn { color: #8EC07C } /* Name.Namespace */
:root[data-theme="dark"] .codehilite .nx { color: #DDD } /* Name.Other */
:root[data-theme="dark"] .codehilite .py { color: #DDD } /* Name.Property */
:root[data-theme="dark"] .codehilite .nt { color: #8EC07C } /* Name.Tag */
:root[data-theme="dark"] .codehilite .nv { color: #83A598 } /* Name.Variable */
:root[data-theme="dark"] .codehilite .ow { color: #FB4934 } /* Operator.Word */
:root[data-theme="dark"] .codehilite .pm { color: #DDD } /* Punctuation.Marker */
:root[data-theme="dark"] .codehilite .w { color: #DDD } /* Text.Whitespace */
:root[data-theme="dark"] .codehilite .mb { color: #D3869B } /* Literal.Number.Bin */
:root[data-theme="dark"] .codehilite .mf { color: #D3869B } /* Literal.Number.Float */
:root[data-theme="dark"] .codehilite .mh { color: #D3869B } /* Literal.Number.Hex */
:root[data-theme="dark"] .codehilite .mi { color: #D3869B } /* Literal.Number.Integer */
:root[data-theme="dark"] .codehilite .mo { color: #D3869B } /* Literal.Number.Oct */
:root[data-theme="dark"] .codehilite .sa { color: #B8BB26 } /* Literal.String.Affix */
:root[data-theme="dark"] .codehilite .sb { color: #B8BB26 } /* Literal.String.Backtick */
:root[data-theme="dark"] .codehilite .sc { color: #B8BB26 } /* Literal.String.Char */
:root[data-theme="dark"] .codehilite .dl { color: #B8BB26 } /* Literal.String.Delimiter */
:root[data-theme="dark"] .codehilite .sd { color: #B8BB26 } /* Literal.String.Doc */
:root[data-theme="dark"] .codehilite .s2 { color: #B8BB26 } /* Literal.String.Double */
:root[data-theme="dark"] .codehilite .se { color: #FE8019 } /* Literal.String.Escape */
:root[data-theme="dark"] .codehilite .sh { color: #B8BB26 } /* Literal.String.Heredoc */
:root[data-theme="dark"] .codehilite .si { color: #B8BB26 } /* Literal.String.Interpol */
:root[data-theme="dark"] .codehilite .sx { color: #B8BB26 } /* Literal.String.Other */
:root[data-theme="dark"] .codehilite .sr { color: #B8BB26 } /* Literal.String.Regex */
:root[data-theme="dark"] .codehilite .s1 { color: #B8BB26 } /* Literal.String.Single */
:root[data-theme="dark"] .codehilite .ss { color: #B8BB26 } /* Literal.String.Symbol */
:root[data-theme="dark"] .codehilite .bp { color: #FE8019 } /* Name.Builtin.Pseudo */
:root[data-theme="dark"] .codehilite .fm { color: #8EC07C } /* Name.Function.Magic */
:root[data-theme="dark"] .codehilite .vc { color: #83A598 } /* Name.Variable.Class */
:root[data-theme="dark"] .codehilite .vg { color: #83A598 } /* Name.Variable.Global */
:root[data-theme="dark"] .codehilite .vi { color: #83A598 } /* Name.Variable.Instance */
:root[data-theme="dark"] .codehilite .vm { color: #83A598 } /* Name.Variable.Magic */
:root[data-theme="dark"] .codehilite .il { color: #D3869B } /* Literal.Number.Integer.Long */

:root[data-theme="light"] .codehilite .hll { background-color: #3c3836 }
:root[data-theme="light"] .codehilite { background: #fbf1c7; }
:root[data-theme="light"] .codehilite .c { color: #928374; font-style: italic } /* Comment */
:root[data-theme="light"] .codehilite .err { color: #FBF1C7; background-color: #9D0006 } /* Error */
:root[data-theme="light"] .codehilite .k { color: #9D0006 } /* Keyword */
:root[data-theme="light"] .codehilite .ch { color: #928374; font-style: italic } /* Comment.Hashbang */
:root[data-theme="light"] .codehilite .cm { color: #928374; font-style: italic } /* Comment.Multiline */
:root[data-theme="light"] .codehilite .c-PreProc { color: #427B58; font-style: italic } /* Comment.PreProc */
:root[data-theme="light"] .codehilite .cp { color: #928374; font-style: italic } /* Comment.Preproc */
:root[data-theme="light"] .codehilite .cpf { color: #928374; font-style: italic } /* Comment.PreprocFile */
:root[data-theme="light"] .codehilite .c1 { color: #928374; font-style: italic } /* Comment.Single */
:root[data-theme="light"] .codehilite .cs { color: #3C3836; font-weight: bold; font-style: italic } /* Comment.Special */
:root[data-theme="light"] .codehilite .gd { color: #FBF1C7; background-color: #9D0006 } /* Generic.Deleted */
:root[data-theme="light"] .codehilite .ge { font-style: italic } /* Generic.Emph */
:root[data-theme="light"] .codehilite .gr { color: #9D0006 } /* Generic.Error */
:root[data-theme="light"] .codehilite .gh { color: #3C3836; font-weight: bold } /* Generic.Heading */
:root[data-theme="light"] .codehilite .gi { color: #FBF1C7; background-color: #79740E } /* Generic.Inserted */
:root[data-theme="light"] .codehilite .go { color: #32302F } /* Generic.Output */
:root[data-theme="light"] .codehilite .gp { color: #7C6F64 } /* Generic.Prompt */
:root[data-theme="light"] .codehilite .gs { font-weight: bold } /* Generic.Strong */
:root[data-theme="light"] .codehilite .gu { color: #3C3836; text-decoration: underline } /* Generic.Subheading */
:root[data-theme="light"] .codehilite .gt { color: #9D0006 } /* Generic.Traceback */
:root[data-theme="light"] .codehilite .kc { color: #9D0006 } /* Keyword.Constant */
:root[data-theme="light"] .codehilite .kd { color: #9D0006 } /* Keyword.Declaration */
:root[data-theme="light"] .codehilite .kn { color: #9D0006 } /* Keyword.Namespace */
:root[data-theme="light"] .codehilite .kp { color: #9D0006 } /* Keyword.Pseudo */
:root[data-theme="light"] .codehilite .kr { color: #9D0006 } /* Keyword.Reserved */
:root[data-theme="light"] .codehilite .kt { color: #9D0006 } /* Keyword.Type */
:root[data-theme="light"] .codehilite .m { color: #8F3F71 } /* Literal.Number */
:root[data-theme="light"] .codehilite .s { color: #79740E } /* Literal.String */
:root[data-theme="light"] .codehilite .na { color: #B57614 } /* Name.Attribute */
:root[data-theme="light"] .codehilite .nb { color: #AF3A03 } /* Name.Builtin */
:root[data-theme="light"] .codehilite .nc { color: #427B58 } /* Name.Class */
:root[data-theme="light"] .codehilite .no { color: #8F3F71 } /* Name.Constant */
:root[data-theme="light"] .codehilite .nd { color: #9D0006 } /* Name.Decorator */
:root[data-theme="light"] .codehilite .ne { color: #9D0006 } /* Name.Exception */
:root[data-theme="light"] .codehilite .nf { color: #427B58 } /* Name.Function */
:root[data-theme="light"] .codehilite .nn { color: #427B58 } /* Name.Namespace */
:root[data-theme="light"] .codehilite .nt { color: #427B58 } /* Name.Tag */
:root[data-theme="light"] .codehilite .nv { color: #076678 } /* Name.Variable */
:root[data-theme="light"] .codehilite .ow { color: #9D0006 } /* Operator.Word */
:root[data-theme="light"] .codehilite .mb { color: #8F3F71 } /* Literal.Number.Bin */
:root[data-theme="light"] .codehilite .mf { color: #8F3F71 } /* Literal.Number.Float */
:root[data-theme="light"] .codehilite .mh { color: #8F3F71 } /* Literal.Number.Hex */
:root[data-theme="light"] .codehilite .mi { color: #8F3F71 } /* Literal.Number.Integer */
:root[data-theme="light"] .codehilite .mo { color: #8F3F71 } /* Literal.Number.Oct */
:root[data-theme="light"] .codehilite .sa { color: #79740E } /* Literal.String.Affix */
:root[data-theme="light"] .codehilite .sb { color: #79740E } /* Literal.String.Backtick */
:root[data-theme="light"] .codehilite .sc { color: #79740E } /* Literal.String.Char */
:root[data-theme="light"] .codehilite .dl { color: #79740E } /* Literal.String.Delimiter */
:root[data-theme="light"] .codehilite .sd { color: #79740E } /* Literal.String.Doc */
:root[data-theme="light"] .codehilite .s2 { color: #79740E } /* Literal.String.Double */
:root[data-theme="light"] .codehilite .se { color: #AF3A03 } /* Literal.String.Escape */
:root[data-theme="light"] .codehilite .sh { color: #79740E } /* Literal.String.Heredoc */
:root[data-theme="light"] .codehilite .si { color: #79740E } /* Literal.String.Interpol */
:root[data-theme="light"] .codehilite .sx { color: #79740E } /* Literal.String.Other */
:root[data-theme="light"] .codehilite .sr { color: #79740E } /* Literal.String.Regex */
:root[data-theme="light"] .codehilite .s1 { color: #79740E } /* Literal.String.Single */
:root[data-theme="light"] .codehilite .ss { color: #79740E } /* Literal.String.Symbol */
:root[data-theme="light"] .codehilite .bp { color: #AF3A03 } /* Name.Builtin.Pseudo */
:root[data-theme="light"] .codehilite .fm { color: #427B58 } /* Name.Function.Magic */
:root[data-theme="light"] .codehilite .vc { color: #076678 } /* Name.Variable.Class */
:root[data-theme="light"] .codehilite .vg { color: #076678 } /* Name.Variable.Global */
:root[data-theme="light"] .codehilite .vi { color: #076678 } /* Name.Variable.Instance */
:root[data-theme="light"] .codehilite .vm { color: #076678 } /* Name.Variable.Magic */
:root[data-theme="light"] .codehilite .il { color: #8F3F71 } /* Literal.Number.Integer.Long */
```

- [ ] **Step 3: Manual verification**

Run `./run.sh`, open a chat session containing a fenced code block (or add one via a new-session
test command). Confirm code renders with Gruvbox colors. Toggle `data-theme` via DevTools (as in
Task 3) between `"dark"`/`"light"`/removed, and confirm the code block's colors switch alongside
the page's own theme in all three states. Confirm a wide table or long code line scrolls
horizontally *inside its own box* on a narrow viewport (DevTools device toolbar, iPhone size) —
the page itself must not gain a horizontal scrollbar.

- [ ] **Step 4: Commit**

```bash
git add backend/app/static/style.css
git commit -m "feat(frontend): Gruvbox syntax-highlight CSS + mobile-safe overflow for code/tables"
```

---

### Task 5: Dark/light toggle button + anti-flash script

**Files:**
- Modify: `backend/app/templates/base.html` (28 lines, full file)
- Modify: `backend/app/static/app.js:310` (append a new `DOMContentLoaded` block)

**Interfaces:**
- Consumes: Task 3/4's `:root[data-theme="dark"]`/`:root[data-theme="light"]` CSS — this task is
  what actually sets/clears that attribute at runtime.

- [ ] **Step 1: Add the anti-flash script and toggle button to `base.html`**

Replace `backend/app/templates/base.html` in full:

```html
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}AI Remote Chats{% endblock %}</title>
  <script>
    (function () {
      var t = localStorage.getItem("theme");
      if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    })();
  </script>
  <link rel="manifest" href="/static/manifest.json">
  <link rel="stylesheet" href="/static/style.css">
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='22' fill='%235b9dff'/><text x='50' y='66' font-size='54' text-anchor='middle' fill='white' font-family='-apple-system,sans-serif'>AI</text></svg>">
  <link rel="apple-touch-icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='22' fill='%235b9dff'/><text x='50' y='66' font-size='54' text-anchor='middle' fill='white' font-family='-apple-system,sans-serif'>AI</text></svg>">
</head>
<body>
  <header>
    <a href="/">AI Remote Chats</a>
    <nav>
      <a href="/projects/new">Neue Session</a>
      <a href="/jobs">Audit-Log</a>
      <button id="theme-toggle" type="button" aria-label="Hell/Dunkel umschalten">🌙</button>
    </nav>
  </header>
  {% if remote_commands_paused %}
  <div class="paused-banner">Remote-Befehle sind pausiert.</div>
  {% endif %}
  <main>{% block content %}{% endblock %}</main>
  <footer class="build-info">Version from {{ build_timestamp }}</footer>
  <script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Add the `#theme-toggle` button's own sizing to `style.css`**

In `backend/app/static/style.css`, extend the existing `header nav` rule (currently `header nav {
display: inline-flex; gap: 0.75rem; margin-left: 1rem; font-size: 0.85rem; }`) — add immediately
after it:

```css
#theme-toggle {
  min-height: 32px;
  min-width: 32px;
  padding: 0;
  background: transparent;
  border: none;
  font-size: 1rem;
  line-height: 1;
}
```

(Deliberately smaller than the app's general 44px `button`/`input` touch-target rule — it's an
icon-only header control, not a form action; 32px is still comfortably tappable and matches the
header's compact height.)

- [ ] **Step 3: Add the toggle click handler to `app.js`**

Append to the end of `backend/app/static/app.js` (after the existing final `DOMContentLoaded`
block, currently ending at line 310):

```js

document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.getElementById("theme-toggle");
  if (!toggle) return;

  const effectiveTheme = () => {
    const explicit = document.documentElement.getAttribute("data-theme");
    if (explicit === "light" || explicit === "dark") return explicit;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  };

  const applyTheme = (theme) => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("theme", theme);
    toggle.textContent = theme === "dark" ? "🌙" : "☀️";
  };

  toggle.textContent = effectiveTheme() === "dark" ? "🌙" : "☀️";

  toggle.addEventListener("click", () => {
    applyTheme(effectiveTheme() === "dark" ? "light" : "dark");
  });
});
```

- [ ] **Step 4: Run the full existing suite to confirm no regressions**

Run: `cd backend && .venv/bin/pytest -v`
Expected: all tests PASS (no existing test asserts on exact `base.html`/header markup — confirmed
by grep across `backend/tests/` before writing this plan).

- [ ] **Step 5: Manual end-to-end verification**

Run `./run.sh`, open the app in a Mac browser:
- Click the toggle button — page switches theme instantly, button icon flips (🌙 ↔ ☀️).
- Reload the page — the explicit choice persists (no flash of the other theme first).
- Navigate to a different page (e.g. `/jobs`) — the explicit choice still applies there too.
- Clear `localStorage` (DevTools → Application → Local Storage → delete `theme`) and reload —
  falls back to following OS appearance again.
- Repeat the click-and-reload check on an iPhone Safari (same backend URL) — confirms the anti-flash
  script and toggle work without any browser-specific issues; note that its `localStorage` is
  independent from the Mac's, so it starts back at "follow OS appearance" until toggled there too.

- [ ] **Step 6: Commit**

```bash
git add backend/app/templates/base.html backend/app/static/style.css backend/app/static/app.js
git commit -m "feat(frontend): dark/light theme toggle button with anti-flash inline script"
```

## Non-Goals (carried over from the design doc)

- No cross-device sync of the manual theme preference.
- No path-detection beyond absolute Unix paths and `~/...`.
- No changes to message fetch/sync timing or logic.
