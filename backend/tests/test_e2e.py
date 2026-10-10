import base64
import json
import logging
import os
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db, e2e

API = "test-api-key-1234567890-abcdefgh"
KEY = {"Authorization": f"Bearer {API}"}
CANARY = "CANARY-7f3a"
SID = "claude-code:abc"
PNG = b"\x89PNG\r\n\x1a\n" + CANARY.encode() + b"\x00" * 32
SALT = base64.b64encode(b"s" * 16).decode()
KDF = {"alg": "argon2id", "m": 65536, "t": 3, "p": 1, "v": 19}
CHECK = "ab" * 32
PARAMS = {"salt": SALT, "kdf": KDF, "key_check": CHECK, "reset": False}


def ct(n=40):
    return "e2e1:" + base64.urlsafe_b64encode(os.urandom(n)).decode().rstrip("=")


def blob(n=60):
    return b"e2e1" + os.urandom(n)


def session(sid=SID, enc=False, **over):
    s = {
        "id": sid, "tool": "claude-code", "project_path": "/p",
        "title": ct() if enc else "plain title", "created_at": "2026-08-01T10:00:00Z",
        "last_updated_at": "2026-08-01T10:05:00Z", "message_count": 1,
        "last_message_preview": ct() if enc else "plain preview",
        "recent_messages": [{"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z",
                             "content": ct() if enc else "plain msg"}],
    }
    s.update(over)
    return s


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("API_KEY", API)
    monkeypatch.setenv("SECRET_KEY", "test-secret-value-1234567890abcd")
    monkeypatch.setenv("SESSION_COOKIE_HTTPS_ONLY", "false")
    monkeypatch.setenv("IMAGE_DIR", str(tmp_path / "images"))
    from app import settings

    monkeypatch.setattr(settings, "IMAGE_UPLOAD_ENABLED", True)
    monkeypatch.setattr(settings, "ALLOWED_PROJECTS", ["/p"])
    return tmp_path


def start(monkeypatch, e2e_on):
    from app import settings
    from app.main import app

    monkeypatch.setattr(settings, "E2E_ENCRYPTION", e2e_on)
    c = TestClient(app)
    c.__enter__()
    c.post("/login", data={"api_key": API}, follow_redirects=False)
    return c


@pytest.fixture
def plain(env, monkeypatch):
    c = start(monkeypatch, False)
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def enc(env, monkeypatch):
    c = start(monkeypatch, True)
    yield c
    c.__exit__(None, None, None)


def raw_conn(env):
    return db.get_connection(str(env / "test.db"))


def _row():
    return {"entrypoint": "", "status": "idle", **{k: v for k, v in session().items() if k != "recent_messages"}}


def sync(c, *sessions):
    return c.post("/sync/index", headers=KEY, json={"sessions": list(sessions)})


# --- is_ciphertext -----------------------------------------------------------------

def test_ciphertext_format():
    good = ct()
    assert e2e.is_ciphertext(good)
    assert not e2e.is_ciphertext(good + "\n")
    assert not e2e.is_ciphertext("e2e1:" + "A" * 37)
    assert e2e.is_ciphertext("e2e1:" + "A" * 38)
    assert not e2e.is_ciphertext("e2e2:" + "A" * 40)
    assert not e2e.is_ciphertext("e2e1:" + "A" * 40 + "=")
    assert not e2e.is_ciphertext("hello")
    assert not e2e.is_ciphertext(None)


# --- reconcile ---------------------------------------------------------------------

def test_legacy_db_is_plain_without_wipe(conn):
    conn.execute("UPDATE e2e_state SET mode = NULL")
    db.upsert_session(conn, _row())
    epoch = e2e.get_state(conn)["epoch"]
    e2e.reconcile_mode(conn, False)
    assert len(db.get_sessions(conn)) == 1
    assert e2e.get_state(conn)["epoch"] == epoch
    assert conn.execute("SELECT mode FROM e2e_state").fetchone()["mode"] == "plain"


def test_plain_to_e2e_wipes_and_logs(conn, caplog):
    e2e.reconcile_mode(conn, False)
    db.upsert_session(conn, _row())
    db.create_job(conn, "fetch_full", SID)
    epoch = e2e.get_state(conn)["epoch"]
    with caplog.at_level(logging.WARNING):
        e2e.reconcile_mode(conn, True)
    assert "plain->e2e" in caplog.text and "backups" in caplog.text
    assert db.get_sessions(conn) == [] and db.get_all_jobs(conn) == []
    state = e2e.get_state(conn)
    assert state["mode"] == "e2e" and state["epoch"] != epoch


def test_e2e_to_plain_wipes_keeps_params(conn):
    e2e.reconcile_mode(conn, True)
    e2e.set_params(conn, SALT, KDF, CHECK)
    db.create_job(conn, "search", "*")
    epoch = e2e.get_state(conn)["epoch"]
    e2e.reconcile_mode(conn, False)
    state = e2e.get_state(conn)
    assert db.get_all_jobs(conn) == []
    assert state["mode"] == "plain" and state["epoch"] != epoch
    assert state["salt"] == SALT  # kept: e2e -> plain -> e2e keeps the passphrase valid


