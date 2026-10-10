from datetime import datetime, timedelta, timezone

from app import db


def test_init_db_creates_expected_tables(conn):
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"sessions", "messages", "jobs"} <= tables


def _sample_session(session_id="claude-code:abc"):
    return {
        "id": session_id,
        "tool": "claude-code",
        "entrypoint": "cli",
        "project_path": "/Users/jan/source/demo",
        "title": "Demo session",
        "created_at": "2026-08-01T10:00:00Z",
        "last_updated_at": "2026-08-01T10:05:00Z",
        "message_count": 2,
        "last_message_preview": "done",
        "status": "idle",
    }


def test_upsert_and_get_session(conn):
    db.upsert_session(conn, _sample_session())
    fetched = db.get_session(conn, "claude-code:abc")
    assert fetched["title"] == "Demo session"
    assert fetched["full_content_synced"] == 0


def test_get_sessions_filters_by_tool_and_search(conn):
    db.upsert_session(conn, _sample_session("claude-code:abc"))
    db.upsert_session(conn, _sample_session("cursor:xyz") | {"tool": "cursor", "id": "cursor:xyz", "title": "Other"})

    assert [s["id"] for s in db.get_sessions(conn, tool="cursor")] == ["cursor:xyz"]
    assert [s["id"] for s in db.get_sessions(conn, q="Demo")] == ["claude-code:abc"]
    assert db.get_sessions(conn, q="nonexistent") == []


def test_get_sessions_filters_by_date_group(conn):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(minutes=5)).isoformat()
    old = (now - timedelta(days=15)).isoformat()

    db.upsert_session(
        conn,
        _sample_session("claude-code:today") | {"id": "claude-code:today", "last_updated_at": recent},
    )
    db.upsert_session(
        conn,
        _sample_session("claude-code:old") | {"id": "claude-code:old", "last_updated_at": old},
    )

    assert [s["id"] for s in db.get_sessions(conn, date_group="today")] == ["claude-code:today"]
    assert [s["id"] for s in db.get_sessions(conn, date_group="older")] == ["claude-code:old"]


def test_job_lifecycle_and_message_replacement(conn):
    db.upsert_session(conn, _sample_session())
    job_id = db.create_job(conn, "fetch_full", "claude-code:abc")

    pending = db.claim_pending_jobs(conn)
    assert pending[0]["id"] == job_id
    assert pending[0]["status"] == "running"
    assert db.claim_pending_jobs(conn) == []  # already claimed, no double pickup

    messages = [
        {"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": "hi"},
        {"idx": 1, "role": "assistant", "timestamp": "2026-08-01T10:01:00Z", "content": "hello"},
    ]
    db.complete_job(conn, job_id, "done", "", messages)

    job = db.get_job(conn, job_id)
    assert job["status"] == "done"
    stored = db.get_messages(conn, "claude-code:abc")
    assert stored == messages
    assert db.get_session(conn, "claude-code:abc")["full_content_synced"] == 1


def test_agent_contact_starts_unset_then_records_timestamp(conn):
    assert db.get_last_agent_contact(conn) is None
    db.record_agent_contact(conn)
    contact = db.get_last_agent_contact(conn)
    assert contact is not None


def test_upsert_session_resets_full_content_synced_when_last_updated_at_changes(conn):
    db.upsert_session(conn, _sample_session())
    db.replace_messages(
        conn,
        "claude-code:abc",
        [
            {"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": "hi"},
            {"idx": 1, "role": "assistant", "timestamp": "2026-08-01T10:01:00Z", "content": "hello"},
        ],
    )
    assert db.get_session(conn, "claude-code:abc")["full_content_synced"] == 1

    # Re-upsert with the SAME last_updated_at: no spurious reset.
    db.upsert_session(conn, _sample_session())
    assert db.get_session(conn, "claude-code:abc")["full_content_synced"] == 1

    # Re-upsert with a NEW last_updated_at: the cached full-message view is stale now.
    db.upsert_session(
        conn,
        _sample_session() | {"last_updated_at": "2026-08-02T10:05:00Z"},
    )
    assert db.get_session(conn, "claude-code:abc")["full_content_synced"] == 0


