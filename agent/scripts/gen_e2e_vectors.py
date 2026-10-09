"""Regenerate tests/fixtures/e2e_vectors.json (run from agent/: python scripts/gen_e2e_vectors.py).

The file is consumed by the Python tests and by the browser-crypto tests, so any change
to the scheme must be reflected in both implementations.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import e2e  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "e2e_vectors.json"

PASSPHRASE = "correct horse battery staple ✓ Ünïcode"
FAST_KDF = {"alg": "argon2id", "m": 19456, "t": 2, "p": 1, "v": 19}

TEXTS = [
    ("title", "Ünïcode Tïtle 🚀 日本語", "session|s-1|title"),
    ("empty", "", "msg|s-1|0"),
    ("html", '<p>Hello <b>wörld</b> &amp; <code>a &lt; b</code></p>\n<pre>x = "1"</pre>', "msg|s-1|7"),
    ("preview_json", json.dumps({"text": "hi 👋", "html": "<p>hi 👋</p>"}, ensure_ascii=False), "session|s-1|preview"),
    ("search", "needle ä", "job-prompt|search"),
]
IMAGE_BYTES = bytes(range(256)) + b"\x89PNG\r\n\x1a\n"
IMAGE_AAD = "image|s-1|0123456789abcdef"


def _nonce(i: int) -> bytes:
    return bytes((i * 16 + j) % 256 for j in range(12))


def build() -> dict:
    salt = bytes(range(16))
    kdf_cases = []
    for name, kdf in (("fast", FAST_KDF), ("default", e2e.DEFAULT_KDF)):
        master = e2e.derive_master(PASSPHRASE, salt, kdf)
        kdf_cases.append(
            {
                "name": name,
                "passphrase": PASSPHRASE,
                "salt_b64": e2e.b64e(salt),
                "kdf": kdf,
                "master_b64": e2e.b64e(master),
                "check_hex": e2e.Keys.from_master(master).check,
            }
        )

    master = e2e.derive_master(PASSPHRASE, salt, FAST_KDF)
    keys = e2e.Keys.from_master(master)
    cipher_cases = []
    for i, (name, plaintext, aad) in enumerate(TEXTS):
        nonce = _nonce(i)
        cipher_cases.append(
            {
                "name": name,
                "master_b64": e2e.b64e(master),
                "nonce_b64": e2e.b64e(nonce),
                "aad": aad,
                "plaintext": plaintext,
                "ciphertext": e2e.encrypt_text(keys, plaintext, aad, nonce),
            }
        )
    nonce = _nonce(9)
    binary = {
        "name": "image",
        "master_b64": e2e.b64e(master),
        "nonce_b64": e2e.b64e(nonce),
        "aad": IMAGE_AAD,
        "bytes_b64": e2e.b64e(IMAGE_BYTES),
        "ciphertext_b64": e2e.b64e(e2e.encrypt_bytes(keys, IMAGE_BYTES, IMAGE_AAD, nonce)),
    }
    return {
        "description": (
            "Interop vectors for ai-remote E2E. master = Argon2id(passphrase_utf8, salt, kdf, len 32). "
            "enc_key = HKDF-SHA256(master, salt=empty, info='ai-remote/v1/enc', 32); "
            "check = hex(HKDF-SHA256(master, salt=empty, info='ai-remote/v1/key-check', 32)). "
            "Text ciphertext = 'e2e1:' + base64url_nopad(nonce12 || AES-256-GCM(enc_key, nonce, utf8(plaintext), aad=utf8(aad))). "
            "Binary ciphertext = 'e2e1' || nonce12 || AES-256-GCM(...). All *_b64 fields are standard padded base64."
        ),
        "kdf_cases": kdf_cases,
        "cipher_cases": cipher_cases,
        "binary_cases": [binary],
    }


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