def test_same_mode_is_a_noop(conn):
    e2e.reconcile_mode(conn, True)
    epoch = e2e.get_state(conn)["epoch"]
    db.create_job(conn, "search", "*")
    e2e.reconcile_mode(conn, True)
    assert e2e.get_state(conn)["epoch"] == epoch and len(db.get_all_jobs(conn)) == 1


def test_wipe_raises_when_something_survives(conn, monkeypatch):
    monkeypatch.setattr(e2e, "_search_index_ddl", lambda: "CREATE TABLE search_index (a)")
    conn.execute("INSERT INTO sessions (id, tool, title, created_at, last_updated_at) VALUES ('x','cursor','t','a','b')")
    # recreated table is empty, so wipe succeeds; instead make a delete impossible
    conn.execute("CREATE TRIGGER keep AFTER DELETE ON jobs BEGIN INSERT INTO jobs (id,type,target,created_at) VALUES ('r','search','*','x'); END")
    conn.execute("INSERT INTO jobs (id,type,target,created_at) VALUES ('j','search','*','x')")
    with pytest.raises(RuntimeError):
        e2e.wipe_content(conn)


def test_old_jobs_table_without_search_is_rebuilt(tmp_path):
    c = db.get_connection(str(tmp_path / "old.db"))
    c.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('fetch_full','resume_message','new_session','fetch_image')),"
        " target TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',"
        " result_text TEXT, created_at TEXT NOT NULL, completed_at TEXT)"
    )
    c.execute("INSERT INTO jobs (id,type,target,created_at) VALUES ('a','fetch_full','t','x')")
    c.commit()
    db.init_db(c)
    db.create_job(c, "search", "*")
    assert len(db.get_all_jobs(c)) == 2
    c.close()


def test_search_job_times_out(conn):
    from datetime import datetime, timedelta, timezone

    jid = db.create_job(conn, "search", "*")
    old = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
    conn.execute("UPDATE jobs SET created_at = ?", (old,))
    db.fail_stale_jobs(conn)
    assert db.get_job(conn, jid)["status"] == "failed"


# --- wipe canary -------------------------------------------------------------------

def test_mode_switch_leaves_no_canary_on_disk(env, monkeypatch):
    c = start(monkeypatch, False)
    s = session(title=CANARY, last_message_preview=CANARY)
    s["recent_messages"][0]["content"] = f"{CANARY} body"
    assert sync(c, s).status_code == 200
    r = c.post(f"/chats/{SID}/command", json={"prompt": f"resume {CANARY}"})
    jid = r.json()["job_id"]
    c.get("/jobs/pending", headers=KEY)
    c.post(f"/jobs/{jid}/complete", headers=KEY, json={"status": "done", "result_text": f"res {CANARY}"})
    r = c.post("/sync/image", headers=KEY, json={"session_id": SID, "path": "/x/a.png", "data_b64": base64.b64encode(PNG).decode()})
    assert r.status_code == 200
    assert any(CANARY.encode() in (env / "images" / f).read_bytes() for f in os.listdir(env / "images"))
    c.__exit__(None, None, None)

    c = start(monkeypatch, True)
    c.__exit__(None, None, None)
    files = [env / "test.db"] + ([env / "test.db-wal"] if (env / "test.db-wal").exists() else [])
    files += [env / "images" / f for f in os.listdir(env / "images")]
    for f in files:
        assert CANARY.encode() not in f.read_bytes(), f
    assert os.listdir(env / "images") == []


def test_switch_back_to_plain_empties_tables(env, monkeypatch):
    c = start(monkeypatch, True)
    assert sync(c, session(enc=True)).status_code == 200
    c.__exit__(None, None, None)
    conn = raw_conn(env)
    epoch = e2e.get_state(conn)["epoch"]
    conn.close()
    c = start(monkeypatch, False)
    c.__exit__(None, None, None)
    conn = raw_conn(env)
    assert db.get_sessions(conn) == [] and e2e.get_state(conn)["epoch"] != epoch
    conn.close()


# --- ingest ------------------------------------------------------------------------

def test_e2e_ingest_accepts_ciphertext_and_skips_fts(enc, env):
    assert sync(enc, session(enc=True)).status_code == 200
    jid = db.create_job(raw_conn(env), "fetch_full", SID)
    r = enc.post(f"/jobs/{jid}/complete", headers=KEY, json={
        "status": "done", "result_text": ct(),
        "messages": [{"idx": 0, "role": "user", "timestamp": "t", "content": ct()}]})
    assert r.status_code == 200
    conn = raw_conn(env)
    assert conn.execute("SELECT COUNT(*) FROM search_index").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("field", ["title", "last_message_preview", "msg"])