def test_get_sessions_limit_truncates_most_recent_first(conn):
    now = datetime.now(timezone.utc)
    for i in range(5):
        ts = (now - timedelta(minutes=i)).isoformat()
        db.upsert_session(
            conn,
            _sample_session(f"claude-code:s{i}") | {"id": f"claude-code:s{i}", "last_updated_at": ts},
        )

    limited = db.get_sessions(conn, limit=2)
    assert len(limited) == 2
    assert [s["id"] for s in limited] == ["claude-code:s0", "claude-code:s1"]


def test_get_sessions_sort_orders(conn):
    db.upsert_session(
        conn,
        _sample_session("claude-code:b")
        | {"id": "claude-code:b", "title": "Banana", "project_path": "/z/path", "last_updated_at": "2026-08-01T10:00:00Z"},
    )
    db.upsert_session(
        conn,
        _sample_session("claude-code:a")
        | {"id": "claude-code:a", "title": "Apple", "project_path": "/a/path", "last_updated_at": "2026-08-02T10:00:00Z"},
    )

    assert [s["id"] for s in db.get_sessions(conn, sort="date_desc")] == ["claude-code:a", "claude-code:b"]
    assert [s["id"] for s in db.get_sessions(conn, sort="date_asc")] == ["claude-code:b", "claude-code:a"]
    assert [s["id"] for s in db.get_sessions(conn, sort="title_asc")] == ["claude-code:a", "claude-code:b"]
    assert [s["id"] for s in db.get_sessions(conn, sort="path_asc")] == ["claude-code:a", "claude-code:b"]
    assert [s["id"] for s in db.get_sessions(conn, sort="unknown-value")] == ["claude-code:a", "claude-code:b"]


def test_get_distinct_project_paths(conn):
    db.upsert_session(conn, _sample_session("claude-code:a") | {"id": "claude-code:a", "project_path": "/a/path"})
    db.upsert_session(conn, _sample_session("claude-code:b") | {"id": "claude-code:b", "project_path": "/b/path"})
    db.upsert_session(conn, _sample_session("claude-code:c") | {"id": "claude-code:c", "project_path": "/a/path"})

    assert db.get_distinct_project_paths(conn) == ["/a/path", "/b/path"]


def test_replace_messages_sets_loaded_count_and_partial_sync_flag(conn):
    db.upsert_session(conn, _sample_session() | {"message_count": 5})
    db.replace_messages(
        conn,
        "claude-code:abc",
        [
            {"idx": 3, "role": "user", "timestamp": "2026-08-01T10:03:00Z", "content": "d"},
            {"idx": 4, "role": "assistant", "timestamp": "2026-08-01T10:04:00Z", "content": "e"},
        ],
    )
    session = db.get_session(conn, "claude-code:abc")
    assert session["loaded_message_count"] == 2
    assert session["full_content_synced"] == 0  # 2 loaded < 5 total

    db.replace_messages(
        conn,
        "claude-code:abc",
        [
            {"idx": i, "role": "user", "timestamp": f"2026-08-01T10:0{i}:00Z", "content": str(i)}
            for i in range(5)
        ],
    )
    session = db.get_session(conn, "claude-code:abc")
    assert session["loaded_message_count"] == 5
    assert session["full_content_synced"] == 1  # 5 loaded >= 5 total


