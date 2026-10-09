import json

import pytest

from agent import e2e, main, render_core, seal, search
from e2e_harness import CANARY, KEYS, PNG, Backend, config, handshake_body, msg, session

pytestmark = pytest.mark.real_handshake

GOOD, BAD = "claude-code:good", "claude-code:bad"


def _patch_sources(monkeypatch, sessions, full=None):
    monkeypatch.setattr("agent.claude_code_source.list_claude_code_sessions", lambda: sessions)
    monkeypatch.setattr("agent.claude_code_source.get_full_messages", full or (lambda raw: []))
    monkeypatch.setattr("agent.cursor_source.list_cursor_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.enrich_with_messages", lambda s: None)


def _raise_for_bad(monkeypatch):
    real = render_core.render_markdown

    def fake(text, images=None):
        if "BOOM" in text:
            raise RuntimeError(f"secret {CANARY}")
        return real(text, images)

    monkeypatch.setattr("agent.seal.render_markdown", fake)


def test_one_unsealable_session_does_not_stop_the_cycle(tmp_path, monkeypatch, capsys):
    good = session(GOOD, messages=[msg(0, "fine")])
    bad = session(BAD, messages=[msg(0, "BOOM")])
    _patch_sources(monkeypatch, [good, bad])
    _raise_for_bad(monkeypatch)
    backend = Backend(handshake=(200, handshake_body(True)))
    cfg = config(tmp_path)
    main.run_cycle(cfg, backend.client)
    sent = backend.bodies("/sync/index")[0]["sessions"]
    assert [s["id"] for s in sent] == [GOOD]
    assert "/jobs/pending" in backend.paths()
    state = json.loads(cfg.state_path.read_text())
    assert GOOD in state and BAD not in state
    err = capsys.readouterr().err
    assert f"session {BAD}: cannot seal (RuntimeError)" in err
    assert CANARY not in err


def test_fetch_full_seal_failure_fails_job_with_generic_encrypted_message(tmp_path, monkeypatch, capsys):
    _patch_sources(monkeypatch, [], full=lambda raw: [msg(0, "BOOM")])
    _raise_for_bad(monkeypatch)
    jobs = [{"id": "j1", "type": "fetch_full", "target": BAD, "payload": ""}]
    backend = Backend(handshake=(200, handshake_body(True)), jobs=jobs)
    main.run_cycle(config(tmp_path), backend.client)
    body = backend.bodies("/jobs/j1/complete")[0]
    assert body["status"] == "failed" and body["messages"] == []
    text = e2e.decrypt_text(KEYS, body["result_text"], e2e.aad_job_result("j1"))
    assert text == "cannot render messages"
    assert CANARY not in capsys.readouterr().err


def test_search_skips_session_that_fails_to_load(monkeypatch):
    sessions = [session(GOOD, title="x"), session(BAD, title="y")]

    def full(raw):
        if raw == "bad":
            raise OSError("unreadable")
        return [msg(0, "needle here")]

    _patch_sources(monkeypatch, sessions, full=full)
    job = {"id": "j", "type": "search", "target": "*",
           "payload": json.dumps({"query": e2e.encrypt_text(KEYS, "needle", e2e.AAD_SEARCH)})}
    result = search.execute_search(job, KEYS, ("claude-code",))
    assert result["status"] == "done"
    assert json.loads(result["result_text"]) == {"ids": [GOOD]}


@pytest.mark.parametrize("e2e_on", [True, False])
def test_mode_header_on_sync_and_job_complete(tmp_path, monkeypatch, e2e_on):
    sess = session(GOOD, messages=[msg(0, "hi")])
    _patch_sources(monkeypatch, [sess], full=lambda raw: [msg(0, "hi")])
    jobs = [{"id": "j1", "type": "fetch_full", "target": GOOD, "payload": ""}]
    backend = Backend(handshake=(200, handshake_body(e2e_on)), jobs=jobs)
    main.run_cycle(config(tmp_path, e2e_on=e2e_on), backend.client)
    want = "1" if e2e_on else "0"
    for path in ("/sync/index", "/jobs/j1/complete"):
        reqs = [r for r in backend.requests if r.url.path == path]
        assert reqs and all(r.headers["x-ai-remote-e2e"] == want for r in reqs)


@pytest.mark.parametrize("e2e_on", [True, False])
def test_mode_header_on_image_upload(tmp_path, monkeypatch, e2e_on):
    img = tmp_path / "s.png"
    img.write_bytes(PNG + b"data")
    sess = session(GOOD, messages=[msg(0, f"[Image: source: {img}]")])
    _patch_sources(monkeypatch, [sess])
    backend = Backend(handshake=(200, handshake_body(e2e_on)))
    main.run_cycle(config(tmp_path, e2e_on=e2e_on, image_upload_enabled=True), backend.client)
    reqs = [r for r in backend.requests if r.url.path == "/sync/image"]
    assert reqs and all(r.headers["x-ai-remote-e2e"] == ("1" if e2e_on else "0") for r in reqs)


def _decrypted_html(sealed, sid):
    m = sealed["recent_messages"][0]
    html = e2e.decrypt_text(KEYS, m["content"], e2e.aad_message(sid, m["idx"]))
    prev = json.loads(e2e.decrypt_text(KEYS, sealed["last_message_preview"], e2e.aad_preview(sid)))["html"]
    return html, prev


def test_seal_session_image_buttons_only_when_uploads_enabled():
    text = "see [Image: source: /tmp/a.png]"
    sess = session(GOOD, preview=text, messages=[msg(0, text)])
    on_msg, on_prev = _decrypted_html(seal.seal_session(sess, KEYS, True), GOOD)
    off_msg, off_prev = _decrypted_html(seal.seal_session(sess, KEYS, False), GOOD)
    assert "data-session-id" in on_msg and "data-session-id" in on_prev
    assert "data-session-id" not in off_msg and "data-session-id" not in off_prev
    assert "a.png" in off_msg
    assert "data-session-id" not in e2e.decrypt_text(
        KEYS, seal.seal_message(GOOD, msg(0, text), KEYS, False)["content"], e2e.aad_message(GOOD, 0)
    )