def test_e2e_ingest_rejects_plaintext(enc, field):
    s = session(enc=True)
    if field == "msg":
        s["recent_messages"][0]["content"] = "plain"
    else:
        s[field] = "plain"
    assert sync(enc, s).status_code == 422


def test_e2e_ingest_rejects_trailing_newline(enc):
    s = session(enc=True)
    s["title"] += "\n"
    assert sync(enc, s).status_code == 422


def test_e2e_complete_validation(enc, env):
    sync(enc, session(enc=True))
    conn = raw_conn(env)
    jid = db.create_job(conn, "fetch_full", SID)
    bad_msg = {"status": "done", "messages": [{"idx": 0, "role": "u", "timestamp": "t", "content": "plain"}]}
    assert enc.post(f"/jobs/{jid}/complete", headers=KEY, json=bad_msg).status_code == 422
    assert enc.post(f"/jobs/{jid}/complete", headers=KEY, json={"status": "done", "result_text": "plain"}).status_code == 422
    assert enc.post(f"/jobs/{jid}/complete", headers=KEY, json={"status": "done", "result_text": ""}).status_code == 200
    conn.close()


def test_plain_mode_accepts_content_starting_with_e2e1(plain, env):
    s = session(title="e2e1: what is this", last_message_preview="e2e1: preview")
    s["recent_messages"][0]["content"] = "e2e1: what is this"
    assert sync(plain, s).status_code == 200
    jid = db.create_job(raw_conn(env), "fetch_full", SID)
    r = plain.post(f"/jobs/{jid}/complete", headers=KEY, json={
        "status": "done", "result_text": "e2e1: result",
        "messages": [{"idx": 0, "role": "user", "timestamp": "t", "content": "e2e1: " + "A" * 60}]})
    assert r.status_code == 200
    assert plain.post(f"/chats/{SID}/command", json={"prompt": "e2e1: what is this"}).status_code == 200
    assert plain.post(f"/chats/{SID}/command", json={"prompt": ct()}).status_code == 200


def test_mode_header_mismatch_is_409_in_plain(plain, env):
    h1 = {**KEY, "X-AI-Remote-E2E": "1"}
    assert plain.post("/sync/index", headers=h1, json={"sessions": [session()]}).status_code == 409
    r = plain.post("/sync/index", headers=h1, json={"sessions": [session()]})
    assert "E2E mode but the server is not" in r.json()["detail"]
    jid = db.create_job(raw_conn(env), "fetch_full", SID)
    assert plain.post(f"/jobs/{jid}/complete", headers=h1, json={"status": "done"}).status_code == 409
    assert plain.post("/sync/image", headers=h1, json={"session_id": SID, "path": "/a.png", "data_b64": ""}).status_code == 409
    h0 = {**KEY, "X-AI-Remote-E2E": "0"}
    assert plain.post("/sync/index", headers=h0, json={"sessions": [session()]}).status_code == 200
    assert plain.post(f"/jobs/{jid}/complete", headers=h0, json={"status": "done"}).status_code == 200


def test_mode_header_mismatch_is_409_in_e2e(enc, env):
    h0 = {**KEY, "X-AI-Remote-E2E": "0"}
    assert enc.post("/sync/index", headers=h0, json={"sessions": [session(enc=True)]}).status_code == 409
    jid = db.create_job(raw_conn(env), "fetch_full", SID)
    assert enc.post(f"/jobs/{jid}/complete", headers=h0, json={"status": "done"}).status_code == 409
    assert enc.post("/sync/image", headers=h0, json={"session_id": SID, "path": "/a.png", "data_b64": ""}).status_code == 409
    h1 = {**KEY, "X-AI-Remote-E2E": "1"}
    assert enc.post("/sync/index", headers=h1, json={"sessions": [session(enc=True)]}).status_code == 200
    assert enc.post(f"/jobs/{jid}/complete", headers=h1, json={"status": "done"}).status_code == 200


def test_e2e_mode_still_rejects_plaintext_with_header(enc):
    h1 = {**KEY, "X-AI-Remote-E2E": "1"}
    assert enc.post("/sync/index", headers=h1, json={"sessions": [session()]}).status_code == 422


def test_plain_mode_still_uses_fts(plain):
    sync(plain, session())
    assert [s["id"] for s in plain.get("/?q=plain").context["sessions"]] == [SID]


# --- handshake / params ------------------------------------------------------------

def test_handshake_requires_key(plain):
    assert plain.get("/agent/handshake").status_code == 401
    assert plain.put("/agent/e2e-params", json=PARAMS).status_code == 401


def test_handshake_plain(plain):
    body = plain.get("/agent/handshake", headers=KEY).json()
    assert body["e2e"] is False and body["salt"] is None and body["kdf"] is None
    assert body["key_check"] is None and body["epoch"]


