import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def logged_in_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("API_KEY", "test-api-key-1234567890-abcdefgh")
    monkeypatch.setenv("SECRET_KEY", "test-secret-value-1234567890abcd")
    monkeypatch.setenv("SESSION_COOKIE_HTTPS_ONLY", "false")
    from app.main import app

    with TestClient(app, headers={"Accept-Language": "de"}) as test_client:
        # follow_redirects=False: logging in redirects to "/", and following that would
        # make the fixture itself the first authenticated page view — bumping the active
        # window before the test body runs anything. Authentication is setup, not activity.
        test_client.post(
            "/login", data={"api_key": "test-api-key-1234567890-abcdefgh"}, follow_redirects=False
        )
        yield test_client


def test_login_with_wrong_key_shows_error(logged_in_client):
    response = logged_in_client.post("/login", data={"api_key": "wrong"}, headers={"Accept-Language": "en"})
    assert response.status_code == 401
    assert "Invalid" in response.text
    german = logged_in_client.post("/login", data={"api_key": "wrong"})
    assert "Ungültiger Schlüssel" in german.text


def test_list_page_shows_synced_session_and_respects_tool_filter(logged_in_client):
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
    logged_in_client.post(
        "/sync/index",
        json={"sessions": [session]},
        headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"},
    )

    response = logged_in_client.get("/")
    assert "Demo session" in response.text

    filtered = logged_in_client.get("/", params={"tool": "cursor"})
    assert "Demo session" not in filtered.text


def test_list_page_shows_staleness_banner(logged_in_client):
    never = logged_in_client.get("/")
    assert "noch nie erreichbar" in never.text

    logged_in_client.post(
        "/sync/index", json={"sessions": []}, headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"}
    )
    after_contact = logged_in_client.get("/")
    assert "zuletzt erreichbar" in after_contact.text


def test_list_page_preview_is_escaped_not_rendered_as_html(logged_in_client):
    session = {
        "id": "claude-code:xss",
        "tool": "claude-code",
        "entrypoint": "cli",
        "project_path": "/Users/jan/source/demo",
        "title": "XSS session",
        "created_at": "2026-08-01T10:00:00Z",
        "last_updated_at": "2026-08-01T10:05:00Z",
        "message_count": 1,
        "last_message_preview": "<script>alert(1)</script>",
        "status": "idle",
    }
    logged_in_client.post(
        "/sync/index",
        json={"sessions": [session]},
        headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"},
    )

    response = logged_in_client.get("/")
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text


def test_list_page_sort_by_title(logged_in_client):
    # Deliberately diverging: "Zebra" is most recent (would win date_desc) but
    # alphabetically last (must lose under title_asc) — proves sort=title_asc
    # is actually applied, not coincidentally matching the default date order.
    for title, ts in [("Zebra chat", "2026-08-02T10:00:00Z"), ("Apple chat", "2026-08-01T10:00:00Z")]:
        session = {
            "id": f"claude-code:{title}",
            "tool": "claude-code",
            "entrypoint": "cli",
            "project_path": "/Users/jan/source/demo",
            "title": title,
            "created_at": ts,
            "last_updated_at": ts,
            "message_count": 1,
            "last_message_preview": "hi",
            "status": "idle",
        }
        logged_in_client.post(
            "/sync/index", json={"sessions": [session]}, headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"}
        )

    default_order = logged_in_client.get("/")
    assert default_order.text.index("Zebra chat") < default_order.text.index("Apple chat")  # date_desc default

    title_order = logged_in_client.get("/", params={"sort": "title_asc"})
    assert title_order.text.index("Apple chat") < title_order.text.index("Zebra chat")  # title_asc override


def test_expand_home_dir_maps_tilde_to_local_home_dir():
    from app.main import _expand_home_dir
    from app import settings

    assert _expand_home_dir("~/source/demo") == f"{settings.LOCAL_HOME_DIR}/source/demo"
    assert _expand_home_dir("~") == settings.LOCAL_HOME_DIR
    assert _expand_home_dir("/already/absolute") == "/already/absolute"


def test_project_filter_matches_via_home_dir_expansion(logged_in_client, monkeypatch):
    from app import settings

    session = {
        "id": "claude-code:home",
        "tool": "claude-code",
        "entrypoint": "cli",
        "project_path": f"{settings.LOCAL_HOME_DIR}/source/demo",
        "title": "Home dir session",
        "created_at": "2026-08-01T10:00:00Z",
        "last_updated_at": "2026-08-01T10:05:00Z",
        "message_count": 1,
        "last_message_preview": "hi",
        "status": "idle",
    }
    logged_in_client.post(
        "/sync/index", json={"sessions": [session]}, headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"}
    )
    response = logged_in_client.get("/", params={"project": "~/source"})
    assert "Home dir session" in response.text


def test_list_page_shows_sort_dropdown_and_combobox_markup(logged_in_client):
    response = logged_in_client.get("/")
    assert 'name="sort"' in response.text
    assert 'id="project-input"' in response.text
    assert 'id="project-listbox"' in response.text
    assert 'id="project-paths-data"' in response.text


def test_list_page_embeds_real_synced_project_paths_in_json_data(logged_in_client):
    distinctive_path = "/Users/jan/source/distinctive-project-name-xyz"
    session = {
        "id": "claude-code:distinctive",
        "tool": "claude-code",
        "entrypoint": "cli",
        "project_path": distinctive_path,
        "title": "Distinctive session",
        "created_at": "2026-08-01T10:00:00Z",
        "last_updated_at": "2026-08-01T10:05:00Z",
        "message_count": 1,
        "last_message_preview": "hi",
        "status": "idle",
    }
    logged_in_client.post(
        "/sync/index", json={"sessions": [session]}, headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"}
    )

    response = logged_in_client.get("/")
    script_start = response.text.index('id="project-paths-data"')
    script_end = response.text.index("</script>", script_start)
    script_content = response.text[script_start:script_end]
    assert distinctive_path in script_content


def test_pause_toggle_shows_banner_on_list_page(logged_in_client):
    before = logged_in_client.get("/")
    assert 'class="paused-banner"' not in before.text

    toggle = logged_in_client.post(
        "/settings/pause-remote-commands", data={"paused": "true"}, follow_redirects=False
    )
    assert toggle.status_code == 303

    after = logged_in_client.get("/")
    assert 'class="paused-banner"' in after.text

    logged_in_client.post("/settings/pause-remote-commands", data={"paused": "false"})
    resumed = logged_in_client.get("/")
    assert 'class="paused-banner"' not in resumed.text
