import json
import os
import shutil
from pathlib import Path

import pytest

from agent import claude_code_source

FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


@pytest.fixture
def projects_dir(tmp_path):
    project_dir = tmp_path / "-Users-jan-source-demo"
    project_dir.mkdir()
    shutil.copy(FIXTURE, project_dir / "test-session-1.jsonl")
    subagents_dir = project_dir / "subagents"
    subagents_dir.mkdir()
    shutil.copy(FIXTURE, subagents_dir / "agent-should-be-ignored.jsonl")
    return tmp_path


def test_list_claude_code_sessions_extracts_title_and_preview(projects_dir):
    sessions = claude_code_source.list_claude_code_sessions(projects_dir)
    assert len(sessions) == 1  # subagent file excluded

    session = sessions[0]
    assert session["id"] == "claude-code:test-session-1"
    assert session["tool"] == "claude-code"
    assert session["entrypoint"] == "claude-vscode"
    assert session["project_path"] == "/Users/jan/source/demo"
    assert session["title"] == "Recent project changes"
    assert session["last_message_preview"] == "Three commits landed yesterday."
    assert session["message_count"] == 4  # 1 user + 3 assistant events
    assert session["created_at"] == "2026-08-01T10:00:00.100Z"
    assert session["last_updated_at"] == "2026-08-01T10:00:03.000Z"


def test_get_full_messages_skips_events_without_text(projects_dir):
    messages = claude_code_source.get_full_messages("test-session-1", projects_dir)
    assert [m["content"] for m in messages] == [
        "What changed recently?",
        "Three commits landed yesterday.",
    ]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["idx"] == 0
    assert messages[1]["idx"] == 1


def test_extract_text_handles_string_content():
    # Test that string-typed content is handled directly
    message = {"content": "plain string text"}
    assert claude_code_source._extract_text(message) == "plain string text"


def test_extract_text_handles_string_content_with_whitespace():
    # Test that string-typed content is stripped
    message = {"content": "  plain string with spaces  "}
    assert claude_code_source._extract_text(message) == "plain string with spaces"


def test_list_sessions_skips_malformed_json_lines(tmp_path):
    # Create a project with a JSONL file containing malformed lines
    project_dir = tmp_path / "-Users-test-project"
    project_dir.mkdir()

    jsonl_file = project_dir / "malformed-session.jsonl"
    jsonl_file.write_text(
        '{"type": "queue-operation", "timestamp": "2026-08-01T10:00:00.000Z"}\n'
        'not valid json {{{\n'  # malformed line
        '{"type": "user", "message": {"content": [{"type": "text", "text": "hello"}]}, "timestamp": "2026-08-01T10:00:01.000Z"}\n'
    )

    # Should not crash; should skip the malformed line and process the valid lines
    sessions = claude_code_source.list_claude_code_sessions(tmp_path)
    assert len(sessions) == 1


def test_list_sessions_skips_non_dict_json_lines(tmp_path):
    # Create a project with a JSONL file containing valid JSON but non-dict lines
    project_dir = tmp_path / "-Users-test-project"
    project_dir.mkdir()

    jsonl_file = project_dir / "non-dict-session.jsonl"
    jsonl_file.write_text(
        '{"type": "queue-operation", "timestamp": "2026-08-01T10:00:00.000Z"}\n'
        '42\n'  # valid JSON but not a dict
        '"string"\n'  # valid JSON but not a dict
        '{"type": "user", "message": {"content": [{"type": "text", "text": "hello"}]}, "timestamp": "2026-08-01T10:00:01.000Z"}\n'
    )

    # Should not crash; should skip the non-dict lines and process the valid dict lines
    sessions = claude_code_source.list_claude_code_sessions(tmp_path)
    assert len(sessions) == 1


def test_get_full_messages_skips_malformed_and_non_dict_lines(tmp_path):
    # Create a project with a JSONL file containing various malformed lines
    project_dir = tmp_path / "-Users-test-project"
    project_dir.mkdir()

    session_id = "test-mixed-lines"
    jsonl_file = project_dir / f"{session_id}.jsonl"
    jsonl_file.write_text(
        '{"type": "user", "message": {"content": [{"type": "text", "text": "first"}]}, "timestamp": "2026-08-01T10:00:00.000Z"}\n'
        'malformed }\n'  # malformed line
        '42\n'  # valid JSON but not a dict
        '{"type": "assistant", "message": {"content": [{"type": "text", "text": "second"}]}, "timestamp": "2026-08-01T10:00:01.000Z"}\n'
    )

    # Should not crash; should skip bad lines and process valid ones
    messages = claude_code_source.get_full_messages(session_id, tmp_path)
    assert len(messages) == 2
    assert [m["content"] for m in messages] == ["first", "second"]


def test_unchanged_file_is_served_from_cache_changed_file_is_reparsed(projects_dir):
    # First call populates the module-level mtime cache.
    sessions = claude_code_source.list_claude_code_sessions(projects_dir)
    assert sessions[0]["title"] == "Recent project changes"

    file_path = projects_dir / "-Users-jan-source-demo" / "test-session-1.jsonl"
    assert file_path in claude_code_source._file_cache
    cached_mtime, cached_session = claude_code_source._file_cache[file_path]

    # Tamper with the cached value directly, WITHOUT touching the file on disk.
    tampered = dict(cached_session)
    tampered["title"] = "SENTINEL-FROM-CACHE"
    claude_code_source._file_cache[file_path] = (cached_mtime, tampered)

    # Same mtime -> the cache must be used, proving no re-parse happened.
    sessions_again = claude_code_source.list_claude_code_sessions(projects_dir)
    assert sessions_again[0]["title"] == "SENTINEL-FROM-CACHE"

    # Bump the mtime -> a real re-parse must happen, sentinel must disappear.
    bumped = cached_mtime + 5
    os.utime(file_path, (bumped, bumped))
    sessions_after_change = claude_code_source.list_claude_code_sessions(projects_dir)
    assert sessions_after_change[0]["title"] == "Recent project changes"


