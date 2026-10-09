"""Chat images: storage on disk, type sniffing and the markers that reference them.

The agent uploads image files that a chat refers to; the UI then shows them inline for
``settings.IMAGE_RETENTION_DAYS`` days. Everything that touches the bytes is strict about
what an "image" is (PNG/JPEG/GIF/WebP by magic number, never SVG — it can carry script).
"""

import os
import re
from pathlib import Path

from . import db
from .render_core import BARE_PATH_RE, IMAGE_EXTENSIONS, MARKER_RE, is_image_path, path_key  # noqa: F401  (re-exported)

KEY_RE = re.compile(r"[0-9a-f]{32}")
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


def sniff_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_dir() -> Path:
    configured = os.environ.get("IMAGE_DIR")
    if configured:
        return Path(configured)
    return Path(os.environ.get("DATABASE_PATH", "app.db")).resolve().parent / "images"


def file_path(path_key: str, mime: str) -> Path:
    return image_dir() / f"{path_key}.{_EXTENSIONS[mime]}"


def store(conn, session_id: str, source_path: str, data: bytes) -> tuple[str, str]:
    """Validates and stores the bytes. Returns (path_key, mime); raises ValueError."""
    from . import settings  # lazy: settings insists on API_KEY/SECRET_KEY at import

    if len(data) > settings.IMAGE_MAX_BYTES:
        raise ValueError("too large")
    mime = sniff_mime(data)
    if mime is None:
        raise ValueError("not a supported image")
    key = path_key(session_id, source_path)
    target = file_path(key, mime)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(target)
    db.save_image(conn, session_id, key, source_path, mime, len(data))
    return key, mime


def cleanup_expired(conn) -> int:
    from . import settings

    expired = db.pop_expired_images(conn, settings.IMAGE_RETENTION_DAYS)
    for row in expired:
        try:
            file_path(row["path_key"], row["mime"]).unlink(missing_ok=True)
        except OSError:
            pass
    return len(expired)
