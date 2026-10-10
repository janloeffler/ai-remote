"""Optional end-to-end encryption: server-side state, mode switch and validation.

In E2E mode the server stores and relays ciphertext only; the key never reaches it. This
module owns what the server *does* know: the mode, the (non-secret) KDF parameters and
key-check value the agent registers, a data epoch that changes on every wipe, and the
checks that keep plaintext out of the database. See
docs/superpowers/specs/2026-10-09-e2e-encryption-design.md.
"""

import base64
import binascii
import hmac
import json
import logging
import re
import sqlite3
import time
import uuid
from pathlib import Path

from . import db, images

logger = logging.getLogger("ai_remote.e2e")

# 12-byte nonce + 16-byte GCM tag = 28 bytes = 38 base64url characters, minimum.
CIPHERTEXT_RE = re.compile(r"e2e1:[A-Za-z0-9_-]{38,}")
IMAGE_MAGIC = b"e2e1"
IMAGE_OVERHEAD = 32  # magic + nonce + tag = 32 bytes: the ciphertext of a max-size image
KEY_CHECK_RE = re.compile(r"[0-9a-f]{64}")
KDF_RANGES = {"m": (19456, 1048576), "t": (2, 10), "p": (1, 4)}
CHECKPOINT_ATTEMPTS = 5
CHECKPOINT_BACKOFF_SECONDS = 0.2


class ConflictError(Exception):
    """The request contradicts the server's current state (HTTP 409)."""


def is_ciphertext(value) -> bool:
    return isinstance(value, str) and CIPHERTEXT_RE.fullmatch(value) is not None


def check_agent_mode(header: str | None, enabled: bool) -> None:
    """The agent declares its mode (X-AI-Remote-E2E: 1/0); a mismatch is a 409.

    No content sniffing: a plaintext chat may legitimately start with "e2e1:". A missing
    header is a legacy agent and is accepted.
    """
    if header is None:
        return
    value = header.strip()
    if value not in ("0", "1"):
        raise ValueError("invalid X-AI-Remote-E2E header")
    if enabled and value == "0":
        raise ConflictError("agent is in plaintext mode but the server is in E2E mode")
    if not enabled and value == "1":
        raise ConflictError("agent is in E2E mode but the server is not")


def valid_content(value: str, enabled: bool, allow_empty: bool = False) -> bool:
    """Whether an ingested text field fits the mode. E2E: ciphertext only; plaintext: anything."""
    if enabled:
        return is_ciphertext(value) or (allow_empty and value == "")
    return True


def new_epoch() -> str:
    return uuid.uuid4().hex


def ensure_state_row(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR IGNORE INTO e2e_state (id, mode, data_epoch) VALUES (1, NULL, ?)", (new_epoch(),))
    conn.commit()


def get_state(conn: sqlite3.Connection) -> dict:
    query = "SELECT mode, salt, kdf, key_check, data_epoch FROM e2e_state WHERE id = 1"
    row = conn.execute(query).fetchone()
    if row is None:  # init_db creates it; this only covers a connection that skipped init_db
        ensure_state_row(conn)
        row = conn.execute(query).fetchone()
    return {
        "mode": row["mode"] or "plain",
        "salt": row["salt"],
        "kdf": json.loads(row["kdf"]) if row["kdf"] else None,
        "key_check": row["key_check"],
        "epoch": row["data_epoch"],
    }


def binding() -> str:
    """Fingerprint of API_KEY for the browser: rotating API_KEY invalidates stored keys."""
    from . import settings

    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"), b"e2e-binding:" + settings.API_KEY.encode("utf-8"), "sha256"
    ).hexdigest()


def handshake_payload(conn: sqlite3.Connection) -> dict:
    state = get_state(conn)
    e2e = state["mode"] == "e2e"
    return {
        "e2e": e2e,
        "epoch": state["epoch"],
        "salt": state["salt"] if e2e else None,
        "kdf": state["kdf"] if e2e else None,
        "key_check": state["key_check"] if e2e else None,
    }