def test_migration_adds_loaded_message_count_to_pre_existing_db_and_backfills(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    legacy_conn = db.get_connection(db_path)
    legacy_conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            tool TEXT NOT NULL,
            entrypoint TEXT NOT NULL DEFAULT '',
            project_path TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_updated_at TEXT NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            last_message_preview TEXT NOT NULL DEFAULT '',
            full_content_synced INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'idle'
        )
        """
    )
    legacy_conn.execute(
        "INSERT INTO sessions (id, tool, title, created_at, last_updated_at, message_count, full_content_synced) "
        "VALUES ('claude-code:legacy', 'claude-code', 'Old', 't', 't', 7, 1)"
    )
    # Real ground truth: 7 messages are actually stored for this fully-synced session.
    legacy_conn.execute(
        """
        CREATE TABLE messages (
            session_id TEXT NOT NULL REFERENCES sessions(id),
            idx INTEGER NOT NULL,
            role TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            content TEXT NOT NULL,
            PRIMARY KEY (session_id, idx)
        )
        """
    )
    for i in range(7):
        legacy_conn.execute(
            "INSERT INTO messages (session_id, idx, role, timestamp, content) VALUES (?, ?, 'user', 't', ?)",
            ("claude-code:legacy", i, str(i)),
        )
    legacy_conn.commit()
    legacy_conn.close()

    migrated_conn = db.get_connection(db_path)
    db.init_db(migrated_conn)  # must not fail on a table that already exists without the new column

    columns = {row["name"] for row in migrated_conn.execute("PRAGMA table_info(sessions)").fetchall()}
    assert "loaded_message_count" in columns

    session = db.get_session(migrated_conn, "claude-code:legacy")
    # Backfilled from the actual count of stored messages (ground truth), which
    # happens to match message_count in this normal fully-synced case.
    assert session["loaded_message_count"] == 7
    migrated_conn.close()


def test_migration_backfills_from_actual_messages_not_from_message_count_or_full_content_synced(tmp_path):
    # Reproduces the real production bug: upsert_session resets full_content_synced
    # to 0 whenever a session gets new activity, but leaves previously-stored
    # messages rows in place. A pre-existing row can therefore have
    # full_content_synced = 0 (and a stale/unrelated message_count) while the
    # messages table genuinely already holds a full history for it. The old
    # migration backfilled loaded_message_count from message_count gated on
    # full_content_synced = 1, which gave such rows loaded_message_count = 0 and
    # made the app believe nothing was stored — the next "Mehr laden" click would
    # then have replace_messages delete all those real, irreplaceable messages.
    db_path = str(tmp_path / "legacy2.db")
    legacy_conn = db.get_connection(db_path)
    legacy_conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            tool TEXT NOT NULL,
            entrypoint TEXT NOT NULL DEFAULT '',
            project_path TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_updated_at TEXT NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            last_message_preview TEXT NOT NULL DEFAULT '',
            full_content_synced INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'idle'
        )
        """
    )
    # full_content_synced = 0 (reset by new activity) and a stale message_count,
    # but the messages table genuinely still holds 42 real stored messages.
    legacy_conn.execute(
        "INSERT INTO sessions (id, tool, title, created_at, last_updated_at, message_count, full_content_synced) "
        "VALUES ('claude-code:legacy2', 'claude-code', 'Old', 't', 't', 3, 0)"
    )
    legacy_conn.execute(
        """
        CREATE TABLE messages (
            session_id TEXT NOT NULL REFERENCES sessions(id),
            idx INTEGER NOT NULL,
            role TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            content TEXT NOT NULL,
            PRIMARY KEY (session_id, idx)
        )
        """
    )
    for i in range(42):
        legacy_conn.execute(
            "INSERT INTO messages (session_id, idx, role, timestamp, content) VALUES (?, ?, 'user', 't', ?)",
            ("claude-code:legacy2", i, str(i)),
        )
    legacy_conn.commit()
    legacy_conn.close()

    migrated_conn = db.get_connection(db_path)
    db.init_db(migrated_conn)

    session = db.get_session(migrated_conn, "claude-code:legacy2")
    # Must reflect what's actually stored (42) — NOT message_count (3) and NOT 0
    # (the column default the old logic would have left it at).
    assert session["loaded_message_count"] == 42
    migrated_conn.close()


def test_partially_loaded_messages_are_searchable_immediately(conn):
    db.upsert_session(conn, _sample_session() | {"message_count": 5})
    db.replace_messages(
        conn,
        "claude-code:abc",
        [{"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": "zebra-unique-word"}],
    )
    session = db.get_session(conn, "claude-code:abc")
    assert session["full_content_synced"] == 0  # only a partial load, not the full 5 messages

    assert [s["id"] for s in db.get_sessions(conn, q="zebra-unique-word")] == ["claude-code:abc"]


def test_replace_messages_with_is_complete_marks_fully_synced_even_if_fewer_than_message_count(conn):
    # Reproduces the real bug: message_count (8) counts tool-only turns that
    # have no text, so only 5 real messages will ever exist. Before this fix,
    # full_content_synced could never become 1 for such a session.
    db.upsert_session(conn, _sample_session() | {"message_count": 8})
    messages = [
        {"idx": i, "role": "user", "timestamp": f"2026-08-01T10:0{i}:00Z", "content": str(i)}
        for i in range(5)
    ]
    db.replace_messages(conn, "claude-code:abc", messages, is_complete=True)
    session = db.get_session(conn, "claude-code:abc")
    assert session["loaded_message_count"] == 5
    assert session["full_content_synced"] == 1  # trusted the agent's ground truth, not the count comparison


