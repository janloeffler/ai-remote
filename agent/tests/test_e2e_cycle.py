import base64
import json
import secrets
import time

import pytest

from agent import e2e, main, render_core
from e2e_harness import CANARY, KEYS, PNG, Backend, config, handshake_body, msg, session

SID = "claude-code:abc"
RAW = "abc"

pytestmark = pytest.mark.real_handshake


def _enc_prompt(text, aad):
    return e2e.encrypt_text(KEYS, text, aad)


def _enc_job_prompt(text, aad):
    env = {"v": 1, "prompt": text, "rid": secrets.token_hex(16), "ts": int(time.time() * 1000)}
    return e2e.encrypt_text(KEYS, json.dumps(env), aad)


@pytest.fixture
def world(tmp_path, monkeypatch):
    img = tmp_path / "shot.png"
    img.write_bytes(PNG + f"{CANARY}-image-bytes".encode())
    messages = [
        msg(0, f"hello {CANARY} [Image: source: {img}]"),
        msg(1, f"answer {CANARY}", "assistant"),
    ]
    sess = session(SID, title=f"{CANARY} title", preview=f"{CANARY} preview", messages=messages)
    monkeypatch.setattr("agent.claude_code_source.list_claude_code_sessions", lambda: [sess])
    monkeypatch.setattr("agent.claude_code_source.get_full_messages", lambda raw: messages if raw == RAW else [])
    monkeypatch.setattr("agent.claude_code_source.get_project_path", lambda raw: str(tmp_path))
    monkeypatch.setattr("agent.cursor_source.list_cursor_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.enrich_with_messages", lambda s: None)
    monkeypatch.setattr("agent.permission_profile.missing_profile_reason", lambda p, t: None)
    launched = []

    def fake_run(cmd, cwd):
        launched.append(cmd)
        return 0, f"output with {CANARY}", False

    monkeypatch.setattr("agent.executor._run_subprocess", fake_run)
    return {"sess": sess, "messages": messages, "img": img, "launched": launched, "tmp": tmp_path}


def _cfg(w, **kw):
    return config(w["tmp"], image_upload_enabled=True, allowed_projects=[str(w["tmp"])], **kw)


def test_canary_never_leaves_the_machine_in_plaintext(world):
    jobs = [
        {"id": "j1", "type": "fetch_full", "target": SID, "payload": ""},
        {
            "id": "j2",
            "type": "resume_message",
            "target": SID,
            "payload": json.dumps({"prompt": _enc_job_prompt("please CANARY-7f3a", e2e.aad_resume_prompt(SID))}),
        },
        {
            "id": "j3",
            "type": "search",
            "target": "*",
            "payload": json.dumps({"query": _enc_prompt("canary-7F3A", e2e.AAD_SEARCH)}),
        },
        {"id": "j4", "type": "fetch_image", "target": SID, "payload": json.dumps({"path": str(world["img"])})},
    ]
    backend = Backend(handshake=(200, handshake_body(True)), jobs=jobs)
    sess_before = json.dumps(world["sess"], sort_keys=True)
    main.run_cycle(_cfg(world), backend.client)

    # cached session dicts untouched
    assert json.dumps(world["sess"], sort_keys=True) == sess_before
    # resume got the decrypted prompt
    assert world["launched"][0][-1] == "please CANARY-7f3a"

    needle = CANARY.encode()
    for r in backend.requests:
        assert needle not in r.content, r.url.path
        assert needle.lower() not in r.content.lower()
        if r.url.path == "/sync/image":
            raw = base64.b64decode(json.loads(r.content)["data_b64"])
            assert needle not in raw and raw.startswith(b"e2e1")

    # ciphertexts decrypt to the expected content with the right AAD
    s = backend.bodies("/sync/index")[0]["sessions"][0]
    assert e2e.decrypt_text(KEYS, s["title"], e2e.aad_title(SID)) == f"{CANARY} title"
    prev = json.loads(e2e.decrypt_text(KEYS, s["last_message_preview"], e2e.aad_preview(SID)))
    assert prev["text"] == f"{CANARY} preview" and CANARY in prev["html"]
    assert len(s["recent_messages"]) == 2
    for m in s["recent_messages"]:
        html = e2e.decrypt_text(KEYS, m["content"], e2e.aad_message(SID, m["idx"]))
        assert CANARY in html

    done = {r.url.path: json.loads(r.content) for r in backend.requests if "/complete" in r.url.path}
    full = done["/jobs/j1/complete"]
    assert CANARY in e2e.decrypt_text(KEYS, full["messages"][1]["content"], e2e.aad_message(SID, 1))
    assert full["result_text"] == "" and full["is_complete"] is True
    resume = e2e.decrypt_text(KEYS, done["/jobs/j2/complete"]["result_text"], e2e.aad_job_result("j2"))
    assert CANARY in resume
    found = e2e.decrypt_text(KEYS, done["/jobs/j3/complete"]["result_text"], e2e.aad_job_result("j3"))
    assert json.loads(found) == {"ids": [SID]}
    assert done["/jobs/j4/complete"]["status"] == "done"

    # images: pasted upload and fetch_image, both decryptable
    imgs = backend.bodies("/sync/image")
    assert len(imgs) == 2
    for body in imgs:
        blob = base64.b64decode(body["data_b64"])
        plain = e2e.decrypt_bytes(KEYS, blob, e2e.aad_image(SID, render_core.path_key(SID, str(world["img"]))))
        assert plain.startswith(PNG) and CANARY.encode() in plain
        assert body["path"] == str(world["img"])


