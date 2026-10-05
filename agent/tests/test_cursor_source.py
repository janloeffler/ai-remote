import json
import sqlite3
from pathlib import Path

import pytest

from agent import cursor_source


@pytest.fixture
def state_db(tmp_path):
    path = tmp_path / "state.vscdb"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE composerHeaders (
            composerId TEXT PRIMARY KEY,
            workspaceId TEXT,
            createdAt INTEGER,
            lastUpdatedAt INTEGER,
            isArchived INTEGER,
            isSubagent INTEGER,
            value TEXT
        )
        """
    )
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO composerHeaders VALUES ('composer-1', 'workspace-1', 1754039000000, 1754040000000, 0, 0, ?)",
        (json.dumps({"name": "Demo Cursor chat"}),),
    )
    conn.execute(
        "INSERT INTO composerHeaders VALUES ('composer-archived', 'workspace-1', 1700000000000, 1700000000000, 1, 0, ?)",
        (json.dumps({"name": "Old"}),),
    )
    conn.execute(
        "INSERT INTO composerHeaders VALUES ('composer-subagent', 'workspace-1', 1754040000000, 1754040000000, 0, 1, ?)",
        (json.dumps({"name": "Background sub-composer"}),),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:composer-1:b1", json.dumps({"type": 1, "createdAt": "2026-08-01T10:00:00.000Z", "text": "Hi there"})),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:composer-1:b2", json.dumps({"type": 2, "createdAt": "2026-08-01T10:00:05.000Z", "text": ""})),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:composer-1:b3", json.dumps({"type": 2, "createdAt": "2026-08-01T10:00:10.000Z", "text": "Sure, here you go."})),
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def workspace_storage(tmp_path):
    workspace_dir = tmp_path / "workspaceStorage" / "workspace-1"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "workspace.json").write_text(json.dumps({"folder": "file:///Users/jan/source/demo"}))
    return tmp_path / "workspaceStorage"


def test_list_cursor_sessions_excludes_archived(state_db):
    sessions = cursor_source.list_cursor_sessions(state_db)
    assert len(sessions) == 1
    assert sessions[0]["id"] == "cursor:composer-1"
    assert sessions[0]["title"] == "Demo Cursor chat"
    assert sessions[0]["tool"] == "cursor"


def test_list_cursor_sessions_excludes_subagents(state_db):
    """isSubagent rows are background sub-composers, not real user-facing chats."""
    sessions = cursor_source.list_cursor_sessions(state_db)
    assert "cursor:composer-subagent" not in [s["id"] for s in sessions]


def test_enrich_with_messages_fills_preview_and_project_path(state_db, workspace_storage, monkeypatch):
    sessions = cursor_source.list_cursor_sessions(state_db)
    monkeypatch.setattr(cursor_source, "CURSOR_WORKSPACE_STORAGE", workspace_storage)
    cursor_source.enrich_with_messages(sessions, state_db)

    assert sessions[0]["message_count"] == 2  # the empty-text bubble is skipped
    assert sessions[0]["last_message_preview"] == "Sure, here you go."
    assert sessions[0]["project_path"] == "/Users/jan/source/demo"


def test_get_full_messages_orders_by_timestamp(state_db):
    messages = cursor_source.get_full_messages("composer-1", state_db)
    assert [m["content"] for m in messages] == ["Hi there", "Sure, here you go."]
    assert [m["role"] for m in messages] == ["user", "assistant"]


def test_get_full_messages_range_scan_returns_correct_bubbles(state_db):
    """The LIKE-to-range-scan rewrite must still return exactly the bubbles for the given composer_id."""
    messages = cursor_source.get_full_messages("composer-1", state_db)
    assert [m["content"] for m in messages] == ["Hi there", "Sure, here you go."]
    assert [m["role"] for m in messages] == ["user", "assistant"]


def test_get_full_messages_range_scan_excludes_composer_id_with_shared_prefix(tmp_path):
    """A composer_id that is a strict prefix of another composer_id (e.g. 'abc' vs 'abc-extra')
    must not have the other's bubbles leak in. This pins down that the range bounds
    (key >= 'bubbleId:abc:' AND key < 'bubbleId:abc;') are precise, not off-by-one:
    'bubbleId:abc-extra:x' sorts before 'bubbleId:abc;' (since '-' < ';') so a sloppy
    upper bound could accidentally include it."""
    path = tmp_path / "state.vscdb"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE composerHeaders (composerId TEXT PRIMARY KEY, workspaceId TEXT)")
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO composerHeaders VALUES ('abc', 'workspace-1')")
    conn.execute("INSERT INTO composerHeaders VALUES ('abc-extra', 'workspace-1')")
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:abc:x", json.dumps({"type": 1, "createdAt": "2026-08-01T10:00:00.000Z", "text": "For abc"})),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "bubbleId:abc-extra:x",
            json.dumps({"type": 1, "createdAt": "2026-08-01T10:00:01.000Z", "text": "For abc-extra"}),
        ),
    )
    conn.commit()
    conn.close()

    messages = cursor_source.get_full_messages("abc", path)
    assert [m["content"] for m in messages] == ["For abc"]

    other_messages = cursor_source.get_full_messages("abc-extra", path)
    assert [m["content"] for m in other_messages] == ["For abc-extra"]


def test_get_full_messages_handles_null_createdAt(tmp_path):
    """Verify that bubbles with createdAt: null (JSON null -> Python None) produce timestamp: '' not None."""
    path = tmp_path / "state.vscdb"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE composerHeaders (composerId TEXT PRIMARY KEY, workspaceId TEXT)")
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO composerHeaders VALUES ('composer-null', 'workspace-1')")
    # Insert a bubble with createdAt: null (literal JSON null) and non-empty text
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:composer-null:b1", json.dumps({"type": 1, "createdAt": None, "text": "Message with null timestamp"})),
    )
    conn.commit()
    conn.close()

    messages = cursor_source.get_full_messages("composer-null", path)
    assert len(messages) == 1
    assert messages[0]["timestamp"] == ""  # Should be empty string, not None
    assert messages[0]["content"] == "Message with null timestamp"


def test_get_project_path_resolves_via_workspace_json(tmp_path):
    import json
    import sqlite3

    from agent.cursor_source import get_project_path

    state_db = tmp_path / "state.vscdb"
    conn = sqlite3.connect(state_db)
    conn.execute("CREATE TABLE composerHeaders (composerId TEXT, workspaceId TEXT)")
    conn.execute("INSERT INTO composerHeaders VALUES ('composer-1', 'workspace-1')")
    conn.commit()
    conn.close()

    workspace_storage = tmp_path / "workspaceStorage"
    workspace_dir = workspace_storage / "workspace-1"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "workspace.json").write_text(
        json.dumps({"folder": "file:///Users/jan/source/demo"})
    )

    assert get_project_path("composer-1", state_db_path=state_db, workspace_storage=workspace_storage) == "/Users/jan/source/demo"


def test_get_project_path_returns_none_when_state_db_missing(tmp_path):
    from agent.cursor_source import get_project_path

    assert get_project_path("composer-1", state_db_path=tmp_path / "missing.vscdb") is None


def test_enrich_with_messages_fills_recent_messages(state_db, workspace_storage, monkeypatch):
    sessions = cursor_source.list_cursor_sessions(state_db)
    monkeypatch.setattr(cursor_source, "CURSOR_WORKSPACE_STORAGE", workspace_storage)
    cursor_source.enrich_with_messages(sessions, state_db)

    assert sessions[0]["recent_messages"] == [
        {"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00.000Z", "content": "Hi there"},
        {"idx": 1, "role": "assistant", "timestamp": "2026-08-01T10:00:10.000Z", "content": "Sure, here you go."},
    ]


def test_enrich_with_messages_caps_recent_messages_at_limit(tmp_path):
    state_path = tmp_path / "state.vscdb"
    state_conn = sqlite3.connect(state_path)
    state_conn.execute(
        """
        CREATE TABLE composerHeaders (
            composerId TEXT PRIMARY KEY,
            workspaceId TEXT,
            createdAt INTEGER,
            lastUpdatedAt INTEGER,
            isArchived INTEGER,
            isSubagent INTEGER,
            value TEXT
        )
        """
    )
    state_conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    state_conn.execute(
        "INSERT INTO composerHeaders VALUES ('composer-many', 'workspace-1', 1754040000000, 1754040000000, 0, 0, ?)",
        (json.dumps({"name": "Many"}),),
    )
    for i in range(15):
        state_conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (
                f"bubbleId:composer-many:b{i:02d}",
                json.dumps(
                    {
                        "type": 1 if i % 2 == 0 else 2,
                        "createdAt": f"2026-08-01T10:00:{i:02d}.000Z",
                        "text": f"msg{i}",
                    }
                ),
            ),
        )
    state_conn.commit()
    state_conn.close()

    sessions = cursor_source.list_cursor_sessions(state_path)
    cursor_source.enrich_with_messages(sessions, state_path)

    recent = sessions[0]["recent_messages"]
    assert len(recent) == 10
    assert [m["idx"] for m in recent] == list(range(5, 15))
    assert [m["content"] for m in recent] == [f"msg{i}" for i in range(5, 15)]
