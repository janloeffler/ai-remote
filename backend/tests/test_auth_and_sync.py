import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("API_KEY", "test-api-key-1234567890-abcdefgh")
    monkeypatch.setenv("SECRET_KEY", "test-secret-value-1234567890abcd")
    monkeypatch.setenv("SESSION_COOKIE_HTTPS_ONLY", "false")
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_agent_route_rejects_missing_key(client):
    response = client.get("/jobs/pending")
    assert response.status_code == 401


def test_agent_route_accepts_correct_key(client):
    response = client.get("/jobs/pending", headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"})
    assert response.status_code == 200


def test_browser_route_without_session_redirects_to_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_login_locks_out_after_repeated_failures(client):
    for _ in range(5):
        response = client.post("/login", data={"api_key": "wrong"})
        assert response.status_code == 401

    locked_response = client.post("/login", data={"api_key": "wrong"})
    assert locked_response.status_code == 429

    # Even the correct key is rejected while locked out — that's the point.
    still_locked = client.post("/login", data={"api_key": "test-api-key-1234567890-abcdefgh"})
    assert still_locked.status_code == 429


def test_login_throttles_rotating_x_forwarded_for_when_no_trusted_proxy_configured(client):
    """SEC-001 regression guard. X-Forwarded-For is client-supplied; trusting it
    unconditionally meant a fresh header value bought a fresh bucket, i.e. unlimited
    guesses at the one secret that gates remote code execution."""
    codes = [
        client.post("/login", data={"api_key": "wrong"}, headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code
        for i in range(10)
    ]

    assert codes[:5] == [401] * 5
    assert codes[5:] == [429] * 5


def test_client_key_ignores_forwarded_header_by_default(client):
    from app import rate_limit

    class _Req:
        headers = {"x-forwarded-for": "1.2.3.4"}
        client = type("C", (), {"host": "192.168.1.9"})()

    assert rate_limit._client_key(_Req()) == "192.168.1.9"


def _trust_proxy(monkeypatch):
    """Configure the app as if it sat behind one trusted reverse proxy. TestClient
    presents peer address 'testclient'."""
    from app import settings

    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["testclient"])


def test_client_key_honors_forwarded_header_for_configured_hop_count(monkeypatch, client):
    from app import rate_limit, settings

    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["192.168.1.9"])

    class _Req:
        headers = {"x-forwarded-for": "1.2.3.4, 172.17.0.1"}
        client = type("C", (), {"host": "192.168.1.9"})()

    assert rate_limit._client_key(_Req()) == "172.17.0.1"


def test_client_key_ignores_forwarded_header_from_an_untrusted_peer(monkeypatch, client):
    from app import rate_limit, settings

    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.9.9.9"])

    class _Req:
        headers = {"x-forwarded-for": "1.2.3.4"}
        client = type("C", (), {"host": "192.168.1.9"})()

    assert rate_limit._client_key(_Req()) == "192.168.1.9"


def test_login_lockout_is_per_client_when_a_trusted_proxy_is_configured(client, monkeypatch):
    _trust_proxy(monkeypatch)

    for _ in range(5):
        client.post("/login", data={"api_key": "wrong"}, headers={"X-Forwarded-For": "1.2.3.4"})

    blocked = client.post("/login", data={"api_key": "wrong"}, headers={"X-Forwarded-For": "1.2.3.4"})
    assert blocked.status_code == 429

    other_client = client.post(
        "/login",
        data={"api_key": "test-api-key-1234567890-abcdefgh"},
        headers={"X-Forwarded-For": "9.9.9.9"},
        follow_redirects=False,
    )
    assert other_client.status_code == 303


