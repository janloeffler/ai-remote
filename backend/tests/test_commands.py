import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def logged_in_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("API_KEY", "test-api-key-1234567890-abcdefgh")
    monkeypatch.setenv("SECRET_KEY", "test-secret-value-1234567890abcd")
    monkeypatch.setenv("SESSION_COOKIE_HTTPS_ONLY", "false")
    monkeypatch.setenv("AI_REMOTE_ALLOWED_PROJECTS", "/Users/jan/source/demo")
    from app.main import app
    from app import settings

    monkeypatch.setattr(settings, "ALLOWED_PROJECTS", ["/Users/jan/source/demo"])

    with TestClient(app, headers={"Accept-Language": "de"}) as test_client:
        # follow_redirects=False: logging in redirects to "/", and following that would
        # make the fixture itself the first authenticated page view — bumping the active
        # window before the test body runs anything. Authentication is setup, not activity.
        test_client.post(
            "/login", data={"api_key": "test-api-key-1234567890-abcdefgh"}, follow_redirects=False
        )
        yield test_client


@pytest.fixture
def logged_in_client_no_prior_login(tmp_path, monkeypatch):
    # Same setup as logged_in_client, but the fixture itself does not log in yet —
    # for tests that need to observe state around the login request itself.
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("API_KEY", "test-api-key-1234567890-abcdefgh")
    monkeypatch.setenv("SECRET_KEY", "test-secret-value-1234567890abcd")
    monkeypatch.setenv("SESSION_COOKIE_HTTPS_ONLY", "false")
    monkeypatch.setenv("AI_REMOTE_ALLOWED_PROJECTS", "/Users/jan/source/demo")
    from app.main import app
    from app import settings

    monkeypatch.setattr(settings, "ALLOWED_PROJECTS", ["/Users/jan/source/demo"])

    with TestClient(app, headers={"Accept-Language": "de"}) as test_client:
        yield test_client


