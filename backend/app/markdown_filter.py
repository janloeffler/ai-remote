import re

import bleach
import markdown as _markdown
from bs4 import BeautifulSoup, NavigableString

_PATH_RE = re.compile(r"(?<![\w`/])(/(?:[\w.\-]+/)*[\w.\-]+|~(?:/[\w.\-]+)+)")
_ALIGN_RE = re.compile(r'<(th|td) style="text-align: (left|right|center);">')
_SKIP_ANCESTORS = {"code", "pre", "a"}


def render_markdown(text: str) -> str:
    html = _markdown.markdown(
        text or "", extensions=["fenced_code", "codehilite", "tables"]
    )
    html = _ALIGN_RE.sub(r'<\1 class="align-\2">', html)
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
        "th": ["class"],
        "td": ["class"],
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
