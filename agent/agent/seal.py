"""Pure helpers that turn plaintext sync/job data into E2E ciphertext (and back).

Everything works on copies: session dicts are cached across cycles by the sources and
must never be mutated.
"""

import json

from . import e2e
from .render_core import ImageContext, render_markdown


def seal_message(session_id: str, message: dict, keys: e2e.Keys) -> dict:
    html = render_markdown(message["content"], ImageContext(session_id, available=set()))
    sealed = dict(message)
    sealed["content"] = e2e.encrypt_text(keys, html, e2e.aad_message(session_id, message["idx"]))
    return sealed


def seal_messages(session_id: str, messages: list[dict], keys: e2e.Keys) -> list[dict]:
    return [seal_message(session_id, m, keys) for m in messages]


def seal_session(session: dict, keys: e2e.Keys) -> dict:
    sid = session["id"]
    sealed = dict(session)
    if "title" in sealed:
        sealed["title"] = e2e.encrypt_text(keys, sealed["title"] or "", e2e.aad_title(sid))
    if "last_message_preview" in sealed:
        preview = sealed["last_message_preview"] or ""
        body = json.dumps({"text": preview, "html": render_markdown(preview, ImageContext(sid))})
        sealed["last_message_preview"] = e2e.encrypt_text(keys, body, e2e.aad_preview(sid))
    if "recent_messages" in sealed:
        sealed["recent_messages"] = seal_messages(sid, sealed["recent_messages"], keys)
    return sealed


def seal_result_text(job_id, text: str, keys: e2e.Keys) -> str:
    return e2e.encrypt_text(keys, text, e2e.aad_job_result(job_id)) if text else ""


class PromptError(Exception):
    pass


def open_job_prompt(job: dict, keys: e2e.Keys) -> dict:
    """Copy of a resume_message/new_session job with the payload prompt decrypted.

    Raises PromptError when it cannot be decrypted with the AAD of this very job.
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
        prompt = e2e.decrypt_text(keys, payload["prompt"], aad)
    except (ValueError, TypeError) as exc:
        raise PromptError("cannot decrypt prompt") from exc
    opened = dict(job)
    opened["payload"] = json.dumps({**payload, "prompt": prompt})
    return opened
