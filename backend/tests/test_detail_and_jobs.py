import pytest
from fastapi.testclient import TestClient
from app.markdown_filter import render_markdown
from app import db


def test_compute_eta_seconds_no_prior_contact_returns_full_interval(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key-1234567890-abcdefgh")
    monkeypatch.setenv("SECRET_KEY", "test-secret-value-1234567890abcd")
    monkeypatch.setenv("SESSION_COOKIE_HTTPS_ONLY", "false")
    from app.main import _compute_eta_seconds

    assert _compute_eta_seconds(None, 60) == 60


def test_compute_eta_seconds_recent_contact_returns_remaining_time(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key-1234567890-abcdefgh")
    monkeypatch.setenv("SECRET_KEY", "test-secret-value-1234567890abcd")
    monkeypatch.setenv("SESSION_COOKIE_HTTPS_ONLY", "false")
    from datetime import datetime, timedelta, timezone
    from app.main import _compute_eta_seconds

    last_contact = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    eta = _compute_eta_seconds(last_contact, 60)
    assert 48 <= eta <= 50  # ~50s of a 60s interval remain; allow a little test jitter


def test_compute_eta_seconds_overdue_contact_clamps_to_zero(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key-1234567890-abcdefgh")
    monkeypatch.setenv("SECRET_KEY", "test-secret-value-1234567890abcd")
    monkeypatch.setenv("SESSION_COOKIE_HTTPS_ONLY", "false")
    from datetime import datetime, timedelta, timezone
    from app.main import _compute_eta_seconds

    last_contact = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    assert _compute_eta_seconds(last_contact, 60) == 0


@pytest.fixture
def logged_in_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("API_KEY", "test-api-key-1234567890-abcdefgh")
    monkeypatch.setenv("SECRET_KEY", "test-secret-value-1234567890abcd")
    monkeypatch.setenv("SESSION_COOKIE_HTTPS_ONLY", "false")
    from app.main import app

    with TestClient(app) as test_client:
        # follow_redirects=False: logging in redirects to "/", and following that would
        # make the fixture itself the first authenticated page view — bumping the active
        # window before the test body runs anything. Authentication is setup, not activity.
        test_client.post(
            "/login", data={"api_key": "test-api-key-1234567890-abcdefgh"}, follow_redirects=False
        )
        yield test_client


def _sync_one(client):
    session = {
        "id": "claude-code:abc",
        "tool": "claude-code",
        "entrypoint": "cli",
        "project_path": "/Users/jan/source/demo",
        "title": "Demo session",
        "created_at": "2026-08-01T10:00:00Z",
        "last_updated_at": "2026-08-01T10:05:00Z",
        "message_count": 1,
        "last_message_preview": "hi there",
        "status": "idle",
    }
    client.post(
        "/sync/index",
        json={"sessions": [session]},
        headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"},
    )


def test_detail_page_shows_preview_when_not_fully_synced(logged_in_client):
    _sync_one(logged_in_client)
    response = logged_in_client.get("/chats/claude-code:abc")
    assert response.status_code == 200
    assert "hi there" in response.text


def test_detail_page_404_for_unknown_session(logged_in_client):
    response = logged_in_client.get("/chats/does-not-exist")
    assert response.status_code == 404


def test_render_markdown_sanitizes_xss():
    html = render_markdown("hi <script>alert(1)</script> there")
    assert "<script>" not in html
    assert "</script>" not in html
    assert "hi" in html
    assert "there" in html


def test_render_markdown_preserves_legitimate_formatting():
    html = render_markdown("**bold** and `code`")
    assert "<strong>bold</strong>" in html
    assert "<code>code</code>" in html


def test_detail_page_shows_full_content_when_synced(logged_in_client, tmp_path, monkeypatch):
    import os
    db_path = os.environ["DATABASE_PATH"]

    _sync_one(logged_in_client)

    conn = db.get_connection(db_path)
    messages = [
        {
            "idx": 0,
            "role": "user",
            "timestamp": "2026-08-01T10:00:00Z",
            "content": "Hello assistant",
        },
        {
            "idx": 1,
            "role": "assistant",
            "timestamp": "2026-08-01T10:01:00Z",
            "content": "Hi user, **how can I help?**",
        },
    ]
    db.replace_messages(conn, "claude-code:abc", messages)
    conn.close()

    response = logged_in_client.get("/chats/claude-code:abc")
    assert response.status_code == 200
    assert "Hello assistant" in response.text
    assert "Hi user" in response.text


def test_fetch_full_404s_for_unknown_session(logged_in_client):
    """SEC-005: the queued job's target is handed to the agent and used to build a
    filesystem path, so unknown ids must not become jobs."""
    response = logged_in_client.post("/chats/claude-code:never-synced/fetch-full")
    assert response.status_code == 404

    pending = logged_in_client.get(
        "/jobs/pending", headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"}
    ).json()["jobs"]
    assert pending == []


def test_fetch_full_job_round_trip(logged_in_client):
    _sync_one(logged_in_client)

    enqueue = logged_in_client.post("/chats/claude-code:abc/fetch-full")
    assert enqueue.status_code == 200
    job_id = enqueue.json()["job_id"]

    pending = logged_in_client.get(
        "/jobs/pending", headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"}
    ).json()["jobs"]
    assert pending[0]["id"] == job_id
    assert pending[0]["target"] == "claude-code:abc"

    complete = logged_in_client.post(
        f"/jobs/{job_id}/complete",
        json={
            "status": "done",
            "result_text": "",
            "messages": [
                {"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": "hi"},
                {"idx": 1, "role": "assistant", "timestamp": "2026-08-01T10:01:00Z", "content": "hello back"},
            ],
        },
        headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"},
    )
    assert complete.status_code == 200

    status = logged_in_client.get(f"/chats/claude-code:abc/status?job_id={job_id}")
    assert status.json() == {"status": "done"}

    detail = logged_in_client.get("/chats/claude-code:abc")
    assert "hello back" in detail.text


def test_fetch_full_default_creates_count_based_payload_job(logged_in_client):
    _sync_one(logged_in_client)

    enqueue = logged_in_client.post("/chats/claude-code:abc/fetch-full")
    job_id = enqueue.json()["job_id"]

    pending = logged_in_client.get(
        "/jobs/pending", headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"}
    ).json()["jobs"]
    assert pending[0]["id"] == job_id
    import json as json_module

    payload = json_module.loads(pending[0]["payload"])
    assert payload == {"count": 10}  # loaded_message_count(0) + default CHAT_HISTORY_PAGE_SIZE(10)


def test_fetch_full_with_full_true_creates_empty_payload_job(logged_in_client):
    _sync_one(logged_in_client)

    enqueue = logged_in_client.post("/chats/claude-code:abc/fetch-full", params={"full": "true"})
    job_id = enqueue.json()["job_id"]

    pending = logged_in_client.get(
        "/jobs/pending", headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"}
    ).json()["jobs"]
    assert pending[0]["id"] == job_id
    assert pending[0]["payload"] == ""


def test_detail_page_shows_partial_messages_and_load_more_button(logged_in_client):
    import os

    _sync_one(logged_in_client)
    db_path = os.environ["DATABASE_PATH"]
    conn = db.get_connection(db_path)
    db.upsert_session(
        conn,
        {
            "id": "claude-code:abc",
            "tool": "claude-code",
            "entrypoint": "cli",
            "project_path": "/Users/jan/source/demo",
            "title": "Demo session",
            "created_at": "2026-08-01T10:00:00Z",
            "last_updated_at": "2026-08-01T10:05:00Z",
            "message_count": 5,
            "last_message_preview": "hi there",
            "status": "idle",
        },
    )
    db.replace_messages(
        conn,
        "claude-code:abc",
        [{"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": "partial one"}],
    )
    conn.close()

    response = logged_in_client.get("/chats/claude-code:abc")
    assert "partial one" in response.text
    assert 'id="load-more"' in response.text
    assert 'id="load-all"' in response.text


def test_fetch_full_job_completion_with_is_complete_marks_fully_synced_via_http(logged_in_client):
    # Reproduces the real bug found via end-to-end testing against real data: a session
    # can have message_count higher than the number of messages that will ever actually
    # exist (tool-only turns have no text and are never returned by get_full_messages).
    # The full HTTP completion path must trust the agent's is_complete flag directly,
    # not a loaded-vs-message_count comparison, or the UI's pagination buttons never
    # disappear even after genuinely loading everything available.
    session = {
        "id": "claude-code:toolheavy",
        "tool": "claude-code",
        "entrypoint": "cli",
        "project_path": "/Users/jan/source/demo",
        "title": "Tool-heavy session",
        "created_at": "2026-08-01T10:00:00Z",
        "last_updated_at": "2026-08-01T10:05:00Z",
        "message_count": 8,  # inflated: counts tool-only turns with no text
        "last_message_preview": "hi",
        "status": "idle",
    }
    logged_in_client.post(
        "/sync/index", json={"sessions": [session]}, headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"}
    )

    enqueue = logged_in_client.post("/chats/claude-code:toolheavy/fetch-full", params={"full": "true"})
    job_id = enqueue.json()["job_id"]

    complete = logged_in_client.post(
        f"/jobs/{job_id}/complete",
        json={
            "status": "done",
            "result_text": "",
            "is_complete": True,  # the agent's ground truth: only 5 text-bearing messages ever existed
            "messages": [
                {"idx": i, "role": "user", "timestamp": f"2026-08-01T10:0{i}:00Z", "content": str(i)}
                for i in range(5)
            ],
        },
        headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"},
    )
    assert complete.status_code == 200

    detail = logged_in_client.get("/chats/claude-code:toolheavy")
    # Both buttons must be gone now — everything available was genuinely loaded,
    # even though loaded_message_count (5) never reaches message_count (8).
    assert 'id="load-more"' not in detail.text
    assert 'id="load-all"' not in detail.text


def test_detail_page_hides_buttons_once_fully_loaded(logged_in_client):
    import os

    _sync_one(logged_in_client)
    db_path = os.environ["DATABASE_PATH"]
    conn = db.get_connection(db_path)
    db.replace_messages(
        conn,
        "claude-code:abc",
        [{"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": "the only message"}],
    )  # _sync_one's message_count is 1, so loading 1 message here means fully loaded
    conn.close()

    response = logged_in_client.get("/chats/claude-code:abc")
    assert "the only message" in response.text
    assert 'id="load-more"' not in response.text
    assert 'id="load-all"' not in response.text


def test_detail_page_with_zero_message_count_and_partial_load_still_shows_buttons(logged_in_client):
    import os

    session = {
        "id": "cursor:zerocount",
        "tool": "cursor",
        "entrypoint": "cursor",
        "project_path": "/Users/jan/source/demo",
        "title": "Zero message_count session",
        "created_at": "2026-08-01T10:00:00Z",
        "last_updated_at": "2026-08-01T10:05:00Z",
        "message_count": 0,  # real-world case: Cursor's enrich_with_messages leaves this at 0 for some sessions
        "last_message_preview": "",
        "status": "idle",
    }
    logged_in_client.post(
        "/sync/index", json={"sessions": [session]}, headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"}
    )

    db_path = os.environ["DATABASE_PATH"]
    conn = db.get_connection(db_path)
    messages = [
        {"idx": i, "role": "user", "timestamp": f"2026-08-01T10:0{i}:00Z", "content": str(i)}
        for i in range(10)
    ]
    db.replace_messages(conn, "cursor:zerocount", messages, is_complete=False)
    conn.close()

    response = logged_in_client.get("/chats/cursor:zerocount")
    assert 'id="load-more"' in response.text
    assert 'id="load-all"' in response.text


def test_generic_job_status_endpoint(logged_in_client):
    _sync_one(logged_in_client)
    enqueue = logged_in_client.post("/chats/claude-code:abc/fetch-full", params={"full": "true"})
    job_id = enqueue.json()["job_id"]

    response = logged_in_client.get(f"/jobs/{job_id}/status")
    assert response.status_code == 200
    assert response.json()["status"] in ("pending", "running")


def test_generic_job_status_unknown_job(logged_in_client):
    response = logged_in_client.get("/jobs/does-not-exist/status")
    assert response.json() == {"status": "unknown", "result_text": None}


def test_jobs_audit_page_lists_jobs_newest_first(logged_in_client):
    _sync_one(logged_in_client)
    logged_in_client.post("/chats/claude-code:abc/fetch-full", params={"full": "true"})

    response = logged_in_client.get("/jobs")
    assert response.status_code == 200
    assert "fetch_full" in response.text
    assert "claude-code:abc" in response.text


def test_fetch_full_response_includes_eta_seconds_when_agent_never_contacted(logged_in_client):
    _sync_one(logged_in_client)

    enqueue = logged_in_client.post("/chats/claude-code:abc/fetch-full")
    assert enqueue.json()["eta_seconds"] == 60  # default AI_REMOTE_INTERVAL_SECONDS, no prior contact


def test_fetch_full_second_call_reflects_active_interval(logged_in_client):
    _sync_one(logged_in_client)
    first = logged_in_client.post("/chats/claude-code:abc/fetch-full")
    assert first.json()["eta_seconds"] == 60

    second = logged_in_client.post("/chats/claude-code:abc/fetch-full")
    assert second.json()["eta_seconds"] == 10


def test_fetch_full_response_eta_seconds_reflects_recent_agent_contact(logged_in_client):
    import os
    from datetime import datetime, timedelta, timezone

    _sync_one(logged_in_client)
    db_path = os.environ["DATABASE_PATH"]
    conn = db.get_connection(db_path)
    recent = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    conn.execute("UPDATE agent_status SET last_contact_at = ? WHERE id = 1", (recent,))
    conn.commit()
    conn.close()

    enqueue = logged_in_client.post("/chats/claude-code:abc/fetch-full")
    eta = enqueue.json()["eta_seconds"]
    assert 45 <= eta <= 50  # ~50s remaining out of the 60s default interval, allow test jitter


def test_fetch_full_response_eta_seconds_clamps_to_zero_when_agent_overdue(logged_in_client):
    import os
    from datetime import datetime, timedelta, timezone

    _sync_one(logged_in_client)
    db_path = os.environ["DATABASE_PATH"]
    conn = db.get_connection(db_path)
    overdue = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    conn.execute("UPDATE agent_status SET last_contact_at = ? WHERE id = 1", (overdue,))
    conn.commit()
    conn.close()

    enqueue = logged_in_client.post("/chats/claude-code:abc/fetch-full")
    assert enqueue.json()["eta_seconds"] == 0
