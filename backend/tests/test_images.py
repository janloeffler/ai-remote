import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, images
from app.markdown_filter import ImageContext, render_markdown

KEY = {"Authorization": "Bearer test-api-key-1234567890-abcdefgh"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
SID = "claude-code:abc"
PASTED = "/var/folders/x/T/pasted.png"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("API_KEY", "test-api-key-1234567890-abcdefgh")
    monkeypatch.setenv("SECRET_KEY", "test-secret-value-1234567890abcd")
    monkeypatch.setenv("SESSION_COOKIE_HTTPS_ONLY", "false")
    from app import settings
    from app.main import app

    monkeypatch.setattr(settings, "IMAGE_UPLOAD_ENABLED", True)
    with TestClient(app) as c:
        c.post("/login", data={"api_key": "test-api-key-1234567890-abcdefgh"}, follow_redirects=False)
        c.post(
            "/sync/index",
            headers=KEY,
            json={
                "sessions": [
                    {
                        "id": SID,
                        "tool": "claude-code",
                        "title": "t",
                        "created_at": "2026-08-01T10:00:00Z",
                        "last_updated_at": "2026-08-01T10:05:00Z",
                        "message_count": 1,
                        "recent_messages": [
                            {
                                "idx": 0,
                                "role": "user",
                                "timestamp": "2026-08-01T10:00:00Z",
                                "content": f"look [Image: source: {PASTED}] and ~/shots/b.png",
                            }
                        ],
                    }
                ]
            },
        )
        yield c


def _upload(client, data=PNG, path=PASTED, session_id=SID):
    return client.post(
        "/sync/image",
        headers=KEY,
        json={"session_id": session_id, "path": path, "data_b64": base64.b64encode(data).decode()},
    )


def test_upload_then_serve_with_hardening_headers(client):
    key = _upload(client).json()["key"]
    response = client.get(f"/chats/{SID}/images/{key}")
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]


def test_upload_requires_api_key(client):
    response = client.post("/sync/image", json={"session_id": SID, "path": "/a.png", "data_b64": ""})
    assert response.status_code == 401


def test_upload_rejects_non_images_and_svg(client):
    assert _upload(client, data=b"<svg onload=alert(1)>").status_code == 415
    assert _upload(client, data=b"plain text").status_code == 415


def test_upload_rejects_too_large(client):
    assert _upload(client, data=PNG + b"\x00" * (5 * 1024 * 1024)).status_code == 413


def test_upload_unknown_session_and_bad_base64(client):
    assert _upload(client, session_id="claude-code:nope").status_code == 404
    bad = client.post("/sync/image", headers=KEY, json={"session_id": SID, "path": "/a.png", "data_b64": "!!!"})
    assert bad.status_code == 400


def test_upload_disabled(client, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "IMAGE_UPLOAD_ENABLED", False)
    assert _upload(client).status_code == 403
    assert client.post(f"/chats/{SID}/fetch-image", json={"path": PASTED}).status_code == 403


def test_serve_unknown_or_malformed_key(client):
    assert client.get(f"/chats/{SID}/images/{'0' * 32}").status_code == 404
    assert client.get(f"/chats/{SID}/images/..%2F..%2Fapp.db").status_code == 404


def test_fetch_image_only_for_paths_in_the_chat(client):
    assert client.post(f"/chats/{SID}/fetch-image", json={"path": "/etc/passwd.png"}).status_code == 400
    assert client.post(f"/chats/{SID}/fetch-image", json={"path": "/etc/passwd"}).status_code == 400


def test_fetch_image_queues_job_then_reports_available(client):
    response = client.post(f"/chats/{SID}/fetch-image", json={"path": PASTED}).json()
    assert response["available"] is False
    pending = client.get("/jobs/pending", headers=KEY).json()["jobs"]
    job = next(j for j in pending if j["type"] == "fetch_image")
    assert job["target"] == SID and json.loads(job["payload"]) == {"path": PASTED}
    _upload(client)
    assert client.post(f"/chats/{SID}/fetch-image", json={"path": PASTED}).json()["available"] is True


def test_detail_page_shows_fetch_button_then_inline_image(client):
    from app import db as _db
    import os

    conn = _db.get_connection(os.environ["DATABASE_PATH"])
    _db.apply_recent_messages(conn, SID, [{"idx": 0, "role": "user", "timestamp": "t", "content": f"[Image: source: {PASTED}]"}])
    conn.close()
    page = client.get(f"/chats/{SID}").text
    assert 'class="image-fetch"' in page and "<img" not in page.split('class="messages"')[1]
    _upload(client)
    page = client.get(f"/chats/{SID}").text
    assert "/chats/claude-code%3Aabc/images/" in page and "<img" in page


def test_expired_images_are_removed_from_db_and_disk(client):
    key = _upload(client).json()["key"]
    path = images.file_path(key, "image/png")
    assert path.exists()
    import os

    conn = db.get_connection(os.environ["DATABASE_PATH"])
    old = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    conn.execute("UPDATE images SET created_at = ?", (old,))
    conn.commit()
    assert images.cleanup_expired(conn) == 1
    conn.close()
    assert not path.exists()
    assert client.get(f"/chats/{SID}/images/{key}").status_code == 404


def test_jobs_table_migration_allows_fetch_image(tmp_path):
    conn = db.get_connection(str(tmp_path / "old.db"))
    conn.executescript(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, type TEXT NOT NULL CHECK(type IN "
        "('fetch_full','resume_message','new_session')), target TEXT NOT NULL, "
        "payload TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN "
        "('pending','running','done','failed')), result_text TEXT, created_at TEXT NOT NULL, completed_at TEXT);"
        "INSERT INTO jobs (id, type, target, created_at) VALUES ('j1','fetch_full','s','2026-01-01');"
    )
    db.init_db(conn)
    assert db.get_job(conn, "j1")["target"] == "s"
    assert db.get_job(conn, db.create_job(conn, "fetch_image", "s"))["type"] == "fetch_image"


def test_markdown_marker_and_bare_path_become_buttons_but_code_blocks_are_left_alone():
    ctx = ImageContext(session_id=SID)
    html = render_markdown(f"[Image: source: {PASTED}]\n\nsee ~/shots/b.png\n\n```\n/x/y.png\n```", ctx)
    assert html.count('class="image-fetch"') == 2
    assert "/x/y.png" in html and html.count("image-fetch") < 6
    assert render_markdown("see `/x/y.png`", ctx).count("image-fetch") == 0


def test_markdown_escapes_path_in_attributes():
    ctx = ImageContext(session_id=SID)
    html = render_markdown('[Image: source: /a/"><script>x</script>.png]', ctx)
    assert "<script>" not in html



def test_marker_without_an_image_path_stays_plain_text():
    html = render_markdown("write [Image: source: …] like this", ImageContext(session_id=SID))
    assert "image-fetch" not in html


def test_unclaimed_job_fails_after_its_timeout_when_the_page_polls(client):
    job_id = client.post(f"/chats/{SID}/fetch-image", json={"path": PASTED}).json()["job_id"]
    assert client.get(f"/chats/{SID}/status?job_id={job_id}").json()["status"] == "pending"
    import os

    conn = db.get_connection(os.environ["DATABASE_PATH"])
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    conn.execute("UPDATE jobs SET created_at = ?", (old,))
    conn.commit()
    conn.close()
    assert client.get(f"/chats/{SID}/status?job_id={job_id}").json()["status"] == "failed"
