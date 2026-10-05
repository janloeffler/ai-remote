import json

import httpx

from agent import uploader


def test_push_sync_returns_true_on_success():
    def handler(request):
        assert request.headers["authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {"sessions": [{"id": "a"}]}
        return httpx.Response(200, json={"received": 1})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = uploader.push_sync("http://backend.example", "test-key", [{"id": "a"}], client=client)
    assert result is True


def test_push_sync_returns_false_on_http_error():
    def handler(request):
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = uploader.push_sync("http://backend.example", "test-key", [{"id": "a"}], client=client)
    assert result is False


def test_push_sync_skips_request_when_no_deltas():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    result = uploader.push_sync("http://backend.example", "test-key", [], client=client)
    assert result is True


from agent import executor, jobs


def test_fetch_pending_jobs_parses_response():
    def handler(request):
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "jobs": [{"id": "j1", "type": "fetch_full", "target": "claude-code:abc"}],
                "poll_interval_seconds": 60,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = jobs.fetch_pending_jobs("http://backend.example", "test-key", client)
    assert result == {
        "jobs": [{"id": "j1", "type": "fetch_full", "target": "claude-code:abc"}],
        "poll_interval_seconds": 60,
    }


def test_report_job_result_sends_expected_body():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    jobs.report_job_result(
        "http://backend.example", "test-key", "j1", "done", client,
        result_text="", messages=[{"idx": 0, "role": "user", "timestamp": "t", "content": "hi"}],
    )
    assert captured["body"] == {
        "status": "done",
        "result_text": "",
        "messages": [{"idx": 0, "role": "user", "timestamp": "t", "content": "hi"}],
        "is_complete": False,
    }


def test_execute_fetch_full_dispatches_to_claude_code_source(monkeypatch):
    monkeypatch.setattr(
        "agent.claude_code_source.get_full_messages",
        lambda raw_id: [{"idx": 0, "role": "user", "timestamp": "t", "content": "hi"}],
    )
    job = {"id": "j1", "type": "fetch_full", "target": "claude-code:abc"}
    result = executor.execute_fetch_full(job)
    assert result["status"] == "done"
    assert result["messages"] == [{"idx": 0, "role": "user", "timestamp": "t", "content": "hi"}]


def test_execute_fetch_full_fails_when_no_messages_found(monkeypatch):
    monkeypatch.setattr("agent.cursor_source.get_full_messages", lambda composer_id: [])
    job = {"id": "j1", "type": "fetch_full", "target": "cursor:missing"}
    result = executor.execute_fetch_full(job)
    assert result["status"] == "failed"


def test_execute_fetch_full_fails_cleanly_on_malformed_target():
    job = {"id": "j1", "type": "fetch_full", "target": "no-colon-here"}
    result = executor.execute_fetch_full(job)
    assert result["status"] == "failed"
    assert "malformed job target" in result["result_text"]
    assert result["messages"] == []


def test_execute_fetch_full_slices_to_requested_count(monkeypatch):
    monkeypatch.setattr(
        "agent.claude_code_source.get_full_messages",
        lambda raw_id: [
            {"idx": 0, "role": "user", "timestamp": "t0", "content": "one"},
            {"idx": 1, "role": "assistant", "timestamp": "t1", "content": "two"},
            {"idx": 2, "role": "user", "timestamp": "t2", "content": "three"},
        ],
    )
    job = {"id": "j1", "type": "fetch_full", "target": "claude-code:abc", "payload": '{"count": 2}'}
    result = executor.execute_fetch_full(job)
    assert result["status"] == "done"
    assert [m["content"] for m in result["messages"]] == ["two", "three"]


def test_execute_fetch_full_returns_everything_when_payload_empty(monkeypatch):
    monkeypatch.setattr(
        "agent.claude_code_source.get_full_messages",
        lambda raw_id: [{"idx": 0, "role": "user", "timestamp": "t0", "content": "one"}],
    )
    job = {"id": "j1", "type": "fetch_full", "target": "claude-code:abc", "payload": ""}
    result = executor.execute_fetch_full(job)
    assert [m["content"] for m in result["messages"]] == ["one"]


def test_execute_fetch_full_count_larger_than_available_returns_everything(monkeypatch):
    monkeypatch.setattr(
        "agent.claude_code_source.get_full_messages",
        lambda raw_id: [{"idx": 0, "role": "user", "timestamp": "t0", "content": "only-one"}],
    )
    job = {"id": "j1", "type": "fetch_full", "target": "claude-code:abc", "payload": '{"count": 50}'}
    result = executor.execute_fetch_full(job)
    assert [m["content"] for m in result["messages"]] == ["only-one"]


def test_execute_fetch_full_malformed_payload_falls_back_to_everything(monkeypatch):
    monkeypatch.setattr(
        "agent.claude_code_source.get_full_messages",
        lambda raw_id: [{"idx": 0, "role": "user", "timestamp": "t0", "content": "one"}],
    )
    job = {"id": "j1", "type": "fetch_full", "target": "claude-code:abc", "payload": "not-json"}
    result = executor.execute_fetch_full(job)
    assert [m["content"] for m in result["messages"]] == ["one"]


def test_execute_fetch_full_slicing_preserves_original_idx_values(monkeypatch):
    monkeypatch.setattr(
        "agent.claude_code_source.get_full_messages",
        lambda raw_id: [
            {"idx": 10, "role": "user", "timestamp": "t10", "content": "a"},
            {"idx": 11, "role": "assistant", "timestamp": "t11", "content": "b"},
            {"idx": 12, "role": "user", "timestamp": "t12", "content": "c"},
        ],
    )
    job = {"id": "j1", "type": "fetch_full", "target": "claude-code:abc", "payload": '{"count": 2}'}
    result = executor.execute_fetch_full(job)
    assert [m["idx"] for m in result["messages"]] == [11, 12]


def test_execute_fetch_full_reports_is_complete_true_for_full_request(monkeypatch):
    monkeypatch.setattr(
        "agent.claude_code_source.get_full_messages",
        lambda raw_id: [{"idx": 0, "role": "user", "timestamp": "t0", "content": "one"}],
    )
    job = {"id": "j1", "type": "fetch_full", "target": "claude-code:abc", "payload": ""}
    result = executor.execute_fetch_full(job)
    assert result["is_complete"] is True


def test_execute_fetch_full_reports_is_complete_false_when_more_remains(monkeypatch):
    monkeypatch.setattr(
        "agent.claude_code_source.get_full_messages",
        lambda raw_id: [
            {"idx": 0, "role": "user", "timestamp": "t0", "content": "one"},
            {"idx": 1, "role": "assistant", "timestamp": "t1", "content": "two"},
            {"idx": 2, "role": "user", "timestamp": "t2", "content": "three"},
        ],
    )
    job = {"id": "j1", "type": "fetch_full", "target": "claude-code:abc", "payload": '{"count": 2}'}
    result = executor.execute_fetch_full(job)
    assert result["is_complete"] is False


def test_execute_fetch_full_reports_is_complete_true_when_count_covers_everything(monkeypatch):
    monkeypatch.setattr(
        "agent.claude_code_source.get_full_messages",
        lambda raw_id: [{"idx": 0, "role": "user", "timestamp": "t0", "content": "one"}],
    )
    job = {"id": "j1", "type": "fetch_full", "target": "claude-code:abc", "payload": '{"count": 50}'}
    result = executor.execute_fetch_full(job)
    assert result["is_complete"] is True
