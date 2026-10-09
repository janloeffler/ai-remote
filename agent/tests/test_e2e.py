import json
from pathlib import Path

import pytest

from agent import e2e

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "e2e_vectors.json").read_text(encoding="utf-8"))
FAST = {"alg": "argon2id", "m": 19456, "t": 2, "p": 1, "v": 19}


def _keys(master_b64: str) -> e2e.Keys:
    return e2e.Keys.from_master(e2e.b64d(master_b64))


@pytest.mark.parametrize("case", VECTORS["kdf_cases"], ids=lambda c: c["name"])
def test_kdf_vectors(case):
    master = e2e.derive_master(case["passphrase"], e2e.b64d(case["salt_b64"]), case["kdf"])
    assert e2e.b64e(master) == case["master_b64"]
    assert e2e.Keys.from_master(master).check == case["check_hex"]


@pytest.mark.parametrize("case", VECTORS["cipher_cases"], ids=lambda c: c["name"])
def test_cipher_vectors(case):
    keys = _keys(case["master_b64"])
    out = e2e.encrypt_text(keys, case["plaintext"], case["aad"], e2e.b64d(case["nonce_b64"]))
    assert out == case["ciphertext"]
    assert e2e.is_ciphertext(out)
    assert e2e.decrypt_text(keys, case["ciphertext"], case["aad"]) == case["plaintext"]


def test_binary_vector():
    case = VECTORS["binary_cases"][0]
    keys = _keys(case["master_b64"])
    data = e2e.b64d(case["bytes_b64"])
    blob = e2e.encrypt_bytes(keys, data, case["aad"], e2e.b64d(case["nonce_b64"]))
    assert e2e.b64e(blob) == case["ciphertext_b64"]
    assert blob.startswith(b"e2e1")
    assert e2e.decrypt_bytes(keys, blob, case["aad"]) == data


@pytest.fixture
def keys():
    return e2e.Keys.from_master(bytes(range(32)))


def test_roundtrip_random_nonce(keys):
    a = e2e.encrypt_text(keys, "hällo 🚀", "x")
    b = e2e.encrypt_text(keys, "hällo 🚀", "x")
    assert a != b
    assert e2e.decrypt_text(keys, a, "x") == "hällo 🚀"
    img = e2e.encrypt_bytes(keys, b"\x00\x01", "y")
    assert e2e.decrypt_bytes(keys, img, "y") == b"\x00\x01"


def test_wrong_aad_tamper_wrong_key(keys):
    ct = e2e.encrypt_text(keys, "secret", "aad1")
    with pytest.raises(ValueError):
        e2e.decrypt_text(keys, ct, "aad2")
    flipped = ct[:-2] + ("A" if ct[-2] != "A" else "B") + ct[-1]
    with pytest.raises(ValueError):
        e2e.decrypt_text(keys, flipped, "aad1")
    with pytest.raises(ValueError):
        e2e.decrypt_text(e2e.Keys.from_master(bytes(32)), ct, "aad1")
    blob = bytearray(e2e.encrypt_bytes(keys, b"data", "a"))
    blob[-1] ^= 1
    with pytest.raises(ValueError):
        e2e.decrypt_bytes(keys, bytes(blob), "a")
    with pytest.raises(ValueError):
        e2e.decrypt_bytes(keys, b"nope" + bytes(40), "a")


def test_decrypt_rejects_bad_format(keys):
    for bad in ("plain", "e2e1:", "e2e1:short", "e2e2:" + "A" * 40):
        with pytest.raises(ValueError):
            e2e.decrypt_text(keys, bad, "a")


def test_is_ciphertext():
    assert e2e.is_ciphertext("e2e1:" + "A" * 38)
    assert e2e.is_ciphertext("e2e1:" + "a-_9" * 20)
    assert not e2e.is_ciphertext("e2e1:" + "A" * 37)
    assert not e2e.is_ciphertext("e2e1:" + "A" * 38 + "=")
    assert not e2e.is_ciphertext("e2e1:" + "A" * 38 + "\n")
    assert not e2e.is_ciphertext("E2E1:" + "A" * 38)
    assert not e2e.is_ciphertext("hello")
    assert not e2e.is_ciphertext("")


@pytest.mark.parametrize(
    "bad",
    [
        {**FAST, "alg": "argon2i"},
        {**FAST, "m": 19455},
        {**FAST, "m": 1048577},
        {**FAST, "t": 1},
        {**FAST, "t": 11},
        {**FAST, "p": 0},
        {**FAST, "p": 5},
        {**FAST, "v": 16},
        {"alg": "argon2id"},
        {**FAST, "m": "19456"},
    ],
)
def test_derive_master_rejects_bad_kdf(bad):
    with pytest.raises(ValueError):
        e2e.derive_master("pw", bytes(16), bad)


def test_master_encoding():
    m = bytes(range(32))
    assert e2e.decode_master(e2e.encode_master(m)) == m
    with pytest.raises(ValueError):
        e2e.decode_master(e2e.b64e(b"short"))
    with pytest.raises(ValueError):
        e2e.decode_master("not base64!!")


def test_aad_helpers():
    assert e2e.aad_title("s") == "session|s|title"
    assert e2e.aad_preview("s") == "session|s|preview"
    assert e2e.aad_message("s", 3) == "msg|s|3"
    assert e2e.aad_resume_prompt("s") == "job-prompt|resume_message|s"
    assert e2e.aad_new_session_prompt("/p", "claude_code") == "job-prompt|new_session|/p|claude_code"
    assert e2e.AAD_SEARCH == "job-prompt|search"
    assert e2e.aad_job_result(7) == "job-result|7"
    assert e2e.aad_image("s", "k") == "image|s|k"
    assert len(e2e.new_salt()) == 16
