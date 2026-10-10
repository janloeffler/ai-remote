"""Uploads images that chats refer to, so the web UI can show them inline.

Two paths feed it:
- automatic: images the user pasted into a chat (`[Image: source: <path>]` in a *user*
  message in the agent's own session data), uploaded as sessions sync;
- on demand: a `fetch_image` job queued when someone clicks an image reference in the UI.

The job's path arrives from the backend, i.e. from outside this machine, and is turned into
a file read — so an on-demand path is only honored when the session's own data names it
(a user-pasted image) or it lies inside an allow-listed project. Only real images (magic
number, size cap, no SVG) ever leave the machine.
"""

import base64
import json
import re
import sys
from pathlib import Path

import httpx

from . import claude_code_source, cursor_source, e2e, render_core, uploader
from .state import IMAGES_STATE_FILENAME

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_UPLOADS_PER_CYCLE = 10
_MARKER_RE = re.compile(r"\[Image: source: ([^\]\n]+?)\]")
_IMAGE_EXT_RE = re.compile(r".+\.(?:png|jpe?g|gif|webp)", re.IGNORECASE)
_STATE_KEEP = 20_000


def _is_image(data: bytes) -> bool:
    return (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith(b"\xff\xd8\xff")
        or data[:6] in (b"GIF87a", b"GIF89a")
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )


def pasted_image_paths(messages: list[dict]) -> list[str]:
    """Paths of images the user pasted. Assistant text is ignored on purpose: a model (or
    anything it read) could print a marker naming any file on this machine."""
    paths: list[str] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        for match in _MARKER_RE.finditer(message.get("content", "")):
            path = match.group(1).strip()
            if path not in paths:
                paths.append(path)
    return paths


def read_image(path: str, allowed_roots: list[Path] | None = None) -> bytes | None:
    """The file's bytes if it is a regular, small, real image; None otherwise.

    With `allowed_roots`, the *resolved* path (symlinks followed) must lie inside one of
    them. Without, the caller vouches for the path (the user pasted it).
    """
    if not _IMAGE_EXT_RE.fullmatch(path):
        return None
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        if allowed_roots is not None and not any(resolved.is_relative_to(root) for root in allowed_roots):
            return None
        if not resolved.is_file() or resolved.stat().st_size > MAX_IMAGE_BYTES:
            return None
        data = resolved.read_bytes()
    except (OSError, RuntimeError):
        return None
    return data if _is_image(data) else None


def _post_image(
    base_url: str, api_key: str, session_id: str, path: str, data: bytes, client: httpx.Client, keys=None
):
    if keys is not None:
        data = e2e.encrypt_bytes(keys, data, e2e.aad_image(session_id, render_core.path_key(session_id, path)))
    return client.post(
        f"{base_url}/sync/image",
        json={"session_id": session_id, "path": path, "data_b64": base64.b64encode(data).decode("ascii")},
        headers=uploader.mode_headers(api_key, keys is not None),
        timeout=60,
    )


def _load_state(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    return set(data) if isinstance(data, list) else set()


def _save_state(path: Path, uploaded: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(uploaded)[-_STATE_KEEP:]))


def upload_pasted_images(
    config, client: httpx.Client, session_messages: dict[str, list[dict]], keys: "e2e.Keys | None" = None
) -> None:
    """Uploads pasted images of the given sessions that were not uploaded before.

    Remembered locally once sent, so an image the server has since expired is not pushed
    again — it is then fetched on demand if someone still wants it.
    """
    state_path = config.state_path.with_name(IMAGES_STATE_FILENAME)
    uploaded = _load_state(state_path)
    budget = MAX_UPLOADS_PER_CYCLE
    changed = False
    for session_id, messages in session_messages.items():
        for path in pasted_image_paths(messages):
            entry = f"{session_id}\0{path}"
            if entry in uploaded:
                continue
            if budget <= 0:
                break
            data = read_image(path)
            if data is None:
                continue  # gone (temp files get cleaned) or not an image — try again never
            try:
                response = _post_image(config.backend_url, config.api_key, session_id, path, data, client, keys)
            except httpx.HTTPError as exc:
                print(f"image upload failed: {exc}", file=sys.stderr)
                budget = 0
                break
            if response.status_code in (403, 404):
                # 403: the backend has image upload off. 404: it doesn't know the session yet.
                if response.status_code == 403:
                    if changed:
                        _save_state(state_path, uploaded)
                    return
                continue
            if response.status_code >= 400:
                uploaded.add(entry)  # rejected for good (size/type) — don't retry every cycle
                changed = True
                continue
            uploaded.add(entry)
            changed = True
            budget -= 1
    if changed:
        _save_state(state_path, uploaded)


def execute_fetch_image(job: dict, config, client: httpx.Client, keys: "e2e.Keys | None" = None) -> dict:
    session_id = job["target"]
    try:
        payload = json.loads(job.get("payload") or "{}")
    except json.JSONDecodeError:
        payload = {}
    path = payload.get("path") if isinstance(payload, dict) else None
    if not isinstance(path, str) or not path:
        return {"status": "failed", "result_text": "missing path in job payload"}

    tool, _, raw_id = session_id.partition(":")
    if tool not in config.enabled_tools:
        return {"status": "failed", "result_text": f"tool is disabled: {tool}"}
    if tool == "claude-code":
        messages = claude_code_source.get_full_messages(raw_id)
    elif tool == "cursor":
        messages = cursor_source.get_full_messages(raw_id)
    else:
        return {"status": "failed", "result_text": f"unknown tool: {tool}"}

    if path in pasted_image_paths(messages):
        data = read_image(path)
    elif any(path in m.get("content", "") for m in messages):
        roots = [Path(p).expanduser().resolve() for p in config.allowed_projects]
        data = read_image(path, allowed_roots=roots)
    else:
        return {"status": "failed", "result_text": "path not found in session"}
    if data is None:
        return {"status": "failed", "result_text": "image not readable, too large or not allowed"}

    try:
        response = _post_image(config.backend_url, config.api_key, session_id, path, data, client, keys)
    except httpx.HTTPError as exc:
        return {"status": "failed", "result_text": f"upload failed: {exc}"}
    if response.status_code >= 400:
        return {"status": "failed", "result_text": f"upload rejected: HTTP {response.status_code}"}
    return {"status": "done", "result_text": ""}
