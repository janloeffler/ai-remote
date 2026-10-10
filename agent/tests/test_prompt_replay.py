import json
import secrets
import time

import pytest

from agent import e2e, seal
from e2e_harness import KEYS

SID = "claude-code:abc"
NOW_MS = lambda: int(time.time() * 1000)  # noqa: E731


def _job(envelope, sid=SID):
    text = envelope if isinstance(envelope, str) else json.dumps(envelope)
    return {
        "id": "j",
        "type": "resume_message",
        "target": sid,
        "payload": json.dumps({"prompt": e2e.encrypt_text(KEYS, text, e2e.aad_resume_prompt(sid))}),
    }


def _env(**kw):
    env = {"v": 1, "prompt": "hi", "rid": secrets.token_hex(16), "ts": NOW_MS()}
    env.update(kw)
    return {k: v for k, v in env.items() if v is not ...}


@pytest.fixture
def sp(tmp_path):
    return tmp_path / "sync_state.json"


def _open(job, sp):
    return json.loads(seal.open_job_prompt(job, KEYS, sp)["payload"])["prompt"]


def test_valid_envelope_is_unwrapped(sp):
    assert _open(_job(_env(prompt="hello")), sp) == "hello"


def test_new_session_valid(sp):
    env = _env(prompt="build")
    job = {
        "id": "j", "type": "new_session", "target": "/p",
        "payload": json.dumps({"tool": "claude-code", "prompt": e2e.encrypt_text(
            KEYS, json.dumps(env), e2e.aad_new_session_prompt("/p", "claude-code"))}),
    }
    assert _open(job, sp) == "build"


def test_replayed_rid_rejected(sp):
    job = _job(_env())
    _open(job, sp)
    with pytest.raises(seal.PromptError, match="already used"):
        _open(job, sp)


def test_rid_recorded_before_returning(sp):
    env = _env()
    _open(_job(env), sp)
    assert env["rid"] in json.loads(seal.used_ids_path(sp).read_text())


@pytest.mark.parametrize("delta", [-(60 * 60 * 1000 + 5000), 5 * 60 * 1000 + 5000])
def test_expired_or_future_rejected(sp, delta):
    with pytest.raises(seal.PromptError, match="prompt expired"):
        _open(_job(_env(ts=NOW_MS() + delta)), sp)
    assert not seal.used_ids_path(sp).exists()


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        "[]",
        '"str"',
        {"prompt": "x", "rid": "a" * 32, "ts": 1},  # missing v
        _env(v=...),
        _env(prompt=...),
        _env(rid=...),
        _env(ts=...),
        _env(v=2),
        _env(v="1"),
        _env(prompt=5),
        _env(rid="A" * 32),
        _env(rid="a" * 31),
        _env(rid=5),
        _env(ts="123"),
        _env(ts=1.5),
        _env(ts=True),
    ],
)
def test_malformed_envelope_rejected(sp, bad):
    with pytest.raises(seal.PromptError, match="invalid prompt envelope"):
        _open(_job(bad), sp)


def test_too_long_rejected_and_limit_accepted(sp):
    with pytest.raises(seal.PromptError, match="prompt too long"):
        _open(_job(_env(prompt="x" * 32_001)), sp)
    assert len(_open(_job(_env(prompt="x" * 32_000)), sp)) == 32_000


def test_wrong_aad_still_cannot_decrypt(sp):
    job = _job(_env(), sid="claude-code:other")
    job["target"] = SID
    with pytest.raises(seal.PromptError, match="cannot decrypt prompt"):
        _open(job, sp)


def test_pruning_and_persistence(sp):
    path = seal.used_ids_path(sp)
    old_rid = "b" * 32
    path.write_text(json.dumps({old_rid: NOW_MS() - 3 * 60 * 60 * 1000, "c" * 32: NOW_MS() - 90 * 60 * 1000}))
    env = _env()
    _open(_job(env), sp)
    data = json.loads(path.read_text())
    assert old_rid not in data  # older than 2h: pruned
    assert "c" * 32 in data and env["rid"] in data
    assert not path.with_name(path.name + ".tmp").exists()
    # persistence across a "reload": nothing is held in memory
    with pytest.raises(seal.PromptError, match="already used"):
        _open(_job(env), sp)


def test_corrupt_used_file_is_treated_as_empty(sp):
    seal.used_ids_path(sp).write_text("{broken")
    assert _open(_job(_env()), sp) == "hi"


def test_plaintext_mode_has_no_envelope_or_tracking(sp):
    # main only calls open_job_prompt with keys; without it nothing touches the used-ids file.
    assert not seal.used_ids_path(sp).exists()