def test_replace_messages_with_explicit_is_complete_false_does_not_trigger_count_fallback(conn):
    # Reproduces the real bug: message_count can be 0 or understated (confirmed on real
    # Cursor data), so the old code's fallback comparison (loaded >= message_count) could
    # wrongly mark a session fully synced even when the agent explicitly said more exists.
    db.upsert_session(conn, _sample_session() | {"message_count": 0})
    messages = [
        {"idx": i, "role": "user", "timestamp": f"2026-08-01T10:0{i}:00Z", "content": str(i)}
        for i in range(10)
    ]
    db.replace_messages(conn, "claude-code:abc", messages, is_complete=False)
    session = db.get_session(conn, "claude-code:abc")
    assert session["loaded_message_count"] == 10
    assert session["full_content_synced"] == 0  # must NOT be marked complete — the agent said more exists


def test_apply_recent_messages_populates_when_nothing_loaded(conn):
    db.upsert_session(conn, _sample_session())
    db.apply_recent_messages(
        conn,
        "claude-code:abc",
        [
            {"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00Z", "content": "hi"},
            {"idx": 1, "role": "assistant", "timestamp": "2026-08-01T10:01:00Z", "content": "hello"},
        ],
    )
    session = db.get_session(conn, "claude-code:abc")
    assert session["loaded_message_count"] == 2
    assert db.get_messages(conn, "claude-code:abc")[0]["content"] == "hi"


def test_apply_recent_messages_does_not_shrink_already_loaded_history(conn):
    db.upsert_session(conn, _sample_session() | {"message_count": 200})
    full_history = [
        {"idx": i, "role": "user", "timestamp": f"2026-08-01T10:{i:02d}:00Z", "content": str(i)}
        for i in range(200)
    ]
    db.replace_messages(conn, "claude-code:abc", full_history, is_complete=True)

    db.apply_recent_messages(
        conn,
        "claude-code:abc",
        [{"idx": 199, "role": "user", "timestamp": "2026-08-01T13:19:00Z", "content": "199"}],
    )
    session = db.get_session(conn, "claude-code:abc")
    assert session["loaded_message_count"] == 200  # untouched — 1 < 200, guard skips


def test_apply_recent_messages_refreshes_same_length_newer_tail(conn):
    db.upsert_session(conn, _sample_session() | {"message_count": 40})
    first_batch = [
        {"idx": i, "role": "user", "timestamp": f"2026-08-01T10:{i:02d}:00Z", "content": f"old{i}"}
        for i in range(30, 40)
    ]
    db.apply_recent_messages(conn, "claude-code:abc", first_batch)
    assert db.get_session(conn, "claude-code:abc")["loaded_message_count"] == 10
    assert [m["content"] for m in db.get_messages(conn, "claude-code:abc")] == [f"old{i}" for i in range(30, 40)]

    db.upsert_session(conn, _sample_session() | {"message_count": 60, "last_updated_at": "2026-08-01T11:00:00Z"})
    second_batch = [
        {"idx": i, "role": "user", "timestamp": f"2026-08-01T11:{i-50:02d}:00Z", "content": f"new{i}"}
        for i in range(50, 60)
    ]
    db.apply_recent_messages(conn, "claude-code:abc", second_batch)
    session = db.get_session(conn, "claude-code:abc")
    assert session["loaded_message_count"] == 10
    assert [m["content"] for m in db.get_messages(conn, "claude-code:abc")] == [f"new{i}" for i in range(50, 60)]


def test_apply_recent_messages_is_noop_when_empty(conn):
    db.upsert_session(conn, _sample_session())
    db.apply_recent_messages(conn, "claude-code:abc", [])
    session = db.get_session(conn, "claude-code:abc")
    assert session["loaded_message_count"] == 0


def test_remote_commands_paused_starts_false_then_can_be_set(conn):
    assert db.get_remote_commands_paused(conn) is False
    db.set_remote_commands_paused(conn, True)
    assert db.get_remote_commands_paused(conn) is True
    db.set_remote_commands_paused(conn, False)
    assert db.get_remote_commands_paused(conn) is False


def test_claim_pending_jobs_withholds_command_jobs_while_paused(conn):
    db.upsert_session(conn, _sample_session())
    fetch_job = db.create_job(conn, "fetch_full", "claude-code:abc")
    resume_job = db.create_job(conn, "resume_message", "claude-code:abc", payload='{"prompt": "hi"}')
    new_session_job = db.create_job(conn, "new_session", "/allowed/path", payload='{"prompt": "hi", "tool": "claude-code"}')

    db.set_remote_commands_paused(conn, True)
    claimed = db.claim_pending_jobs(conn)
    assert {j["id"] for j in claimed} == {fetch_job}

    db.set_remote_commands_paused(conn, False)
    claimed_after_unpause = db.claim_pending_jobs(conn)
    assert {j["id"] for j in claimed_after_unpause} == {resume_job, new_session_job}