def browser_config(conn: sqlite3.Connection) -> dict:
    """What authenticated pages embed for the browser (all non-secret)."""
    state = get_state(conn)
    return {
        "enabled": True,
        "salt": state["salt"],
        "kdf": state["kdf"],
        "key_check": state["key_check"],
        "binding": binding(),
    }


def _search_index_ddl() -> str:
    match = re.search(
        r"CREATE VIRTUAL TABLE IF NOT EXISTS search_index.*?;", db.SCHEMA_PATH.read_text(), re.DOTALL
    )
    if match is None:  # pragma: no cover - schema.sql is part of this repo
        raise RuntimeError("search_index definition missing from schema.sql")
    return match.group(0)


def wipe_content(conn: sqlite3.Connection, clear_params: bool = False) -> None:
    """Removes every cached chat byte the server holds. Raises if anything survives.

    ``clear_params`` also nulls salt/kdf/key_check in the same transaction as the deletes and the
    epoch bump (passphrase reset): agents then fail closed until new params are stored.
    """
    conn.commit()
    conn.execute("PRAGMA secure_delete = ON")
    for table in ("messages", "sessions", "jobs", "images"):
        conn.execute(f"DELETE FROM {table}")
    # New epoch in the same transaction as the deletes: once rows are gone the agent must resync,
    # even if a later step (VACUUM, WAL truncation) fails and raises.
    if clear_params:
        conn.execute(
            "UPDATE e2e_state SET data_epoch = ?, salt = NULL, kdf = NULL, key_check = NULL WHERE id = 1",
            (new_epoch(),),
        )
    else:
        conn.execute("UPDATE e2e_state SET data_epoch = ? WHERE id = 1", (new_epoch(),))
    # FTS5 deletes leave tombstoned plaintext in the segment shadow tables; dropping the
    # virtual table removes them.
    conn.execute("DROP TABLE IF EXISTS search_index")
    conn.execute(_search_index_ddl())
    conn.execute("DROP TABLE IF EXISTS jobs_old")  # leftover of a crashed jobs migration
    directory = images.image_dir()
    if directory.is_dir():
        for entry in directory.iterdir():
            if entry.is_file() or entry.is_symlink():
                entry.unlink()
    conn.commit()
    conn.execute("VACUUM")  # cannot run inside a transaction
    _truncate_wal(conn)
    leftovers = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("messages", "sessions", "jobs", "images", "search_index")
    }
    freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    remaining_files = [e for e in directory.iterdir()] if directory.is_dir() else []
    if any(leftovers.values()) or freelist or remaining_files:
        raise RuntimeError(f"wipe incomplete: rows={leftovers} freelist={freelist} files={len(remaining_files)}")


def _truncate_wal(conn: sqlite3.Connection) -> None:
    """Checkpoints and truncates the WAL, verifying it: a reader holding an old snapshot makes
    wal_checkpoint report busy and leaves the deleted plaintext in the -wal file."""
    db_file = next((r["file"] for r in conn.execute("PRAGMA database_list") if r["name"] == "main"), "")
    wal = Path(db_file + "-wal") if db_file else None
    previous_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.execute("PRAGMA busy_timeout = 200")
    try:
        for attempt in range(CHECKPOINT_ATTEMPTS):
            busy, log, checkpointed = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[:3]
            wal_empty = wal is None or not wal.exists() or wal.stat().st_size == 0
            if busy == 0 and log == checkpointed and wal_empty:
                return
            if attempt < CHECKPOINT_ATTEMPTS - 1:
                time.sleep(CHECKPOINT_BACKOFF_SECONDS)
    finally:
        conn.execute(f"PRAGMA busy_timeout = {int(previous_timeout)}")
    raise RuntimeError(f"wipe incomplete: WAL checkpoint blocked (busy={busy} log={log} checkpointed={checkpointed})")