def test_handshake_e2e_unset_then_set(enc):
    body = enc.get("/agent/handshake", headers=KEY).json()
    assert body["e2e"] is True and body["salt"] is None
    r = enc.put("/agent/e2e-params", headers=KEY, json=PARAMS)
    assert r.status_code == 200
    assert r.json() == {"e2e": True, "epoch": body["epoch"], "salt": SALT, "kdf": KDF, "key_check": CHECK}
    assert enc.get("/agent/handshake", headers=KEY).json() == r.json()


def test_params_409_in_plain(plain):
    assert plain.put("/agent/e2e-params", headers=KEY, json=PARAMS).status_code == 409


@pytest.mark.parametrize("change", [
    {"salt": "!!!"}, {"salt": base64.b64encode(b"x" * 15).decode()},
    {"salt": base64.b64encode(b"x" * 65).decode()},
    {"key_check": "AB" * 32}, {"key_check": "ab" * 31}, {"key_check": 5},
    {"kdf": {**KDF, "alg": "argon2i"}}, {"kdf": {**KDF, "m": 100}}, {"kdf": {**KDF, "m": 2000000}},
    {"kdf": {**KDF, "t": 1}}, {"kdf": {**KDF, "t": 11}}, {"kdf": {**KDF, "p": 0}},
    {"kdf": {**KDF, "p": 5}}, {"kdf": {**KDF, "t": True}}, {"kdf": {**KDF, "v": 16}},
    {"kdf": {**KDF, "x": 1}}, {"kdf": "argon2id"}, {"kdf": {"alg": "argon2id"}},
])
def test_params_invalid_422(enc, change):
    assert enc.put("/agent/e2e-params", headers=KEY, json={**PARAMS, **change}).status_code == 422
    assert enc.get("/agent/handshake", headers=KEY).json()["salt"] is None


def test_params_idempotent_conflict_and_reset(enc, env):
    assert enc.put("/agent/e2e-params", headers=KEY, json=PARAMS).status_code == 200
    sync(enc, session(enc=True))
    epoch = enc.get("/agent/handshake", headers=KEY).json()["epoch"]
    assert enc.put("/agent/e2e-params", headers=KEY, json=PARAMS).status_code == 200
    assert len(db.get_sessions(raw_conn(env))) == 1
    other = {**PARAMS, "salt": base64.b64encode(b"z" * 16).decode()}
    assert enc.put("/agent/e2e-params", headers=KEY, json=other).status_code == 409
    assert enc.get("/agent/handshake", headers=KEY).json()["salt"] == SALT
    r = enc.put("/agent/e2e-params", headers=KEY, json={**other, "reset": True})
    assert r.status_code == 200 and r.json()["salt"] == other["salt"] and r.json()["epoch"] != epoch
    assert db.get_sessions(raw_conn(env)) == []


# --- search / ids / q --------------------------------------------------------------

def test_search_job(enc, env):
    r = enc.post("/search", json={"query": ct()})
    assert r.status_code == 200 and {"job_id", "eta_seconds"} <= set(r.json())
    job = db.get_job(raw_conn(env), r.json()["job_id"])
    assert job["type"] == "search" and job["target"] == "*"
    assert enc.post("/search", json={"query": "plain"}).status_code == 422
    assert enc.post("/search", json={"query": "e2e1:" + "A" * 171_000}).status_code == 422
    assert enc.post("/search", json={}).status_code == 422


def test_search_not_blocked_by_pause_and_claimable(enc, env):
    conn = raw_conn(env)
    db.set_remote_commands_paused(conn, True)
    assert enc.post("/search", json={"query": ct()}).status_code == 200
    jobs = enc.get("/jobs/pending", headers=KEY).json()["jobs"]
    assert [j["type"] for j in jobs] == ["search"]


def test_search_plain_409_and_auth(plain):
    assert plain.post("/search", json={"query": ct()}).status_code == 409
    anon = TestClient(plain.app)
    assert anon.post("/search", json={"query": ct()}, follow_redirects=False).status_code == 307


def test_search_max_length_boundary(enc):
    ok = "e2e1:" + "A" * (171_000 - 5)
    assert enc.post("/search", json={"query": ok}).status_code == 200


def test_ids_only_via_post_never_in_get(plain):
    sync(plain, session("claude-code:a"), session("claude-code:b"))
    ids = lambda r: sorted(s["id"] for s in r.context["sessions"])
    assert len(ids(plain.get("/?ids=claude-code:a"))) == 2  # GET ignores ids
    post = lambda v: plain.post("/", data={"ids": v})
    assert ids(post("claude-code:a")) == ["claude-code:a"]
    assert ids(post("claude-code:a,claude-code:b, ,nope")) == ["claude-code:a", "claude-code:b"]
    assert ids(post("nope")) == []
    # FastAPI reads an empty optional form field as missing, so "" lifts the restriction
    # like an absent field does. The browser never sends it: a search without hits shows
    # "no results" in place instead of navigating.
    assert len(ids(post(""))) == 2
    # No ids field at all (search box cleared on a results page): the full list.
    no_ids = plain.post("/", data={"sort": "date_desc"})
    assert len(ids(no_ids)) == 2 and no_ids.context["ids_restricted"] is False
    assert len(ids(plain.get("/"))) == 2
    assert post(",".join(f"i{n}" for n in range(101))).status_code == 422
    assert post(",".join(f"i{n}" for n in range(100))).status_code == 200