def test_fail_stale_jobs_uses_shorter_timeout_for_fetch_full(conn):
    db.upsert_session(conn, _sample_session())
    old_fetch_job = db.create_job(conn, "fetch_full", "claude-code:abc")
    old_resume_job = db.create_job(conn, "resume_message", "claude-code:abc", payload='{"prompt": "hi"}')
    db.claim_pending_jobs(conn)  # both -> running

    # Backdate created_at past fetch_full's 300s cutoff but well within resume_message's 1800s one.
    backdated = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    conn.execute("UPDATE jobs SET created_at = ? WHERE id IN (?, ?)", (backdated, old_fetch_job, old_resume_job))
    conn.commit()

    db.fail_stale_jobs(conn)

    assert db.get_job(conn, old_fetch_job)["status"] == "failed"
    assert db.get_job(conn, old_resume_job)["status"] == "running"


def test_get_all_jobs_returns_newest_first(conn):
    db.upsert_session(conn, _sample_session())
    first = db.create_job(conn, "fetch_full", "claude-code:abc")
    second = db.create_job(conn, "resume_message", "claude-code:abc", payload='{"prompt": "hi"}')

    jobs = db.get_all_jobs(conn)
    assert [j["id"] for j in jobs] == [second, first]


def test_current_poll_interval_seconds_defaults_when_never_bumped(conn):
    assert db.current_poll_interval_seconds(conn, 60, 10) == 60


def test_current_poll_interval_seconds_returns_active_within_window(conn):
    db.bump_active_interval(conn, 5)
    assert db.current_poll_interval_seconds(conn, 60, 10) == 10


def test_current_poll_interval_seconds_reverts_after_window_expires(conn):
    conn.execute(
        "UPDATE agent_status SET active_until = ? WHERE id = 1",
        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
    )
    conn.commit()
    assert db.current_poll_interval_seconds(conn, 60, 10) == 60


def test_active_window_open_handles_malformed_timestamp_defensively(conn):
    conn.execute("UPDATE agent_status SET active_until = ? WHERE id = 1", ("not-a-real-timestamp",))
    conn.commit()
    assert db.active_window_open(conn) is False
    assert db.current_poll_interval_seconds(conn, 60, 10) == 60


def test_poll_mode_reflects_window_not_value_equality(conn):
    # Even if the two configured intervals happen to be equal, the mode must be
    # derived from active_until, not from comparing the returned int.
    assert db.current_poll_interval_seconds(conn, 60, 60) == 60
    assert db.active_window_open(conn) is False
    db.bump_active_interval(conn, 5)
    assert db.current_poll_interval_seconds(conn, 60, 60) == 60
    assert db.active_window_open(conn) is True


def test_migration_adds_active_until_to_pre_existing_db(tmp_path):
    db_path = str(tmp_path / "legacy3.db")
    legacy_conn = db.get_connection(db_path)
    legacy_conn.execute(
        "CREATE TABLE agent_status (id INTEGER PRIMARY KEY CHECK (id = 1), last_contact_at TEXT)"
    )
    legacy_conn.execute("INSERT INTO agent_status (id, last_contact_at) VALUES (1, NULL)")
    legacy_conn.commit()
    legacy_conn.close()

    migrated_conn = db.get_connection(db_path)
    db.init_db(migrated_conn)  # must not fail on a table that already exists without the new column

    columns = {row["name"] for row in migrated_conn.execute("PRAGMA table_info(agent_status)").fetchall()}
    assert "active_until" in columns
    migrated_conn.close()


def test_connection_can_be_used_from_another_thread(tmp_path):
    # FastAPI may run a request's dependency and route on different threadpool threads.
    import threading

    conn = db.get_connection(str(tmp_path / "t.db"))
    db.init_db(conn)
    errors = []

    def use():
        try:
            conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        except Exception as exc:  # pragma: no cover - the failure being guarded against
            errors.append(exc)

    worker = threading.Thread(target=use)
    worker.start()
    worker.join()
    conn.close()
    assert errors == []