def test_unreadable_prompt_fails_job_without_launching(world):
    wrong = _enc_job_prompt("do it", e2e.aad_resume_prompt("claude-code:other"))
    new_wrong = _enc_job_prompt("x", e2e.aad_new_session_prompt("/other", "claude-code"))
    jobs = [
        {"id": "j1", "type": "resume_message", "target": SID, "payload": json.dumps({"prompt": wrong})},
        {"id": "j2", "type": "resume_message", "target": SID, "payload": json.dumps({"prompt": "not encrypted"})},
        {
            "id": "j3",
            "type": "new_session",
            "target": str(world["tmp"]),
            "payload": json.dumps({"prompt": new_wrong, "tool": "claude-code"}),
        },
    ]
    backend = Backend(handshake=(200, handshake_body(True)), jobs=jobs)
    main.run_cycle(_cfg(world), backend.client)
    assert world["launched"] == []
    for jid in ("j1", "j2", "j3"):
        body = backend.bodies(f"/jobs/{jid}/complete")[0]
        assert body["status"] == "failed"
        assert e2e.decrypt_text(KEYS, body["result_text"], e2e.aad_job_result(jid)) == "cannot decrypt prompt"


def test_new_session_prompt_decrypted_with_project_and_tool_aad(world):
    p = str(world["tmp"])
    prompt = _enc_job_prompt("build", e2e.aad_new_session_prompt(p, "claude-code"))
    job = {"id": "j1", "type": "new_session", "target": p, "payload": json.dumps({"prompt": prompt, "tool": "claude-code"})}
    backend = Backend(handshake=(200, handshake_body(True)), jobs=[job])
    main.run_cycle(_cfg(world), backend.client)
    assert world["launched"][0][-1] == "build"


def test_search_job_in_plaintext_mode_is_rejected(world):
    job = {"id": "j1", "type": "search", "target": "*", "payload": json.dumps({"query": "x"})}
    backend = Backend(handshake=(200, handshake_body(False)), jobs=[job])
    main.run_cycle(config(world["tmp"], e2e_on=False), backend.client)
    body = backend.bodies("/jobs/j1/complete")[0]
    assert body["status"] == "failed" and body["result_text"] == "search jobs require E2E mode"