def test_a_flood_of_failures_never_locks_out_a_different_client(client, monkeypatch):
    """No global attempt ceiling: an earlier design capped total failures across all
    buckets, which handed anyone a repeatable way to lock the real owner out of /login
    at will. Bucket memory is bounded by eviction instead — that must not block a
    login."""
    from app import rate_limit

    _trust_proxy(monkeypatch)
    monkeypatch.setattr(rate_limit, "MAX_TRACKED_KEYS", 8)

    for i in range(200):
        client.post("/login", data={"api_key": "wrong"}, headers={"X-Forwarded-For": f"10.1.{i // 250}.{i % 250}"})

    assert len(rate_limit._failures) <= rate_limit.MAX_TRACKED_KEYS

    owner = client.post(
        "/login",
        data={"api_key": "test-api-key-1234567890-abcdefgh"},
        headers={"X-Forwarded-For": "203.0.113.7"},
        follow_redirects=False,
    )
    assert owner.status_code == 303


def test_eviction_drops_the_least_recently_active_buckets_first(monkeypatch):
    from app import rate_limit

    monkeypatch.setattr(rate_limit, "MAX_TRACKED_KEYS", 2)
    rate_limit.reset()
    rate_limit._failures.update({"old": [1.0], "middle": [2.0], "newest": [3.0]})

    rate_limit._evict_over_cap()

    assert set(rate_limit._failures) == {"middle", "newest"}


def test_failure_map_does_not_grow_without_bound(client, monkeypatch):
    """SEC-014 (memory half): expired buckets are swept globally, not only when their
    own key is touched again."""
    from app import rate_limit

    _trust_proxy(monkeypatch)

    for i in range(30):
        client.post("/login", data={"api_key": "wrong"}, headers={"X-Forwarded-For": f"10.2.0.{i}"})
    assert len(rate_limit._failures) == 30

    # Expire every bucket without sleeping: the next request must sweep all 30 away
    # rather than leave them until their own key happens to be touched again.
    monkeypatch.setattr(rate_limit, "WINDOW_SECONDS", 0.0)
    client.post("/login", data={"api_key": "wrong"}, headers={"X-Forwarded-For": "10.3.0.1"})
    assert len(rate_limit._failures) == 1


def test_non_ascii_login_is_rejected_and_counted(client):
    """SEC-012: hmac.compare_digest raises TypeError on non-ASCII str input, which
    turned any such attempt into an unauthenticated 500 that also skipped the failure
    counter."""
    from app import rate_limit

    response = client.post("/login", data={"api_key": "ü" * 20})

    assert response.status_code == 401
    assert sum(len(a) for a in rate_limit._failures.values()) == 1


def test_non_ascii_bearer_token_is_rejected_without_raising(client):
    """Not reachable over HTTP today (non-ASCII header values are refused before they
    get here), but the same compare_digest trap sits in require_api_key — assert
    directly that it returns 401 rather than blowing up."""
    from fastapi import HTTPException
    from app.auth import require_api_key

    with pytest.raises(HTTPException) as excinfo:
        require_api_key(authorization="Bearer ünicode")
    assert excinfo.value.status_code == 401


def test_login_success_clears_prior_failures(client):
    for _ in range(4):
        client.post("/login", data={"api_key": "wrong"})

    success = client.post("/login", data={"api_key": "test-api-key-1234567890-abcdefgh"}, follow_redirects=False)
    assert success.status_code == 303

    # One more wrong attempt shouldn't be locked out — success reset the count.
    response = client.post("/login", data={"api_key": "wrong"})
    assert response.status_code == 401


def test_sync_index_upserts_sessions(client):
    payload = {
        "sessions": [
            {
                "id": "claude-code:abc",
                "tool": "claude-code",
                "entrypoint": "cli",
                "project_path": "/Users/jan/source/demo",
                "title": "Demo",
                "created_at": "2026-08-01T10:00:00Z",
                "last_updated_at": "2026-08-01T10:05:00Z",
                "message_count": 1,
                "last_message_preview": "hi",
                "status": "idle",
            }
        ]
    }
    response = client.post(
        "/sync/index", json=payload, headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"}
    )
    assert response.status_code == 200
    assert response.json() == {"received": 1}