def _sync_session(client, project_path="/Users/jan/source/demo", session_id="claude-code:abc"):
    session = {
        "id": session_id,
        "tool": "claude-code",
        "entrypoint": "cli",
        "project_path": project_path,
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


def test_command_creates_job_for_allowlisted_session(logged_in_client):
    _sync_session(logged_in_client)
    response = logged_in_client.post("/chats/claude-code:abc/command", json={"prompt": "keep going"})
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    pending = logged_in_client.get(
        "/jobs/pending", headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"}
    ).json()["jobs"]
    assert pending[0]["id"] == job_id
    assert pending[0]["type"] == "resume_message"
    assert pending[0]["target"] == "claude-code:abc"
    assert '"prompt": "keep going"' in pending[0]["payload"]


def test_command_404_for_unknown_session(logged_in_client):
    response = logged_in_client.post("/chats/does-not-exist/command", json={"prompt": "hi"})
    assert response.status_code == 404


def test_command_response_includes_eta_seconds(logged_in_client):
    _sync_session(logged_in_client)
    response = logged_in_client.post("/chats/claude-code:abc/command", json={"prompt": "keep going"})
    assert response.status_code == 200
    assert response.json()["eta_seconds"] == 60  # ~60s remaining since agent contact was just recorded microseconds ago by _sync_session


def test_command_second_call_reflects_active_interval(logged_in_client):
    _sync_session(logged_in_client)
    first = logged_in_client.post("/chats/claude-code:abc/command", json={"prompt": "keep going"})
    assert first.json()["eta_seconds"] == 60  # first action ever: pre-bump default interval still applies

    second = logged_in_client.post("/chats/claude-code:abc/command", json={"prompt": "again"})
    assert second.json()["eta_seconds"] == 10  # inside the window the first action just opened


def test_jobs_pending_reports_default_poll_interval_when_idle(logged_in_client):
    pending = logged_in_client.get(
        "/jobs/pending", headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"}
    ).json()
    assert pending["poll_interval_seconds"] == 60


def test_jobs_pending_reports_active_poll_interval_after_action(logged_in_client):
    _sync_session(logged_in_client)
    logged_in_client.post("/chats/claude-code:abc/command", json={"prompt": "go"})

    pending = logged_in_client.get(
        "/jobs/pending", headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"}
    ).json()
    assert pending["poll_interval_seconds"] == 10


def test_command_403_when_project_not_allowlisted(logged_in_client):
    _sync_session(logged_in_client, project_path="/Users/jan/source/not-allowed", session_id="claude-code:xyz")
    response = logged_in_client.post("/chats/claude-code:xyz/command", json={"prompt": "hi"})
    assert response.status_code == 403


def test_command_409_when_paused(logged_in_client):
    from app import db

    _sync_session(logged_in_client)
    import os

    conn = db.get_connection(os.environ["DATABASE_PATH"])
    db.set_remote_commands_paused(conn, True)
    conn.close()

    response = logged_in_client.post("/chats/claude-code:abc/command", json={"prompt": "hi"})
    assert response.status_code == 409


def test_new_session_form_lists_allowed_projects(logged_in_client):
    response = logged_in_client.get("/projects/new")
    assert response.status_code == 200
    assert "/Users/jan/source/demo" in response.text


@pytest.fixture
def logged_in_client_no_allowed_projects(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("API_KEY", "test-api-key-1234567890-abcdefgh")
    monkeypatch.setenv("SECRET_KEY", "test-secret-value-1234567890abcd")
    monkeypatch.setenv("SESSION_COOKIE_HTTPS_ONLY", "false")
    monkeypatch.setenv("AI_REMOTE_ALLOWED_PROJECTS", "")
    from app.main import app
    from app import settings

    monkeypatch.setattr(settings, "ALLOWED_PROJECTS", [])

    with TestClient(app, headers={"Accept-Language": "de"}) as test_client:
        test_client.post("/login", data={"api_key": "test-api-key-1234567890-abcdefgh"})
        yield test_client


def test_new_session_form_shows_message_when_no_allowed_projects(logged_in_client_no_allowed_projects):
    response = logged_in_client_no_allowed_projects.get("/projects/new")
    assert response.status_code == 200
    assert "Keine Projekte in der Allow-List konfiguriert." in response.text
    assert 'id="new-session-form"' not in response.text


def test_new_session_form_renders_single_dropdown_form(logged_in_client):
    response = logged_in_client.get("/projects/new")
    assert response.status_code == 200
    assert response.text.count('id="new-session-form"') == 1
    assert '<select id="project-select" name="project_path">' in response.text
    assert '<option value="/Users/jan/source/demo">' in response.text
    # no more one <form> per project, no more per-project data-project-path forms
    assert "data-project-path" not in response.text


def test_new_session_command_creates_job(logged_in_client):
    response = logged_in_client.post(
        "/projects/command",
        json={"project_path": "/Users/jan/source/demo", "tool": "claude-code", "prompt": "start fresh"},
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    pending = logged_in_client.get(
        "/jobs/pending", headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"}
    ).json()["jobs"]
    assert pending[0]["id"] == job_id
    assert pending[0]["type"] == "new_session"
    assert pending[0]["target"] == "/Users/jan/source/demo"
    assert '"tool": "claude-code"' in pending[0]["payload"]


def test_new_session_command_response_includes_eta_seconds(logged_in_client):
    response = logged_in_client.post(
        "/projects/command",
        json={"project_path": "/Users/jan/source/demo", "tool": "claude-code", "prompt": "hi"},
    )
    assert response.status_code == 200
    assert response.json()["eta_seconds"] == 60  # default AI_REMOTE_INTERVAL_SECONDS, no prior contact


def test_new_session_command_eta_seconds_reflects_recent_agent_contact(logged_in_client):
    import os
    from datetime import datetime, timedelta, timezone
    from app import db

    db_path = os.environ["DATABASE_PATH"]
    conn = db.get_connection(db_path)
    recent = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    conn.execute("UPDATE agent_status SET last_contact_at = ? WHERE id = 1", (recent,))
    conn.commit()
    conn.close()

    response = logged_in_client.post(
        "/projects/command",
        json={"project_path": "/Users/jan/source/demo", "tool": "claude-code", "prompt": "hi"},
    )
    eta = response.json()["eta_seconds"]
    assert 45 <= eta <= 50  # ~50s remaining out of the 60s default interval, allow test jitter


def test_new_session_command_second_call_reflects_active_interval(logged_in_client):
    first = logged_in_client.post(
        "/projects/command",
        json={"project_path": "/Users/jan/source/demo", "tool": "claude-code", "prompt": "hi"},
    )
    assert first.json()["eta_seconds"] == 60

    second = logged_in_client.post(
        "/projects/command",
        json={"project_path": "/Users/jan/source/demo", "tool": "claude-code", "prompt": "again"},
    )
    assert second.json()["eta_seconds"] == 10


def test_new_session_command_403_when_not_allowlisted(logged_in_client):
    response = logged_in_client.post(
        "/projects/command",
        json={"project_path": "/Users/jan/source/not-allowed", "tool": "claude-code", "prompt": "hi"},
    )
    assert response.status_code == 403


def test_new_session_command_works_even_with_zero_prior_sessions(logged_in_client):
    # No /sync/index call for this path at all — allow-list membership must be
    # the only requirement, independent of whether anything was ever synced from it.
    response = logged_in_client.post(
        "/projects/command",
        json={"project_path": "/Users/jan/source/demo", "tool": "cursor", "prompt": "hi"},
    )
    assert response.status_code == 200


def test_detail_page_shows_composer_for_allowlisted_session(logged_in_client):
    _sync_session(logged_in_client)
    response = logged_in_client.get("/chats/claude-code:abc")
    assert 'id="command-form"' in response.text


def test_detail_page_hides_composer_for_non_allowlisted_session(logged_in_client):
    _sync_session(logged_in_client, project_path="/Users/jan/source/not-allowed", session_id="claude-code:xyz")
    response = logged_in_client.get("/chats/claude-code:xyz")
    assert 'id="command-form"' not in response.text


def test_list_page_shows_default_poll_mode_when_idle(logged_in_client):
    response = logged_in_client.get("/")
    assert "Standard (60s)" in response.text


def test_list_page_shows_active_poll_mode_after_action(logged_in_client):
    _sync_session(logged_in_client)
    logged_in_client.post("/chats/claude-code:abc/command", json={"prompt": "go"})

    response = logged_in_client.get("/")
    assert "Aktiv (10s)" in response.text


def test_plain_navigation_alone_activates_fast_polling_for_next_request(logged_in_client):
    # No job-creating action anywhere here — just viewing pages. The active window
    # should still open, so the next request already sees the fast interval.
    logged_in_client.get("/")
    response = logged_in_client.get("/")
    assert "Aktiv (10s)" in response.text


def test_login_itself_does_not_activate_fast_polling(logged_in_client_no_prior_login):
    # The login POST is the request that establishes the session; it must not count
    # as an "authenticated action" yet, or a fresh login would falsely read as active.
    logged_in_client_no_prior_login.post(
        "/login", data={"api_key": "test-api-key-1234567890-abcdefgh"}, follow_redirects=False
    )
    response = logged_in_client_no_prior_login.get("/")
    assert "Standard (60s)" in response.text


# SEC-014: nothing bounded the prompt, so an authenticated client could grow the SQLite
# database and its FTS index without limit (and hand the agent an argv of any size).
def test_command_prompt_over_the_limit_is_rejected(logged_in_client):
    from app.models import PROMPT_MAX_LENGTH

    _sync_session(logged_in_client)
    response = logged_in_client.post(
        "/chats/claude-code:abc/command", json={"prompt": "x" * (PROMPT_MAX_LENGTH + 1)}
    )
    assert response.status_code == 422


def test_new_session_prompt_over_the_limit_is_rejected(logged_in_client):
    from app.models import PROMPT_MAX_LENGTH

    response = logged_in_client.post(
        "/projects/command",
        json={
            "project_path": "/Users/jan/source/demo",
            "tool": "claude-code",
            "prompt": "x" * (PROMPT_MAX_LENGTH + 1),
        },
    )
    assert response.status_code == 422


def test_prompt_at_the_limit_is_accepted(logged_in_client):
    from app.models import PROMPT_MAX_LENGTH

    _sync_session(logged_in_client)
    response = logged_in_client.post(
        "/chats/claude-code:abc/command", json={"prompt": "x" * PROMPT_MAX_LENGTH}
    )
    assert response.status_code == 200


def test_disabled_tool_hidden_and_rejected(logged_in_client, monkeypatch):
    from app import db, settings

    monkeypatch.setattr(settings, "ENABLED_TOOLS", ("claude-code",))
    monkeypatch.setattr(settings, "DEFAULT_TOOL", "claude-code")
    resp = logged_in_client.post(
        "/projects/command",
        json={"project_path": "/Users/jan/source/demo", "tool": "cursor", "prompt": "hi"},
    )
    assert resp.status_code == 403
    page = logged_in_client.get("/projects/new")
    assert 'value="claude-code"' in page.text and "Cursor" not in page.text
    assert "Cursor" not in logged_in_client.get("/").text


def test_all_tools_disabled_shows_error(logged_in_client, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "ENABLED_TOOLS", ())
    monkeypatch.setattr(settings, "DEFAULT_TOOL", None)
    assert "CLAUDE_CODE_ENABLED" in logged_in_client.get("/").text
    assert "CLAUDE_CODE_ENABLED" in logged_in_client.get("/projects/new").text
