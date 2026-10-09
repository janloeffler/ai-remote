import json

import pytest

from agent import e2e, search
from e2e_harness import KEYS


def _job(query):
    return {"id": "s1", "type": "search", "target": "*", "payload": json.dumps({"query": e2e.encrypt_text(KEYS, query, e2e.AAD_SEARCH)})}


@pytest.fixture
def sources(monkeypatch):
    sessions = [
        {"id": "claude-code:a", "title": "Fix Login Bug", "last_message_preview": "", "last_updated_at": "2026-01-01"},
        {"id": "claude-code:b", "title": "other", "last_message_preview": "mentions LOGIN here", "last_updated_at": "2026-03-01"},
        {"id": "claude-code:c", "title": "nothing", "last_message_preview": "", "last_updated_at": "2026-02-01"},
        {"id": "cursor:d", "title": "none", "last_message_preview": "", "last_updated_at": "2026-04-01"},
    ]
    msgs = {"c": [{"idx": 0, "role": "user", "timestamp": "", "content": "deep in a message: Login!"}], "d": []}
    monkeypatch.setattr("agent.claude_code_source.list_claude_code_sessions", lambda: sessions[:3])
    monkeypatch.setattr("agent.cursor_source.list_cursor_sessions", lambda: sessions[3:])
    monkeypatch.setattr("agent.claude_code_source.get_full_messages", lambda raw: msgs.get(raw, []))
    monkeypatch.setattr("agent.cursor_source.get_full_messages", lambda raw: msgs.get(raw, []))
    return sessions


def test_matches_title_preview_and_content_case_insensitive_sorted(sources):
    r = search.execute_search(_job("login"), KEYS)
    assert r["status"] == "done"
    assert json.loads(r["result_text"]) == {"ids": ["claude-code:b", "claude-code:c", "claude-code:a"]}


def test_respects_enabled_tools(sources):
    r = search.execute_search(_job("none"), KEYS, ("claude-code",))
    assert json.loads(r["result_text"]) == {"ids": []}
    r = search.execute_search(_job("none"), KEYS, ("claude-code", "cursor"))
    assert json.loads(r["result_text"]) == {"ids": ["cursor:d"]}


def test_limit_100(monkeypatch):
    many = [{"id": f"claude-code:{i}", "title": "hit", "last_message_preview": "", "last_updated_at": f"{i:05d}"} for i in range(150)]
    monkeypatch.setattr("agent.claude_code_source.list_claude_code_sessions", lambda: many)
    monkeypatch.setattr("agent.claude_code_source.get_full_messages", lambda raw: [])
    ids = json.loads(search.execute_search(_job("hit"), KEYS, ("claude-code",))["result_text"])["ids"]
    assert len(ids) == 100 and ids[0] == "claude-code:149"


def test_empty_or_undecryptable_query_fails(sources):
    assert search.execute_search(_job("   "), KEYS)["status"] == "failed"
    bad = {"id": "s", "payload": json.dumps({"query": e2e.encrypt_text(KEYS, "x", "other-aad")})}
    assert search.execute_search(bad, KEYS)["status"] == "failed"
    assert search.execute_search({"id": "s", "payload": "{}"}, KEYS)["status"] == "failed"


def test_plaintext_mode_rejects_search(sources):
    r = search.execute_search(_job("login"), None)
    assert r == {"status": "failed", "result_text": "search jobs require E2E mode", "messages": [], "is_complete": False}