def test_sync_index_applies_recent_messages(client):
    payload = {
        "sessions": [
            {
                "id": "claude-code:abc",
                "tool": "claude-code",
                "entrypoint": "cli",
                "project_path": "/Users/jan/source/demo",
                "title": "Demo",
                "created_at": "2026-08-01T10:00:00Z",
                "last_updated_at": "2026-08-01T10:05:00Z",
                "message_count": 2,
                "last_message_preview": "hi",
                "status": "idle",
                "recent_messages": [
                    {"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": "hi"},
                    {"idx": 1, "role": "assistant", "timestamp": "2026-08-01T10:01:00Z", "content": "hello"},
                ],
            }
        ]
    }
    response = client.post(
        "/sync/index", json=payload, headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"}
    )
    assert response.status_code == 200

    from app import db as db_module
    import os

    conn = db_module.get_connection(os.environ["DATABASE_PATH"])
    session = db_module.get_session(conn, "claude-code:abc")
    messages = db_module.get_messages(conn, "claude-code:abc")
    conn.close()
    assert session["loaded_message_count"] == 2
    assert [m["content"] for m in messages] == ["hi", "hello"]


def test_sync_index_recent_messages_render_on_detail_page(client):
    payload = {
        "sessions": [
            {
                "id": "claude-code:renders",
                "tool": "claude-code",
                "entrypoint": "cli",
                "project_path": "/Users/jan/source/demo",
                "title": "Demo",
                "created_at": "2026-08-01T10:00:00Z",
                "last_updated_at": "2026-08-01T10:05:00Z",
                "message_count": 2,
                "last_message_preview": "hi",
                "status": "idle",
                "recent_messages": [
                    {"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": "preload-marker-question"},
                    {"idx": 1, "role": "assistant", "timestamp": "2026-08-01T10:01:00Z", "content": "preload-marker-answer"},
                ],
            }
        ]
    }
    client.post("/sync/index", json=payload, headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"})

    login = client.post("/login", data={"api_key": "test-api-key-1234567890-abcdefgh"}, follow_redirects=False)
    assert login.status_code == 303
    detail = client.get("/chats/claude-code:renders")
    assert detail.status_code == 200
    assert "preload-marker-question" in detail.text
    assert "preload-marker-answer" in detail.text


def test_sync_index_rejects_invalid_tool(client):
    payload = {
        "sessions": [
            {
                "id": "claude-code:abc",
                "tool": "bogus-tool",
                "entrypoint": "cli",
                "project_path": "/Users/jan/source/demo",
                "title": "Demo",
                "created_at": "2026-08-01T10:00:00Z",
                "last_updated_at": "2026-08-01T10:05:00Z",
                "message_count": 1,
                "last_message_preview": "hi",
                "status": "idle",
            }
        ]
    }
    response = client.post(
        "/sync/index", json=payload, headers={"Authorization": "Bearer test-api-key-1234567890-abcdefgh"}
    )
    assert response.status_code == 422


def test_openapi_and_docs_are_not_served(client):
    """SEC-011: the schema and interactive docs were reachable with no session."""
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code == 404, path


# --- SEC-007: logout and API_KEY-bound sessions -------------------------------------

_KEY = "test-api-key-1234567890-abcdefgh"


def _login(client):
    response = client.post("/login", data={"api_key": _KEY}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 200


def test_logout_ends_the_session(client):
    _login(client)
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 307


def test_logout_requires_post(client):
    _login(client)
    assert client.get("/logout", follow_redirects=False).status_code == 405
    assert client.get("/", follow_redirects=False).status_code == 200


def test_logout_without_a_session_is_harmless(client):
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_rotating_api_key_invalidates_existing_sessions(client, monkeypatch):
    from app import settings

    _login(client)
    monkeypatch.setattr(settings, "API_KEY", "rotated-api-key-0987654321-zyxwvuts")
    assert client.get("/", follow_redirects=False).status_code == 307


def test_session_cookie_does_not_contain_the_api_key(client):
    _login(client)
    assert _KEY not in client.cookies.get("session", "")


def test_session_without_binding_is_rejected(client):
    """A cookie minted before this change (authenticated flag only) must not survive."""
    import base64
    import json

    import itsdangerous

    signer = itsdangerous.TimestampSigner("test-secret-value-1234567890abcd")
    payload = base64.b64encode(json.dumps({"authenticated": True}).encode())
    client.cookies.set("session", signer.sign(payload).decode())
    assert client.get("/", follow_redirects=False).status_code == 307