def test_post_list_keeps_filters_and_requires_session(plain, enc):
    sync(plain, session("claude-code:a"))
    r = plain.post("/", data={"ids": "claude-code:a", "tool": "cursor", "sort": "date_asc"})
    assert r.context["sessions"] == [] and r.context["sort"] == "date_asc"
    plain.cookies.clear()
    assert plain.post("/", data={"ids": "x"}, follow_redirects=False).status_code in (303, 307, 401)


def test_e2e_ignores_q_and_title_sort(enc):
    sync(enc, session("claude-code:a", enc=True), session("claude-code:b", enc=True))
    r = enc.get("/?q=nothingmatches&sort=title_asc")
    assert len(r.context["sessions"]) == 2
    assert r.context["sort"] == "date_desc" and r.context["q"] is None
    assert 'value="title_asc"' not in r.text
    assert 'name="q"' not in r.text
    r = enc.post("/", data={"ids": "claude-code:a"})
    assert 'name="ids" value="claude-code:a"' in r.text and 'href="/"' in r.text
    assert '<form method="post" action="/" class="toolbar">' in r.text
    assert '<form method="get" action="/" class="toolbar">' in enc.get("/").text


# --- prompts -----------------------------------------------------------------------

def test_e2e_prompts_need_ciphertext(enc):
    sync(enc, session(enc=True))
    assert enc.post(f"/chats/{SID}/command", json={"prompt": "plain"}).status_code == 422
    assert enc.post(f"/chats/{SID}/command", json={"prompt": ct()}).status_code == 200
    big = "e2e1:" + "A" * (171_000 - 5)
    assert enc.post(f"/chats/{SID}/command", json={"prompt": big}).status_code == 200
    assert enc.post(f"/chats/{SID}/command", json={"prompt": big + "A"}).status_code == 422
    body = {"project_path": "/p", "tool": "claude-code"}
    assert enc.post("/projects/command", json={**body, "prompt": "plain"}).status_code == 422
    assert enc.post("/projects/command", json={**body, "prompt": ct()}).status_code == 200


def test_plain_prompt_limit_stays_32000(plain):
    sync(plain, session())
    assert plain.post(f"/chats/{SID}/command", json={"prompt": "x" * 32000}).status_code == 200
    assert plain.post(f"/chats/{SID}/command", json={"prompt": "x" * 32001}).status_code == 422
    assert plain.post("/projects/command", json={"project_path": "/p", "tool": "claude-code", "prompt": "x" * 32001}).status_code == 422


# --- images ------------------------------------------------------------------------

def test_fetch_image_skips_message_check_in_e2e(enc):
    sync(enc, session(enc=True))
    r = enc.post(f"/chats/{SID}/fetch-image", json={"path": "/not/in/any/message.png"})
    assert r.status_code == 200 and r.json()["available"] is False
    assert enc.post(f"/chats/{SID}/fetch-image", json={"path": "/x/notimage.txt"}).status_code == 400


def test_fetch_image_checks_messages_in_plain(plain):
    sync(plain, session())
    assert plain.post(f"/chats/{SID}/fetch-image", json={"path": "/not/in/msg.png"}).status_code == 400


def _up(c, data, path="/x/a.png"):
    return c.post("/sync/image", headers=KEY, json={"session_id": SID, "path": path, "data_b64": base64.b64encode(data).decode()})


def test_e2e_image_store_and_serve(enc, env):
    sync(enc, session(enc=True))
    data = blob()
    r = _up(enc, data)
    assert r.status_code == 200
    key = r.json()["key"]
    assert (env / "images" / f"{key}.bin").read_bytes() == data
    got = enc.get(f"/chats/{SID}/images/{key}")
    assert got.content == data and got.headers["content-type"] == "application/octet-stream"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in got.headers["content-security-policy"]


def test_e2e_image_rejections(enc):
    sync(enc, session(enc=True))
    assert _up(enc, PNG).status_code == 415
    assert _up(enc, b"e2e").status_code == 415
    assert _up(enc, b"e2e1" + b"x" * (5 * 1024 * 1024 + 28)).status_code == 200
    assert _up(enc, b"e2e1" + b"x" * (5 * 1024 * 1024 + 29), "/x/b.png").status_code == 413


def test_plain_image_with_magic_rejected(plain):
    sync(plain, session())
    assert _up(plain, blob()).status_code == 415
    assert _up(plain, PNG).status_code == 200


# --- rendering ---------------------------------------------------------------------

