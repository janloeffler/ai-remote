"""End-to-end encryption primitives (spec: docs/superpowers/specs/2026-10-09-e2e-encryption-design.md).

The server only ever sees ciphertext. The browser implements the same scheme with
hash-wasm + WebCrypto; tests/fixtures/e2e_vectors.json pins both to identical bytes.
"""

import base64
import os
import re
from dataclasses import dataclass

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

DEFAULT_KDF = {"alg": "argon2id", "m": 65536, "t": 3, "p": 1, "v": 19}

TEXT_PREFIX = "e2e1:"
BIN_MAGIC = b"e2e1"
NONCE_LEN = 12
MASTER_LEN = 32

_INFO_ENC = b"ai-remote/v1/enc"
_INFO_CHECK = b"ai-remote/v1/key-check"
_CIPHERTEXT_RE = re.compile(r"[A-Za-z0-9_-]{38,}")

AAD_SEARCH = "job-prompt|search"


def new_salt() -> bytes:
    return os.urandom(16)


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def validate_kdf(kdf: dict) -> None:
    """Same ranges the server enforces; reject anything else before deriving."""
    try:
        ok = (
            kdf["alg"] == "argon2id"
            and kdf["v"] == 19
            and 19456 <= kdf["m"] <= 1048576
            and 2 <= kdf["t"] <= 10
            and 1 <= kdf["p"] <= 4
            and all(isinstance(kdf[k], int) and not isinstance(kdf[k], bool) for k in ("m", "t", "p", "v"))
        )
    except (KeyError, TypeError):
        ok = False
    if not ok:
        raise ValueError("invalid KDF parameters")


def derive_master(passphrase: str, salt: bytes, kdf: dict) -> bytes:
    validate_kdf(kdf)
    return hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=kdf["t"],
        memory_cost=kdf["m"],
        parallelism=kdf["p"],
        hash_len=MASTER_LEN,
        type=Type.ID,
        version=kdf["v"],
    )


def encode_master(master: bytes) -> str:
    return b64e(master)


def decode_master(value: str) -> bytes:
    try:
        master = b64d(value.strip())
    except ValueError as exc:
        raise ValueError("master key is not valid base64") from exc
    if len(master) != MASTER_LEN:
        raise ValueError("master key must be 32 bytes")
    return master


def _hkdf(master: bytes, info: bytes) -> bytes:
    # salt=None means HashLen zero bytes (RFC 5869), identical to WebCrypto HKDF with
    # salt = new Uint8Array(0) — both sides therefore derive the same keys.
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(master)


@dataclass(frozen=True)
class Keys:
    aead: AESGCM
    check: str

    @classmethod
    def from_master(cls, master: bytes) -> "Keys":
        if len(master) != MASTER_LEN:
            raise ValueError("master key must be 32 bytes")
        return cls(aead=AESGCM(_hkdf(master, _INFO_ENC)), check=_hkdf(master, _INFO_CHECK).hex())


def _seal(keys: Keys, data: bytes, aad: str, nonce: bytes | None) -> bytes:
    nonce = nonce if nonce is not None else os.urandom(NONCE_LEN)
    if len(nonce) != NONCE_LEN:
        raise ValueError("nonce must be 12 bytes")
    return nonce + keys.aead.encrypt(nonce, data, aad.encode("utf-8"))


def _open(keys: Keys, blob: bytes, aad: str) -> bytes:
    if len(blob) < NONCE_LEN + 16:
        raise ValueError("ciphertext too short")
    try:
        return keys.aead.decrypt(blob[:NONCE_LEN], blob[NONCE_LEN:], aad.encode("utf-8"))
    except Exception as exc:  # cryptography raises InvalidTag without a message
        raise ValueError("decryption failed") from exc


def is_ciphertext(value: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(TEXT_PREFIX)
        and _CIPHERTEXT_RE.fullmatch(value[len(TEXT_PREFIX):]) is not None
    )


def encrypt_text(keys: Keys, plaintext: str, aad: str, _nonce: bytes | None = None) -> str:
    return TEXT_PREFIX + _b64url_encode(_seal(keys, plaintext.encode("utf-8"), aad, _nonce))


def decrypt_text(keys: Keys, value: str, aad: str) -> str:
    if not is_ciphertext(value):
        raise ValueError("not an e2e1 ciphertext")
    try:
        blob = _b64url_decode(value[len(TEXT_PREFIX):])
    except ValueError as exc:
        raise ValueError("invalid ciphertext encoding") from exc
    try:
        return _open(keys, blob, aad).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("decrypted text is not UTF-8") from exc


def encrypt_bytes(keys: Keys, data: bytes, aad: str, _nonce: bytes | None = None) -> bytes:
    return BIN_MAGIC + _seal(keys, data, aad, _nonce)


def decrypt_bytes(keys: Keys, blob: bytes, aad: str) -> bytes:
    if not blob.startswith(BIN_MAGIC):
        raise ValueError("not an e2e1 binary ciphertext")
    return _open(keys, blob[len(BIN_MAGIC):], aad)


def aad_title(sid: str) -> str:
    return f"session|{sid}|title"


def aad_preview(sid: str) -> str:
    return f"session|{sid}|preview"


def aad_message(sid: str, idx: int) -> str:
    return f"msg|{sid}|{idx}"


def aad_resume_prompt(sid: str) -> str:
    return f"job-prompt|resume_message|{sid}"


def aad_new_session_prompt(project_path: str, tool: str) -> str:
    return f"job-prompt|new_session|{project_path}|{tool}"


def aad_job_result(job_id) -> str:
    return f"job-result|{job_id}"


def aad_image(sid: str, path_key: str) -> str:
    return f"image|{sid}|{path_key}"
