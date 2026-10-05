import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

CURSOR_GLOBAL_STORAGE = Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage"
STATE_DB = CURSOR_GLOBAL_STORAGE / "state.vscdb"
CURSOR_WORKSPACE_STORAGE = Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "workspaceStorage"


def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _read_only_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _query_with_retry(conn: sqlite3.Connection, sql: str, params: tuple = (), retries: int = 3, delay: float = 0.2) -> list:
    """Cursor may hold a brief write lock on its own DB (WAL checkpoints) while running.
    Retry a couple of times before giving up, rather than crashing the whole sync cycle."""
    for attempt in range(retries):
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            if attempt == retries - 1:
                raise
            time.sleep(delay)
    return []


def list_cursor_sessions(state_db_path: Path = STATE_DB) -> list[dict]:
    # Deliberately NOT reading conversation-search.db: it's a lazily-built full-text
    # search cache, not a live index — it can silently stop gaining new rows (seen in
    # practice) while conversations keep happening. composerHeaders is the table Cursor
    # itself uses to list your chats, so it's always current.
    if not state_db_path.exists():
        return []
    conn = _read_only_connection(state_db_path)
    try:
        rows = _query_with_retry(
            conn,
            "SELECT composerId, createdAt, lastUpdatedAt, value FROM composerHeaders "
            "WHERE isArchived = 0 AND isSubagent = 0",
        )
    finally:
        conn.close()

    sessions = []
    for composer_id, created_at_ms, updated_at_ms, raw_value in rows:
        if not updated_at_ms:
            continue
        title = None
        if raw_value:
            try:
                title = json.loads(raw_value).get("name")
            except (json.JSONDecodeError, TypeError):
                title = None
        last_updated_at = _ms_to_iso(updated_at_ms)
        sessions.append(
            {
                "id": f"cursor:{composer_id}",
                "tool": "cursor",
                "entrypoint": "cursor",
                "project_path": "",
                "title": title or "(untitled)",
                "created_at": _ms_to_iso(created_at_ms) if created_at_ms else last_updated_at,
                "last_updated_at": last_updated_at,
                "message_count": 0,
                "last_message_preview": "",
                "status": "idle",
            }
        )
    return sessions


def _load_bubbles(conn: sqlite3.Connection, composer_id: str) -> list[dict]:
    prefix = f"bubbleId:{composer_id}:"
    upper_bound = prefix[:-1] + ";"  # ':' -> ';' — next byte after ':', bounds the same prefix as a LIKE '...%' would
    rows = _query_with_retry(
        conn,
        "SELECT key, value FROM cursorDiskKV WHERE key >= ? AND key < ?",
        (prefix, upper_bound),
    )
    bubbles = []
    for key, raw_value in rows:
        try:
            data = json.loads(raw_value)
        except (json.JSONDecodeError, TypeError):
            continue
        text = data.get("text") or ""
        if not text:
            continue
        bubbles.append(
            {
                "key": key,
                "role": "user" if data.get("type") == 1 else "assistant",
                "timestamp": data.get("createdAt") or "",
                "content": text,
            }
        )
    bubbles.sort(key=lambda b: (b.get("timestamp") or "", b["key"]))
    return bubbles


def _load_workspace_id(conn: sqlite3.Connection, composer_id: str) -> str | None:
    rows = _query_with_retry(
        conn, "SELECT workspaceId FROM composerHeaders WHERE composerId = ?", (composer_id,)
    )
    return rows[0][0] if rows else None


def resolve_workspace_path(workspace_id: str, workspace_storage: Path = CURSOR_WORKSPACE_STORAGE) -> str | None:
    workspace_json = workspace_storage / workspace_id / "workspace.json"
    if not workspace_json.exists():
        return None
    try:
        data = json.loads(workspace_json.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    folder_uri = data.get("folder")
    if not folder_uri:
        return None
    return unquote(urlparse(folder_uri).path)


RECENT_MESSAGES_LIMIT = 10


def enrich_with_messages(sessions: list[dict], state_db_path: Path = STATE_DB) -> None:
    if not state_db_path.exists():
        return
    conn = _read_only_connection(state_db_path)
    try:
        for session in sessions:
            composer_id = session["id"].split(":", 1)[1]
            bubbles = _load_bubbles(conn, composer_id)
            if bubbles:
                session["message_count"] = len(bubbles)
                session["last_message_preview"] = bubbles[-1]["content"][:500]
                indexed = [
                    {"idx": i, "role": b["role"], "timestamp": b["timestamp"], "content": b["content"]}
                    for i, b in enumerate(bubbles)
                ]
                session["recent_messages"] = indexed[-RECENT_MESSAGES_LIMIT:]
            workspace_id = _load_workspace_id(conn, composer_id)
            if workspace_id:
                session["project_path"] = resolve_workspace_path(workspace_id, CURSOR_WORKSPACE_STORAGE) or ""
    finally:
        conn.close()


def get_full_messages(composer_id: str, state_db_path: Path = STATE_DB) -> list[dict]:
    if not state_db_path.exists():
        return []
    conn = _read_only_connection(state_db_path)
    try:
        bubbles = _load_bubbles(conn, composer_id)
    finally:
        conn.close()
    return [
        {"idx": i, "role": b["role"], "timestamp": b["timestamp"], "content": b["content"]}
        for i, b in enumerate(bubbles)
    ]


def get_project_path(
    composer_id: str,
    state_db_path: Path = STATE_DB,
    workspace_storage: Path = CURSOR_WORKSPACE_STORAGE,
) -> str | None:
    if not state_db_path.exists():
        return None
    conn = _read_only_connection(state_db_path)
    try:
        workspace_id = _load_workspace_id(conn, composer_id)
    finally:
        conn.close()
    if not workspace_id:
        return None
    return resolve_workspace_path(workspace_id, workspace_storage)
