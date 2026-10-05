import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import i18n

_KEY = "test-api-key-1234567890-abcdefgh"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("API_KEY", _KEY)
    monkeypatch.setenv("SECRET_KEY", "test-secret-value-1234567890abcd")
    monkeypatch.setenv("SESSION_COOKIE_HTTPS_ONLY", "false")
    from app.main import app

    with TestClient(app) as test_client:
        test_client.post("/login", data={"api_key": _KEY})
        yield test_client


def _agent_interval(client) -> int:
    response = client.get("/jobs/pending", headers={"Authorization": f"Bearer {_KEY}"})
    return response.json()["poll_interval_seconds"]


def _idle(client):
    """The settings/login requests above opened an active window; close it for the test."""
    from app import db
    import os

    conn = db.get_connection(os.environ["DATABASE_PATH"])
    conn.execute("UPDATE agent_status SET active_until = NULL")
    conn.commit()
    conn.close()


# --- /settings page -----------------------------------------------------------------

def test_settings_requires_a_session(client):
    client.post("/logout")
    assert client.get("/settings", follow_redirects=False).status_code == 307
    assert client.post("/settings", data={}, follow_redirects=False).status_code == 307


def test_settings_page_renders_all_sections(client):
    html = client.get("/settings").text
    for needle in ('name="language"', 'name="interval_default"', 'name="interval_active"',
                   'id="theme-switch"', 'action="/logout"'):
        assert needle in html


def test_saving_intervals_changes_what_the_agent_is_told(client):
    r = client.post("/settings", data={"language": "auto", "interval_default": "120", "interval_active": "7"},
                    follow_redirects=False)
    assert r.status_code == 303
    _idle(client)
    assert _agent_interval(client) == 120
    # the agent's own request does not open an active window, but a browser request does
    client.get("/")
    assert _agent_interval(client) == 7


def test_empty_interval_fields_restore_the_server_defaults(client):
    client.post("/settings", data={"language": "auto", "interval_default": "120", "interval_active": "7"})
    client.post("/settings", data={"language": "auto", "interval_default": "", "interval_active": ""})
    _idle(client)
    assert _agent_interval(client) == 60


@pytest.mark.parametrize("default,active", [
    ("5", "5"), ("99999", "5"), ("abc", "5"), ("60", "1"), ("60", "301"), ("60.5", "5"),
    ("20", "30"),  # active longer than standard
])
def test_invalid_intervals_are_rejected_and_not_stored(client, default, active):
    r = client.post("/settings", data={"language": "auto", "interval_default": default, "interval_active": active})
    assert r.status_code == 422
    _idle(client)
    assert _agent_interval(client) == 60


# --- language -----------------------------------------------------------------------

def test_default_language_is_english(client):
    assert "Settings" in client.get("/settings").text


def test_accept_language_picks_german(client):
    html = client.get("/settings", headers={"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}).text
    assert "Einstellungen" in html and 'lang="de"' in html


def test_unsupported_accept_language_falls_back_to_english(client):
    assert "Settings" in client.get("/settings", headers={"Accept-Language": "fr-FR"}).text


def test_explicit_language_overrides_the_browser_and_persists(client):
    r = client.post("/settings", data={"language": "de", "interval_default": "", "interval_active": ""},
                    headers={"Accept-Language": "en"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Einstellungen" in client.get("/settings", headers={"Accept-Language": "en"}).text
    client.post("/settings", data={"language": "auto", "interval_default": "", "interval_active": ""})
    assert "Settings" in client.get("/settings", headers={"Accept-Language": "en"}).text


def test_unknown_language_is_rejected(client):
    r = client.post("/settings", data={"language": "xx", "interval_default": "", "interval_active": ""})
    assert r.status_code == 422


def test_login_page_is_translated_too(client):
    client.post("/logout")
    assert "Anmelden" in client.get("/login", headers={"Accept-Language": "de"}).text
    assert "Log in" in client.get("/login", headers={"Accept-Language": "en"}).text


# --- catalog hygiene ----------------------------------------------------------------

def test_german_catalog_covers_english_keys():
    same_in_both = {"app.name", "settings.language.de", "settings.language.en"}
    assert set(i18n.EN) - set(i18n.DE) <= same_in_both
    assert set(i18n.DE) <= set(i18n.EN)


def test_placeholders_match_between_languages():
    field = re.compile(r"\{(\w+)\}")
    for key, de in i18n.DE.items():
        assert set(field.findall(de)) == set(field.findall(i18n.EN[key])), key


def test_every_key_used_in_templates_exists():
    used = set()
    for path in (Path(__file__).parent.parent / "app" / "templates").glob("*.html"):
        used |= set(re.findall(r"""\bt\(\s*['"]([\w.\- ]+)['"]""", path.read_text()))
    assert used, "no t() calls found — scan is broken"
    assert used <= set(i18n.EN), used - set(i18n.EN)