def _e2e_pages(enc, env):
    marker = "MARKER-plain-9c1"
    s = session(enc=True, project_path="/p")
    conn = raw_conn(env)
    sync(enc, s)
    jid = db.create_job(conn, "resume_message", SID, payload=json.dumps({"prompt": ct()}))
    enc.post(f"/jobs/{jid}/complete", headers=KEY, json={"status": "done", "result_text": ct()})
    j2 = db.create_job(conn, "search", "*")
    conn.execute("UPDATE jobs SET status='failed', result_text='timed out' WHERE id=?", (j2,))
    conn.commit()
    conn.close()
    return s, marker


def test_e2e_rendering(enc, env):
    s, _ = _e2e_pages(enc, env)
    r = enc.get("/")
    t = r.text
    assert '<script type="application/json" id="e2e-config">' in t
    assert f'data-e2e="{s["title"]}" data-e2e-aad="session|{SID}|title" data-e2e-kind="text">🔒' in t
    assert f'data-e2e-aad="session|{SID}|preview" data-e2e-kind="preview-text"' in t
    assert "plain title" not in t and "plain preview" not in t
    for src in ["hash-wasm-argon2.umd.min.js", "purify.min.js", "e2e-core.js", "e2e.js"]:
        assert f"/static/{'vendor/' if 'min' in src else ''}{src}" in t
    assert t.index("/static/e2e.js") < t.index("/static/app.js")
    versions = re.findall(r'/static/[\w./-]+\?v=([^"\s]+)"', t)
    assert len(versions) >= 5 and all(re.fullmatch(r"[0-9a-f]{12}", v) for v in versions)
    cfg = json.loads(t.split('id="e2e-config">')[1].split("</script>")[0])
    assert cfg["enabled"] is True and cfg["salt"] is None and cfg["binding"] == e2e.binding()
    assert 'id="e2e-search"' in t

    enc.put("/agent/e2e-params", headers=KEY, json=PARAMS)
    t = enc.get("/").text
    cfg = json.loads(t.split('id="e2e-config">')[1].split("</script>")[0])
    assert cfg["salt"] == SALT and cfg["kdf"] == KDF and cfg["key_check"] == CHECK


def test_e2e_detail_and_jobs_rendering(enc, env):
    s, _ = _e2e_pages(enc, env)
    conn = raw_conn(env)
    db.replace_messages(conn, SID, [{"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": ct()},
                                    {"idx": 1, "role": "assistant", "timestamp": "2026-08-01T10:00:00Z", "content": ct()}])
    db.save_image(conn, SID, "a" * 32, "/x/a.png", "application/octet-stream", 5)
    conn.close()
    t = enc.get(f"/chats/{SID}").text
    assert f'<h1 data-e2e="{s["title"]}"' in t
    assert f'data-e2e-aad="msg|{SID}|0" data-e2e-kind="html"></div>' in t
    assert f'data-e2e-aad="msg|{SID}|1"' in t
    assert 'id="image-keys-data">["' + "a" * 32 + '"]' in t
    assert "plain msg" not in t

    t = enc.get("/jobs").text
    assert 'data-e2e-job-payload data-type="resume_message"' in t
    assert "data-e2e-job-result data-job-id=" in t
    assert ">timed out</pre>" in t


def test_e2e_detail_preview(enc):
    sync(enc, session(enc=True, recent_messages=[]))
    t = enc.get(f"/chats/{SID}").text
    assert f'data-e2e-aad="session|{SID}|preview" data-e2e-kind="preview-html"' in t


def test_plain_pages_have_no_e2e_markup(plain):
    sync(plain, session())
    for url in ["/", f"/chats/{SID}", "/jobs", "/settings", "/projects/new"]:
        t = plain.get(url).text
        for needle in ["e2e-config", "e2e.js", "e2e-core.js", "purify", "argon2", "data-e2e", "image-keys-data"]:
            assert needle not in t, (url, needle)
    assert 'name="q"' in plain.get("/").text and 'value="title_asc"' in plain.get("/").text


def test_login_page_has_no_e2e_config_in_e2e_mode(enc):
    anon = TestClient(enc.app)
    t = anon.get("/login").text
    assert "e2e-config" not in t and "e2e.js" not in t


def test_js_strings_present_in_both_languages():
    from app import i18n

    keys = ["unlock_title", "passphrase", "unlock", "wrong_passphrase", "deriving", "not_set_up",
            "decrypt_failed", "searching", "search_failed", "locked"]
    for lang in ("en", "de"):
        cat = i18n.js_catalog(lang)
        for k in keys:
            assert cat[f"js.e2e.{k}"]
    assert i18n.CATALOGS["de"]["js.e2e.unlock"] != i18n.CATALOGS["en"]["js.e2e.unlock"]
    assert i18n.CATALOGS["de"]["list.search_clear"] != i18n.CATALOGS["en"]["list.search_clear"]


