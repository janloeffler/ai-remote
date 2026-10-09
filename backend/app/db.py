import os
import sqlite3
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

_SORT_CLAUSES = {
    "date_desc": "last_updated_at DESC",
    "date_asc": "last_updated_at ASC",
    "title_asc": "title COLLATE NOCASE ASC",
    "path_asc": "project_path COLLATE NOCASE ASC",
}


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers (the chat list) proceed while the agent writes
    # (sync/job completion) instead of every connection serializing on a
    # single file lock — without it, concurrent access reliably produced
    # "database is locked" (seen in production: /jobs/pending 500s that
    # skipped record_agent_contact, making the UI report the Mac as
    # unreachable even while the agent was running fine).
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    _migrate_add_loaded_message_count(conn)
    _migrate_add_active_until(conn)
    _migrate_add_poll_interval_overrides(conn)
    _migrate_jobs_allow_fetch_image(conn)


def _migrate_add_loaded_message_count(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "loaded_message_count" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN loaded_message_count INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "UPDATE sessions SET loaded_message_count = "
            "(SELECT COUNT(*) FROM messages m WHERE m.session_id = sessions.id)"
        )
        conn.commit()


def _migrate_add_active_until(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(agent_status)").fetchall()}
    if "active_until" not in columns:
        conn.execute("ALTER TABLE agent_status ADD COLUMN active_until TEXT")
        conn.commit()


def _migrate_add_poll_interval_overrides(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    for name in ("poll_interval_default_seconds", "poll_interval_active_seconds"):
        if name not in columns:
            conn.execute(f"ALTER TABLE settings ADD COLUMN {name} INTEGER")
    conn.commit()


def _migrate_jobs_allow_fetch_image(conn: sqlite3.Connection) -> None:
    """SQLite can't alter a CHECK constraint, so an older jobs table is rebuilt."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'").fetchone()
    if row is None or "fetch_image" in row["sql"]:
        return
    conn.execute("ALTER TABLE jobs RENAME TO jobs_old")
    conn.executescript(SCHEMA_PATH.read_text())
    conn.execute("INSERT INTO jobs SELECT * FROM jobs_old")
    conn.execute("DROP TABLE jobs_old")
    conn.commit()


def get_db_dependency():
    conn = get_connection(os.environ.get("DATABASE_PATH", "app.db"))
    try:
        yield conn
    finally:
        conn.close()


def upsert_session(conn: sqlite3.Connection, session: dict) -> None:
    conn.execute(
        """
        INSERT INTO sessions (id, tool, entrypoint, project_path, title, created_at,
                               last_updated_at, message_count, last_message_preview, status)
        VALUES (:id, :tool, :entrypoint, :project_path, :title, :created_at,
                :last_updated_at, :message_count, :last_message_preview, :status)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            last_updated_at = excluded.last_updated_at,
            message_count = excluded.message_count,
            last_message_preview = excluded.last_message_preview,
            project_path = excluded.project_path,
            status = excluded.status,
            full_content_synced = CASE
                WHEN excluded.last_updated_at != sessions.last_updated_at THEN 0
                ELSE sessions.full_content_synced
            END
        """,
        session,
    )
    conn.execute(
        "DELETE FROM search_index WHERE session_id = ? AND kind = 'header'",
        (session["id"],),
    )
    conn.execute(
        "INSERT INTO search_index (session_id, kind, text) VALUES (?, 'header', ?)",
        (session["id"], f"{session['title']} {session['last_message_preview']}"),
    )
    conn.commit()


def _date_group_bounds(group: str) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if group == "today":
        start, end = today_start, today_start + timedelta(days=1)
    elif group == "yesterday":
        start, end = today_start - timedelta(days=1), today_start
    elif group == "week":
        start, end = today_start - timedelta(days=7), today_start - timedelta(days=1)
    else:  # "older"
        start, end = datetime.min.replace(tzinfo=timezone.utc), today_start - timedelta(days=7)
    return start.isoformat(), end.isoformat()


def get_sessions(
    conn: sqlite3.Connection,
    tool: str | None = None,
    tools: Sequence[str] | None = None,
    project: str | None = None,
    date_group: str | None = None,
    q: str | None = None,
    limit: int = 100,
    sort: str = "date_desc",
) -> list[dict]:
    ids_filter = None
    if q:
        safe_q = '"' + q.replace('"', '""') + '"'
        rows = conn.execute(
            "SELECT DISTINCT session_id FROM search_index WHERE search_index MATCH ?",
            (safe_q,),
        ).fetchall()
        ids_filter = {row["session_id"] for row in rows}
        if not ids_filter:
            return []

    query = "SELECT * FROM sessions WHERE 1=1"
    params: list = []
    if tools is not None:
        if not tools:
            return []
        query += f" AND tool IN ({','.join('?' * len(tools))})"
        params.extend(tools)
    if tool:
        query += " AND tool = ?"
        params.append(tool)
    if project:
        query += " AND project_path LIKE ?"
        params.append(f"%{project}%")
    if date_group:
        start, end = _date_group_bounds(date_group)
        query += " AND last_updated_at >= ? AND last_updated_at < ?"
        params.extend([start, end])
    query += f" ORDER BY {_SORT_CLAUSES.get(sort, _SORT_CLAUSES['date_desc'])}"

    # Only push LIMIT into SQL for the common unfiltered-by-search browsing path.
    # When a search query narrows results via the FTS ids_filter (applied in
    # Python below), an unconditional SQL LIMIT here would truncate the
    # unfiltered row set before matching against ids_filter and could silently
    # drop real matches that aren't among the most recent `limit` sessions.
    if ids_filter is None:
        query += " LIMIT ?"
        params.append(limit)

    rows = [dict(row) for row in conn.execute(query, params).fetchall()]
    if ids_filter is not None:
        rows = [row for row in rows if row["id"] in ids_filter]
        rows = rows[:limit]
    return rows


def get_distinct_project_paths(conn: sqlite3.Connection, tools: Sequence[str] | None = None) -> list[str]:
    query = "SELECT DISTINCT project_path FROM sessions WHERE project_path != ''"
    params: list = []
    if tools is not None:
        if not tools:
            return []
        query += f" AND tool IN ({','.join('?' * len(tools))})"
        params.extend(tools)
    rows = conn.execute(query + " ORDER BY project_path", params).fetchall()
    return [row["project_path"] for row in rows]


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def get_messages(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT idx, role, timestamp, content FROM messages WHERE session_id = ? ORDER BY idx",
        (session_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def replace_messages(
    conn: sqlite3.Connection, session_id: str, messages: list[dict], is_complete: bool | None = None
) -> None:
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM search_index WHERE session_id = ? AND kind = 'message'", (session_id,))
    for m in messages:
        conn.execute(
            "INSERT INTO messages (session_id, idx, role, timestamp, content) VALUES (?, ?, ?, ?, ?)",
            (session_id, m["idx"], m["role"], m["timestamp"], m["content"]),
        )
        conn.execute(
            "INSERT INTO search_index (session_id, kind, text) VALUES (?, 'message', ?)",
            (session_id, m["content"]),
        )
    if is_complete is None:
        conn.execute(
            "UPDATE sessions SET loaded_message_count = ?, "
            "full_content_synced = CASE WHEN ? >= message_count THEN 1 ELSE 0 END "
            "WHERE id = ?",
            (len(messages), len(messages), session_id),
        )
    else:
        conn.execute(
            "UPDATE sessions SET loaded_message_count = ?, full_content_synced = ? WHERE id = ?",
            (len(messages), int(is_complete), session_id),
        )
    conn.commit()


def apply_recent_messages(conn: sqlite3.Connection, session_id: str, messages: list[dict]) -> None:
    if not messages:
        return
    session = get_session(conn, session_id)
    if session is None or len(messages) < session["loaded_message_count"]:
        return
    replace_messages(conn, session_id, messages)


def create_job(conn: sqlite3.Connection, job_type: str, target: str, payload: str = "") -> str:
    job_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO jobs (id, type, target, payload, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
        (job_id, job_type, target, payload, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return job_id


def claim_pending_jobs(conn: sqlite3.Connection) -> list[dict]:
    query = "SELECT * FROM jobs WHERE status = 'pending'"
    if get_remote_commands_paused(conn):
        query += " AND type NOT IN ('resume_message', 'new_session')"
    rows = conn.execute(query).fetchall()
    claimed = []
    for row in rows:
        cursor = conn.execute(
            "UPDATE jobs SET status = 'running' WHERE id = ? AND status = 'pending'",
            (row["id"],),
        )
        if cursor.rowcount:
            job_dict = dict(row)
            job_dict["status"] = "running"
            claimed.append(job_dict)
    conn.commit()
    return claimed


def complete_job(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    result_text: str,
    messages: list[dict],
    is_complete: bool | None = None,
) -> None:
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        return
    conn.execute(
        "UPDATE jobs SET status = ?, result_text = ?, completed_at = ? WHERE id = ?",
        (status, result_text, datetime.now(timezone.utc).isoformat(), job_id),
    )
    conn.commit()
    if status == "done" and job["type"] == "fetch_full" and messages:
        replace_messages(conn, job["target"], messages, is_complete)


def get_job(conn: sqlite3.Connection, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


_JOB_TIMEOUTS_SECONDS = {"fetch_full": 300, "fetch_image": 300, "resume_message": 1800, "new_session": 1800}


def fail_stale_jobs(conn: sqlite3.Connection) -> None:
    """Fails jobs nobody finished in time — including ones never claimed (agent offline)."""
    now = datetime.now(timezone.utc)
    for job_type, timeout_seconds in _JOB_TIMEOUTS_SECONDS.items():
        cutoff = (now - timedelta(seconds=timeout_seconds)).isoformat()
        conn.execute(
            "UPDATE jobs SET status = 'failed', result_text = 'timed out', completed_at = ? "
            "WHERE status IN ('pending', 'running') AND type = ? AND created_at < ?",
            (now.isoformat(), job_type, cutoff),
        )
    conn.commit()


def record_agent_contact(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE agent_status SET last_contact_at = ? WHERE id = 1",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()


def get_last_agent_contact(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT last_contact_at FROM agent_status WHERE id = 1").fetchone()
    return row["last_contact_at"] if row else None


def bump_active_interval(conn: sqlite3.Connection, duration_min: int) -> None:
    until = (datetime.now(timezone.utc) + timedelta(minutes=duration_min)).isoformat()
    conn.execute("UPDATE agent_status SET active_until = ? WHERE id = 1", (until,))
    conn.commit()


def active_window_open(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT active_until FROM agent_status WHERE id = 1").fetchone()
    active_until = row["active_until"] if row else None
    if not active_until:
        return False
    try:
        return datetime.fromisoformat(active_until) > datetime.now(timezone.utc)
    except ValueError:
        return False


def get_poll_interval_overrides(conn: sqlite3.Connection) -> tuple[int | None, int | None]:
    """The (standard, active) intervals set in the UI; None means "use the env default"."""
    row = conn.execute(
        "SELECT poll_interval_default_seconds, poll_interval_active_seconds FROM settings WHERE id = 1"
    ).fetchone()
    if not row:
        return None, None
    return row["poll_interval_default_seconds"], row["poll_interval_active_seconds"]


def set_poll_interval_overrides(conn: sqlite3.Connection, default_seconds: int | None, active_seconds: int | None) -> None:
    conn.execute(
        "UPDATE settings SET poll_interval_default_seconds = ?, poll_interval_active_seconds = ? WHERE id = 1",
        (default_seconds, active_seconds),
    )
    conn.commit()


def current_poll_interval_seconds(conn: sqlite3.Connection, default_seconds: int, active_seconds: int) -> int:
    """Interval the agent should use now. Arguments are the env defaults; UI overrides win."""
    override_default, override_active = get_poll_interval_overrides(conn)
    default_seconds = override_default if override_default is not None else default_seconds
    active_seconds = override_active if override_active is not None else active_seconds
    return active_seconds if active_window_open(conn) else default_seconds


def get_remote_commands_paused(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT remote_commands_paused FROM settings WHERE id = 1").fetchone()
    return bool(row["remote_commands_paused"]) if row else False


def set_remote_commands_paused(conn: sqlite3.Connection, paused: bool) -> None:
    conn.execute("UPDATE settings SET remote_commands_paused = ? WHERE id = 1", (int(paused),))
    conn.commit()


def get_all_jobs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


def save_image(
    conn: sqlite3.Connection, session_id: str, path_key: str, source_path: str, mime: str, size: int
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO images (session_id, path_key, source_path, mime, size, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, path_key, source_path, mime, size, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def get_image(conn: sqlite3.Connection, session_id: str, path_key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM images WHERE session_id = ? AND path_key = ?", (session_id, path_key)
    ).fetchone()
    return dict(row) if row else None


def get_image_keys(conn: sqlite3.Connection, session_id: str) -> set[str]:
    rows = conn.execute("SELECT path_key FROM images WHERE session_id = ?", (session_id,)).fetchall()
    return {row["path_key"] for row in rows}


def pop_expired_images(conn: sqlite3.Connection, retention_days: int) -> list[dict]:
    """Deletes image rows older than the retention and returns them so files can go too."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    rows = [dict(r) for r in conn.execute("SELECT * FROM images WHERE created_at < ?", (cutoff,)).fetchall()]
    conn.execute("DELETE FROM images WHERE created_at < ?", (cutoff,))
    conn.commit()
    return rows
