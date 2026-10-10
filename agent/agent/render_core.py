"""Markdown + chat-image-marker rendering, free of any backend state.

This file is shared byte-identically between backend/app/render_core.py and
agent/agent/render_core.py so the Mac agent can render the exact same HTML as the
server. Edit the backend copy and copy it over; a test fails if they diverge.
Only stdlib + markdown, bleach, bs4 (and pygments via codehilite) may be imported;
no relative imports, no db/settings.
"""

import hashlib
import html as _html
import re
import secrets
from dataclasses import dataclass, field
from urllib.parse import quote

import bleach
import markdown as _markdown
from bs4 import BeautifulSoup, NavigableString

IMAGE_EXTENSIONS = r"(?:png|jpe?g|gif|webp)"
# `[Image: source: /tmp/x.png]` is what Claude Code writes for a pasted image.
MARKER_RE = re.compile(r"\[Image: source: ([^\]\n]+?)\]")
# A bare path to an image in running text (not inside backticks or a URL).
BARE_PATH_RE = re.compile(
    rf"(?<![\w`/.:])((?:/(?:[\w.\-]+/)*|~/(?:[\w.\-]+/)*)[\w.\-]+\.{IMAGE_EXTENSIONS})(?![\w/])", re.IGNORECASE
)


def path_key(session_id: str, path: str) -> str:
    return hashlib.sha256(f"{session_id}\0{path}".encode("utf-8")).hexdigest()[:32]


def is_image_path(path: str) -> bool:
    return re.fullmatch(rf".+\.{IMAGE_EXTENSIONS}", path, re.IGNORECASE) is not None


_PATH_RE = re.compile(r"(?<![\w`/])(/(?:[\w.\-]+/)*[\w.\-]+|~(?:/[\w.\-]+)+)")
_ALIGN_RE = re.compile(r'<(th|td) style="text-align: (left|right|center);">')
_SKIP_ANCESTORS = {"code", "pre", "a"}


@dataclass
class ImageContext:
    """Where chat images of one session stand: which are already on the server."""

    session_id: str
    available: set[str] = field(default_factory=set)


_FENCE_RE = re.compile(r"(```.*?```|~~~.*?~~~)", re.DOTALL)
# A whole tag, quoted attribute values included (they may contain ">").
_TAG_RE = re.compile(r"(<(?:[^>\"']|\"[^\"]*\"|'[^']*')*>)")


def _tokenize_images(text: str, refs: list[str], nonce: str) -> str:
    """Swaps image markers / bare image paths for tokens, leaving fenced code alone.

    The per-call nonce makes tokens unforgeable: text that happens to contain a token-shaped
    string never matches the regex built from it.
    """

    def token(path: str) -> str:
        refs.append(path)
        return f"@@IMG{nonce}Z{len(refs) - 1}@@"

    parts = _FENCE_RE.split(text)
    for i in range(0, len(parts), 2):
        parts[i] = MARKER_RE.sub(
            lambda m: token(m.group(1).strip()) if is_image_path(m.group(1).strip()) else m.group(0),
            parts[i],
        )
        parts[i] = BARE_PATH_RE.sub(lambda m: token(m.group(1)), parts[i])
    return "".join(parts)


def _image_html(path: str, ctx: ImageContext) -> str:
    name = _html.escape(path.rsplit("/", 1)[-1])
    key = path_key(ctx.session_id, path)
    if key in ctx.available:
        url = f"/chats/{quote(ctx.session_id, safe='')}/images/{key}"
        return (
            f'<a class="chat-image" href="{url}" target="_blank" rel="noopener">'
            f'<img src="{url}" alt="{name}" loading="lazy"></a>'
        )
    return (
        '<button type="button" class="image-fetch" '
        f'data-session-id="{_html.escape(ctx.session_id)}" data-path="{_html.escape(path, quote=True)}">'
        f'<span class="image-fetch-label">🖼 {name}</span></button>'
    )


def render_markdown(text: str, images: ImageContext | None = None) -> str:
    refs: list[str] = []
    nonce = secrets.token_hex(8)
    if images is not None:
        text = _tokenize_images(text or "", refs, nonce)
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
    wrapped = _wrap_bare_paths(clean)
    if images is None:
        return wrapped
    token_re = re.compile(rf"@@IMG{nonce}Z(\d+)@@")

    def restore(m: re.Match) -> str:
        i = int(m.group(1))
        return _image_html(refs[i], images) if i < len(refs) else m.group(0)

    def plain(m: re.Match) -> str:
        i = int(m.group(1))
        return _html.escape(refs[i], quote=True) if i < len(refs) else m.group(0)

    # Tokens become image HTML only in text nodes; one that ended up inside a tag (an href or
    # title attribute) turns back into its plain, escaped path.
    parts = _TAG_RE.split(wrapped)
    for i, part in enumerate(parts):
        parts[i] = token_re.sub(plain if i % 2 else restore, part)
    return "".join(parts)


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