# --- wipe verification, atomic rotation, jobs migration -----------------------------

def test_wipe_raises_while_a_reader_blocks_the_checkpoint(env, monkeypatch):
    monkeypatch.setattr(e2e, "CHECKPOINT_BACKOFF_SECONDS", 0.01)
    c = raw_conn(env)
    db.init_db(c)
    c.execute("INSERT INTO sessions (id, tool, title, created_at, last_updated_at) VALUES ('x','cursor','t','a','b')")
    c.commit()
    reader = raw_conn(env)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM sessions").fetchone()
    with pytest.raises(RuntimeError, match="checkpoint"):
        e2e.wipe_content(c)
    reader.rollback()
    reader.close()
    e2e.wipe_content(c)
    wal = env / "test.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0
    c.close()


def test_wipe_drops_leftover_jobs_old(conn):
    conn.execute("CREATE TABLE jobs_old (id TEXT)")
    e2e.wipe_content(conn)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'jobs_old'").fetchone() is None


def test_reset_nulls_params_with_the_new_epoch_then_stores_new_ones(conn, monkeypatch):
    conn.execute("UPDATE e2e_state SET mode = 'e2e'")
    e2e.set_params(conn, SALT, KDF, CHECK)
    epoch = e2e.get_state(conn)["epoch"]
    seen = []
    real = e2e.wipe_content
    monkeypatch.setattr(e2e, "wipe_content", lambda c, **kw: (real(c, **kw), seen.append(e2e.get_state(c)))[0])
    e2e.set_params(conn, SALT, KDF, "cd" * 32, reset=True)
    # the wipe starts the new epoch and nulls the old params in one transaction: agents fail closed
    assert seen[0]["epoch"] != epoch
    assert seen[0]["key_check"] is None and seen[0]["salt"] is None and seen[0]["kdf"] is None
    state = e2e.get_state(conn)
    assert state["epoch"] != epoch and state["key_check"] == "cd" * 32


def test_get_state_is_read_only(conn):
    before = conn.total_changes
    e2e.get_state(conn)
    e2e.get_state(conn)
    assert conn.total_changes == before


def test_get_state_creates_row_if_missing(conn):
    conn.execute("DELETE FROM e2e_state")
    conn.commit()
    assert e2e.get_state(conn)["mode"] == "plain"


OLD_JOBS = (
    "CREATE TABLE {name} (id TEXT PRIMARY KEY, type TEXT NOT NULL CHECK(type IN "
    "('fetch_full','resume_message','new_session')), target TEXT NOT NULL, "
    "payload TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN "
    "('pending','running','done','failed')), result_text TEXT, created_at TEXT NOT NULL, completed_at TEXT);"
)


def test_jobs_migration_is_atomic(tmp_path, monkeypatch):
    c = db.get_connection(str(tmp_path / "a.db"))
    c.executescript(OLD_JOBS.format(name="jobs") + "INSERT INTO jobs (id,type,target,created_at) VALUES ('j1','fetch_full','s','x');")
    c.commit()
    monkeypatch.setattr(db, "_jobs_table_ddl", lambda: "CREATE TABLE jobs (broken syntax")
    with pytest.raises(sqlite3.OperationalError):
        db._migrate_jobs_check_constraint(c)
    names = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "jobs" in names and "jobs_old" not in names
    assert c.execute("SELECT id FROM jobs").fetchone()["id"] == "j1"
    c.close()


def test_jobs_migration_recovers_leftover_jobs_old(tmp_path):
    c = db.get_connection(str(tmp_path / "b.db"))
    # crashed after rename+create: new empty jobs, old rows stranded in jobs_old
    c.executescript(OLD_JOBS.format(name="jobs_old") + "INSERT INTO jobs_old (id,type,target,created_at) VALUES ('j1','fetch_full','s','x'),('j2','resume_message','s','x');")
    db.init_db(c)
    c.execute("INSERT OR IGNORE INTO jobs (id,type,target,created_at) VALUES ('j2','search','*','y')")
    assert {r["id"] for r in c.execute("SELECT id FROM jobs")} == {"j1", "j2"}
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name = 'jobs_old'").fetchone() is None
    c.close()


def test_jobs_migration_recovers_when_jobs_is_old_schema_too(tmp_path):
    c = db.get_connection(str(tmp_path / "c.db"))
    c.executescript(OLD_JOBS.format(name="jobs") + OLD_JOBS.format(name="jobs_old") +
                    "INSERT INTO jobs_old (id,type,target,created_at) VALUES ('j1','fetch_full','s','x');")
    c.commit()
    db._migrate_jobs_check_constraint(c)
    assert c.execute("SELECT id FROM jobs").fetchone()["id"] == "j1"
    assert "'search'" in c.execute("SELECT sql FROM sqlite_master WHERE name='jobs'").fetchone()["sql"]
    c.close()


# --- wipe failure still starts a new epoch ------------------------------------------

