"""E2E `search` job: scans local plaintext, returns ids of matching sessions."""

import json

from . import ai_tools, claude_code_source, cursor_source, e2e

MAX_RESULTS = 100


def _fail(reason: str) -> dict:
    return {"status": "failed", "result_text": reason, "messages": [], "is_complete": False}


def _decrypt_query(job: dict, keys: "e2e.Keys") -> str | None:
    try:
        payload = json.loads(job.get("payload") or "{}")
        value = payload["query"]
        return e2e.decrypt_text(keys, value, e2e.AAD_SEARCH).strip()
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _matches(needle: str, session: dict, messages: list[dict]) -> bool:
    if needle in (session.get("title") or "").casefold():
        return True
    if needle in (session.get("last_message_preview") or "").casefold():
        return True
    return any(needle in (m.get("content") or "").casefold() for m in messages)


def execute_search(job: dict, keys: "e2e.Keys | None", enabled_tools=ai_tools.ALL_TOOLS) -> dict:
    """Returns {"status", "result_text": JSON {"ids": [...]} (plaintext; the caller encrypts)}."""
    if keys is None:
        return _fail("search jobs require E2E mode")
    query = _decrypt_query(job, keys)
    if not query:
        return _fail("cannot decrypt search query" if query is None else "empty search query")
    needle = query.casefold()

    sessions: list[tuple[dict, str]] = []
    if ai_tools.CLAUDE_CODE in enabled_tools:
        sessions += [(s, "claude-code") for s in claude_code_source.list_claude_code_sessions()]
    if ai_tools.CURSOR in enabled_tools:
        sessions += [(s, "cursor") for s in cursor_source.list_cursor_sessions()]

    hits = []
    for session, tool in sessions:
        raw_id = session["id"].split(":", 1)[-1]
        if tool == "claude-code":
            messages = claude_code_source.get_full_messages(raw_id)
        else:
            messages = cursor_source.get_full_messages(raw_id)
        if _matches(needle, session, messages):
            hits.append(session)
    hits.sort(key=lambda s: s.get("last_updated_at") or "", reverse=True)
    ids = [s["id"] for s in hits[:MAX_RESULTS]]
    return {"status": "done", "result_text": json.dumps({"ids": ids}), "messages": [], "is_complete": False}