def _start_new_epoch(conn: sqlite3.Connection, mode: str | None = None) -> None:
    if mode is None:
        conn.execute("UPDATE e2e_state SET data_epoch = ? WHERE id = 1", (new_epoch(),))
    else:
        conn.execute("UPDATE e2e_state SET mode = ?, data_epoch = ? WHERE id = 1", (mode, new_epoch()))
    conn.commit()


def reconcile_mode(conn: sqlite3.Connection, enabled: bool) -> None:
    """Startup: when the configured mode differs from the stored one, wipe and switch."""
    wanted = "e2e" if enabled else "plain"
    stored_raw = conn.execute("SELECT mode FROM e2e_state WHERE id = 1").fetchone()["mode"]
    stored = stored_raw or "plain"
    if stored == wanted:
        if stored_raw is None:  # legacy database: record the mode, nothing to wipe
            conn.execute("UPDATE e2e_state SET mode = ? WHERE id = 1", (wanted,))
            conn.commit()
        return
    had_data = any(
        conn.execute(f"SELECT EXISTS (SELECT 1 FROM {table})").fetchone()[0]
        for table in ("messages", "sessions", "jobs", "images")
    )
    wipe_content(conn)
    _start_new_epoch(conn, wanted)
    if had_data:
        logger.warning(
            "E2E mode changed %s->%s: server cache dropped, job history deleted, agent will resync. "
            "Delete old backups of data/ yourself — they still contain plaintext.",
            stored,
            wanted,
        )
    else:
        logger.info("E2E mode set to %s (empty database)", wanted)


def _validate_params(salt, kdf, key_check) -> dict:
    """Returns the normalized kdf dict; raises ValueError describing the first problem."""
    if not isinstance(salt, str):
        raise ValueError("salt must be a string")
    try:
        raw = base64.b64decode(salt, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("salt must be standard base64") from None
    if not 16 <= len(raw) <= 64:
        raise ValueError("salt must decode to 16-64 bytes")
    if not isinstance(kdf, dict) or kdf.get("alg") != "argon2id":
        raise ValueError("kdf.alg must be argon2id")
    if set(kdf) - {"alg", "m", "t", "p", "v"}:
        raise ValueError("unknown kdf fields")
    for name, (low, high) in KDF_RANGES.items():
        value = kdf.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"kdf.{name} must be an integer between {low} and {high}")
    if type(kdf.get("v")) is not int or kdf["v"] != 19:
        raise ValueError("kdf.v must be 19")
    if not isinstance(key_check, str) or KEY_CHECK_RE.fullmatch(key_check) is None:
        raise ValueError("key_check must be 64 lowercase hex characters")
    return dict(kdf)


def set_params(conn: sqlite3.Connection, salt, kdf, key_check, reset: bool = False) -> None:
    """Registers the agent's KDF parameters. ValueError = invalid (422), ConflictError = 409."""
    state = get_state(conn)
    if state["mode"] != "e2e":
        raise ConflictError("server is not in E2E mode")
    kdf = _validate_params(salt, kdf, key_check)
    is_set = state["salt"] is not None
    if is_set and (state["salt"], state["kdf"], state["key_check"]) == (salt, kdf, key_check):
        return
    if is_set:
        if not reset:
            raise ConflictError("E2E parameters already set; pass reset=true to replace them (wipes all data)")
        wipe_content(conn, clear_params=True)
        logger.warning("E2E parameters reset: server cache dropped, job history deleted, agent will resync.")
    # On reset the wipe already bumped the epoch and nulled the old params in one transaction, so an
    # agent never sees the new epoch together with the old key_check.
    conn.execute(
        "UPDATE e2e_state SET salt = ?, kdf = ?, key_check = ? WHERE id = 1",
        (salt, json.dumps(kdf, sort_keys=True), key_check),
    )
    conn.commit()