def _fail_truncate(monkeypatch):
    def boom(conn):
        raise RuntimeError("wipe incomplete: WAL checkpoint blocked")

    monkeypatch.setattr(e2e, "_truncate_wal", boom)


def test_reconcile_wipe_failure_changes_epoch_and_retries(conn, monkeypatch):
    conn.execute("UPDATE e2e_state SET mode = 'plain'")
    db.create_job(conn, "search", "*")
    conn.commit()
    epoch = e2e.get_state(conn)["epoch"]
    with monkeypatch.context() as m:
        _fail_truncate(m)
        with pytest.raises(RuntimeError):
            e2e.reconcile_mode(conn, True)
    state = e2e.get_state(conn)
    assert state["epoch"] != epoch and state["mode"] == "plain"
    assert db.get_all_jobs(conn) == []
    e2e.reconcile_mode(conn, True)  # next start retries the wipe
    assert e2e.get_state(conn)["mode"] == "e2e"


def test_params_reset_wipe_failure_is_503_then_retry(enc, env, monkeypatch):
    assert enc.put("/agent/e2e-params", headers=KEY, json=PARAMS).status_code == 200
    sync(enc, session(enc=True))
    epoch = enc.get("/agent/handshake", headers=KEY).json()["epoch"]
    other = {**PARAMS, "salt": base64.b64encode(b"z" * 16).decode(), "reset": True}
    with monkeypatch.context() as m:
        _fail_truncate(m)
        r = enc.put("/agent/e2e-params", headers=KEY, json=other)
    assert r.status_code == 503 and "retry" in r.json()["detail"]
    hs = enc.get("/agent/handshake", headers=KEY).json()
    assert hs["salt"] is None and hs["key_check"] is None and hs["epoch"] != epoch  # fail closed
    assert db.get_sessions(raw_conn(env)) == []
    r = enc.put("/agent/e2e-params", headers=KEY, json=other)
    assert r.status_code == 200 and r.json()["salt"] == other["salt"]


@pytest.mark.parametrize("value", ["2", "yes", "", "true", "01"])
def test_invalid_mode_header_is_400(plain, value):
    r = plain.post("/sync/index", headers={**KEY, "X-AI-Remote-E2E": value}, json={"sessions": [session()]})
    assert r.status_code == 400 and "invalid X-AI-Remote-E2E header" in r.json()["detail"]


def test_mode_header_whitespace_is_stripped(plain):
    r = plain.post("/sync/index", headers={**KEY, "X-AI-Remote-E2E": " 0 "}, json={"sessions": [session()]})
    assert r.status_code == 200


def test_search_is_one_at_a_time(enc):
    assert enc.post("/search", json={"query": ct()}).status_code == 200
    r = enc.post("/search", json={"query": ct()})
    assert r.status_code == 409 and r.json()["detail"] == "a search is already running"


def test_search_allowed_again_after_the_job_finished(enc, env):
    c = raw_conn(env)
    job_id = enc.post("/search", json={"query": ct()}).json()["job_id"]
    c.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (job_id,))
    c.commit()
    assert enc.post("/search", json={"query": ct()}).status_code == 200


def test_params_503_on_sqlite_error(enc, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(e2e, "set_params", boom)
    assert enc.put("/agent/e2e-params", headers=KEY, json=PARAMS).status_code == 503


def test_kdf_version_must_be_19(enc):
    for kdf in ({k: v for k, v in PARAMS["kdf"].items() if k != "v"}, {**PARAMS["kdf"], "v": 16}):
        r = enc.put("/agent/e2e-params", headers=KEY, json={**PARAMS, "kdf": kdf})
        assert r.status_code == 422


def test_reconcile_on_empty_db_logs_neutrally(conn, caplog):
    e2e.reconcile_mode(conn, False)
    with caplog.at_level(logging.INFO):
        e2e.reconcile_mode(conn, True)
    assert "empty database" in caplog.text and "backups" not in caplog.text
    assert e2e.get_state(conn)["mode"] == "e2e"


def test_html_pages_carry_csp_and_framing_headers(plain):
    for r in (plain.get("/"), plain.get("/settings")):
        csp = r.headers["content-security-policy"]
        assert "script-src 'self' 'wasm-unsafe-eval'" in csp and "frame-ancestors 'none'" in csp
        assert "unsafe-inline" not in csp
        assert r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "no-referrer"


def test_base_template_has_no_inline_executable_script():
    import re
    from pathlib import Path

    root = Path(__file__).parent.parent / "app" / "templates"
    for page in root.glob("*.html"):
        for m in re.finditer(r"<script\b([^>]*)>", page.read_text()):
            attrs = m.group(1)
            assert "src=" in attrs or "application/json" in attrs, f"{page.name}: inline script"
        assert "style=" not in page.read_text(), f"{page.name}: inline style"
    assert (Path(__file__).parent.parent / "app" / "static" / "theme.js").is_file()
