"""Pure helpers that turn plaintext sync/job data into E2E ciphertext (and back).

Everything works on copies: session dicts are cached across cycles by the sources and
must never be mutated.
"""

import json
import os
import re
import time
from pathlib import Path

from . import e2e
from .render_core import ImageContext, render_markdown


def _image_ctx(session_id: str, images: bool, **kw) -> ImageContext | None:
    return ImageContext(session_id, **kw) if images else None


def seal_message(session_id: str, message: dict, keys: e2e.Keys, images: bool = True) -> dict:
    html = render_markdown(message["content"], _image_ctx(session_id, images, available=set()))
    sealed = dict(message)
    sealed["content"] = e2e.encrypt_text(keys, html, e2e.aad_message(session_id, message["idx"]))
    return sealed


def seal_messages(session_id: str, messages: list[dict], keys: e2e.Keys, images: bool = True) -> list[dict]:
    return [seal_message(session_id, m, keys, images) for m in messages]


def seal_session(session: dict, keys: e2e.Keys, images: bool = True) -> dict:
    sid = session["id"]
    sealed = dict(session)
    if "title" in sealed:
        sealed["title"] = e2e.encrypt_text(keys, sealed["title"] or "", e2e.aad_title(sid))
    if "last_message_preview" in sealed:
        preview = sealed["last_message_preview"] or ""
        body = json.dumps({"text": preview, "html": render_markdown(preview, _image_ctx(sid, images))})
        sealed["last_message_preview"] = e2e.encrypt_text(keys, body, e2e.aad_preview(sid))
    if "recent_messages" in sealed:
        sealed["recent_messages"] = seal_messages(sid, sealed["recent_messages"], keys, images)
    return sealed


def seal_result_text(job_id, text: str, keys: e2e.Keys) -> str:
    return e2e.encrypt_text(keys, text, e2e.aad_job_result(job_id)) if text else ""


class PromptError(Exception):
    pass


MAX_PROMPT_CHARS = 32_000
PROMPT_MAX_AGE_MS = 60 * 60 * 1000
PROMPT_MAX_FUTURE_MS = 5 * 60 * 1000
USED_IDS_RETENTION_MS = 2 * 60 * 60 * 1000
_RID_RE = re.compile(r"[0-9a-f]{32}")


def used_ids_path(state_path: Path) -> Path:
    return state_path.with_name("used_prompt_ids.json")


def _load_used(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and type(v) is int}


def _save_used(path: Path, used: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(used))
    os.replace(tmp, path)


def _unwrap_envelope(plaintext: str) -> tuple[str, str, int]:
    try:
        env = json.loads(plaintext)
    except json.JSONDecodeError as exc:
        raise PromptError("invalid prompt envelope") from exc
    if (
        not isinstance(env, dict)
        or type(env.get("v")) is not int
        or env["v"] != 1
        or not isinstance(env.get("prompt"), str)
        or not isinstance(env.get("rid"), str)
        or not _RID_RE.fullmatch(env["rid"])
        or type(env.get("ts")) is not int
    ):
        raise PromptError("invalid prompt envelope")
    return env["prompt"], env["rid"], env["ts"]


def _claim_prompt_id(state_path: Path, rid: str, ts: int, now_ms: int) -> None:
    """Rejects a replayed rid, otherwise records it durably (before the command runs)."""
    if ts < now_ms - PROMPT_MAX_AGE_MS or ts > now_ms + PROMPT_MAX_FUTURE_MS:
        raise PromptError("prompt expired")
    path = used_ids_path(state_path)
    used = _load_used(path)
    if rid in used:
        raise PromptError("prompt already used (replay)")
    used = {k: v for k, v in used.items() if v >= now_ms - USED_IDS_RETENTION_MS}
    used[rid] = ts
    try:
        _save_used(path, used)
    except OSError as exc:
        raise PromptError("cannot record prompt id") from exc


def open_job_prompt(job: dict, keys: e2e.Keys, state_path: Path) -> dict:
    """Copy of a resume_message/new_session job with the payload prompt decrypted.

    The plaintext is an envelope {"v":1,"prompt","rid","ts"}; stale, replayed or malformed
    prompts raise PromptError. Raises PromptError when it cannot be decrypted with the AAD
    of this very job.
    """
    try:
        payload = json.loads(job.get("payload") or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict) or "prompt" not in payload:
        return job  # the executor reports the missing prompt itself
    if job["type"] == "resume_message":
        aad = e2e.aad_resume_prompt(job["target"])
    else:
        tool = payload.get("tool")
        if not isinstance(tool, str):
            raise PromptError("cannot decrypt prompt")
        aad = e2e.aad_new_session_prompt(job["target"], tool)
    try:
        plaintext = e2e.decrypt_text(keys, payload["prompt"], aad)
    except (ValueError, TypeError) as exc:
        raise PromptError("cannot decrypt prompt") from exc
    prompt, rid, ts = _unwrap_envelope(plaintext)
    if len(prompt) > MAX_PROMPT_CHARS:
        raise PromptError("prompt too long")
    _claim_prompt_id(state_path, rid, ts, int(time.time() * 1000))
    opened = dict(job)
    opened["payload"] = json.dumps({**payload, "prompt": prompt})
    return opened