def test_one_bad_file_does_not_prevent_other_sessions_from_being_returned(tmp_path, monkeypatch, capsys):
    project_dir = tmp_path / "-Users-jan-source-demo"
    project_dir.mkdir()
    shutil.copy(FIXTURE, project_dir / "good-session.jsonl")
    (project_dir / "bad-session.jsonl").write_text("irrelevant content")

    original_parse = claude_code_source._parse_session_file

    def flaky_parse(path):
        if path.name == "bad-session.jsonl":
            raise OSError("simulated read failure")
        return original_parse(path)

    monkeypatch.setattr(claude_code_source, "_parse_session_file", flaky_parse)

    sessions = claude_code_source.list_claude_code_sessions(tmp_path)

    assert len(sessions) == 1
    assert sessions[0]["id"] == "claude-code:good-session"

    stderr = capsys.readouterr().err
    assert "bad-session.jsonl" in stderr


def test_get_project_path_returns_cwd_from_session_file(tmp_path):
    from agent.claude_code_source import get_project_path

    project_dir = tmp_path / "-Users-jan-source-demo"
    project_dir.mkdir()
    session_file = project_dir / "abc123.jsonl"
    session_file.write_text(
        '{"type": "user", "timestamp": "2026-08-01T10:00:00Z", "cwd": "/Users/jan/source/demo", '
        '"message": {"content": "hi"}}\n'
    )

    assert get_project_path("abc123", projects_dir=tmp_path) == "/Users/jan/source/demo"


def test_get_project_path_returns_none_for_unknown_session(tmp_path):
    from agent.claude_code_source import get_project_path

    assert get_project_path("does-not-exist", projects_dir=tmp_path) is None


def test_get_project_path_returns_none_when_projects_dir_missing(tmp_path):
    from agent.claude_code_source import get_project_path

    assert get_project_path("abc123", projects_dir=tmp_path / "does-not-exist") is None


def test_list_claude_code_sessions_includes_recent_messages(projects_dir):
    sessions = claude_code_source.list_claude_code_sessions(projects_dir)
    session = sessions[0]
    assert session["recent_messages"] == [
        {"idx": 0, "role": "user", "timestamp": "2026-08-01T10:00:00.100Z", "content": "What changed recently?"},
        {"idx": 1, "role": "assistant", "timestamp": "2026-08-01T10:00:03.000Z", "content": "Three commits landed yesterday."},
    ]


def test_recent_messages_capped_at_limit_with_correct_original_idx(tmp_path):
    project_dir = tmp_path / "-Users-test-project"
    project_dir.mkdir()
    lines = []
    for i in range(15):
        lines.append(
            json.dumps(
                {
                    "type": "user" if i % 2 == 0 else "assistant",
                    "message": {"content": [{"type": "text", "text": f"msg{i}"}]},
                    "timestamp": f"2026-08-01T10:00:{i:02d}.000Z",
                }
            )
        )
    (project_dir / "many-messages.jsonl").write_text("\n".join(lines) + "\n")

    sessions = claude_code_source.list_claude_code_sessions(tmp_path)
    recent = sessions[0]["recent_messages"]
    assert len(recent) == 10
    assert [m["idx"] for m in recent] == list(range(5, 15))
    assert [m["content"] for m in recent] == [f"msg{i}" for i in range(5, 15)]


# SEC-005 regression guards. `raw_session_id` was concatenated straight into a path, so
# an absolute value replaced the projects directory entirely (pathlib drops the left
# operand) and `../` escaped it. Not reachable over HTTP today, but any future caller
# that supplies the id from a different source would turn it into an authenticated
# arbitrary-.jsonl read on the owner's Mac.
def test_find_session_file_rejects_absolute_session_id(tmp_path):
    project_dir = tmp_path / "-Users-jan-source-demo"
    project_dir.mkdir()
    outside = tmp_path.parent / "outside-secret.jsonl"
    outside.write_text('{"type": "user", "message": {"content": "secret"}}\n')

    assert claude_code_source._find_session_file(str(outside)[: -len(".jsonl")], tmp_path) is None
    assert claude_code_source.get_full_messages(str(outside)[: -len(".jsonl")], tmp_path) == []


def test_find_session_file_rejects_relative_traversal(tmp_path):
    projects_dir = tmp_path / "projects"
    (projects_dir / "-Users-jan-source-demo").mkdir(parents=True)
    outside = tmp_path / "outside-secret.jsonl"
    outside.write_text('{"type": "user", "message": {"content": "secret"}}\n')

    assert claude_code_source._find_session_file("../../outside-secret", projects_dir) is None


def test_find_session_file_rejects_ids_with_path_separators_or_dots(tmp_path):
    project_dir = tmp_path / "-Users-jan-source-demo"
    project_dir.mkdir()
    shutil.copy(FIXTURE, project_dir / "test-session-1.jsonl")

    for bad_id in ["", "a/b", "a\\b", "..", "sub/test-session-1", "test session"]:
        assert claude_code_source._find_session_file(bad_id, tmp_path) is None

    # …while an ordinary session id still resolves.
    assert claude_code_source._find_session_file("test-session-1", tmp_path) is not None
