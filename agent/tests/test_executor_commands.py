import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent import executor, permission_profile

FAKE_CLI = [sys.executable, str(Path(__file__).parent / "fixtures" / "fake_cli.py")]

# Each tool's own rule syntax — Claude Code's Bash(...)/Edit(...) and Cursor's
# Shell(...) are not interchangeable, and a profile written in the other tool's dialect
# silently matches nothing.
VALID_PROFILES: dict[str, dict] = {
    "claude-code": {"permissions": {"allow": ["Read(**)", "Edit(**)"], "deny": ["Bash(sudo:*)"]}},
    "cursor": {"permissions": {"allow": ["Read(**)", "Write(**)"], "deny": ["Shell(sudo)"]}},
}


def _write_profile(project: Path, tool: str, content: dict | str | None = None) -> Path:
    if content is None:
        content = VALID_PROFILES[tool]
    profile = project.joinpath(*permission_profile.PROFILE_PATHS[tool])
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(content if isinstance(content, str) else json.dumps(content))
    return profile


@pytest.fixture(autouse=True)
def _permission_profiles_for_tmp_project(tmp_path):
    """SEC-003: the executor refuses to run in a project with no permission profile.
    Every test here uses tmp_path as the allow-listed project, so give it a valid
    profile for both tools; the refusal tests point at a fresh subdirectory instead."""
    _write_profile(tmp_path, "claude-code")
    _write_profile(tmp_path, "cursor")


@pytest.fixture(autouse=True)
def _patch_subprocess_timeout(monkeypatch):
    # Tests must not wait 30 real minutes for the 'hang' case — shrink the
    # timeout the executor enforces, without touching its production default.
    monkeypatch.setattr(executor, "COMMAND_TIMEOUT_SECONDS", 1)


@pytest.fixture(autouse=True)
def _patch_cli_binaries(monkeypatch):
    monkeypatch.setattr(executor, "CLAUDE_CLI", FAKE_CLI)
    monkeypatch.setattr(executor, "CURSOR_AGENT_CLI", FAKE_CLI)


def _capture_cmd(monkeypatch) -> dict:
    """Intercepts the argv the executor would hand to the CLI, without running anything."""
    captured: dict = {}

    def fake_run_and_report(cmd, cwd):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return {"status": "done", "result_text": "", "messages": [], "is_complete": False}

    monkeypatch.setattr(executor, "_run_and_report", fake_run_and_report)
    return captured


# SEC-002 regression guards: the prompt is attacker-controlled text and must never be
# parsed as a CLI option. Without a `--` end-of-options separator a prompt like
# `--add-dir=/` becomes a flag and widens the agent past the project allow-list, which
# is this application's only containment boundary.
INJECTION_PROMPT = "--add-dir=/"


def test_resume_message_claude_passes_prompt_after_end_of_options_separator(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.claude_code_source.get_project_path", lambda raw_id: str(tmp_path))
    captured = _capture_cmd(monkeypatch)
    job = {
        "id": "j1",
        "type": "resume_message",
        "target": "claude-code:abc",
        "payload": '{"prompt": "%s"}' % INJECTION_PROMPT,
    }

    executor.execute_resume_message(job, allowed_projects=[str(tmp_path)])

    assert captured["cmd"][-2:] == ["--", INJECTION_PROMPT]


def test_resume_message_cursor_passes_prompt_after_end_of_options_separator(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.cursor_source.get_project_path", lambda raw_id: str(tmp_path))
    captured = _capture_cmd(monkeypatch)
    job = {
        "id": "j1",
        "type": "resume_message",
        "target": "cursor:abc",
        "payload": '{"prompt": "%s"}' % INJECTION_PROMPT,
    }

    executor.execute_resume_message(job, allowed_projects=[str(tmp_path)])

    assert captured["cmd"][-2:] == ["--", INJECTION_PROMPT]


def test_new_session_claude_passes_prompt_after_end_of_options_separator(tmp_path, monkeypatch):
    captured = _capture_cmd(monkeypatch)
    job = {
        "id": "j1",
        "type": "new_session",
        "target": str(tmp_path),
        "payload": '{"prompt": "%s", "tool": "claude-code"}' % INJECTION_PROMPT,
    }

    executor.execute_new_session(job, allowed_projects=[str(tmp_path)])

    assert captured["cmd"][-2:] == ["--", INJECTION_PROMPT]


def test_new_session_cursor_passes_prompt_after_end_of_options_separator(tmp_path, monkeypatch):
    captured = _capture_cmd(monkeypatch)
    job = {
        "id": "j1",
        "type": "new_session",
        "target": str(tmp_path),
        "payload": '{"prompt": "%s", "tool": "cursor"}' % INJECTION_PROMPT,
    }

    executor.execute_new_session(job, allowed_projects=[str(tmp_path)])

    assert captured["cmd"][-2:] == ["--", INJECTION_PROMPT]


# SEC-003 regression guards: containment must fail closed. `dontAsk` / `--force` mean
# the project's permission profile is the only thing scoping what a remote job may do,
# so a project without one must be refused rather than run under the global default.
def _bare_project(tmp_path: Path) -> Path:
    project = tmp_path / "bare"
    project.mkdir()
    return project


def test_resume_message_refuses_claude_project_without_permission_profile(tmp_path, monkeypatch):
    project = _bare_project(tmp_path)
    monkeypatch.setattr("agent.claude_code_source.get_project_path", lambda raw_id: str(project))
    captured = _capture_cmd(monkeypatch)
    job = {"id": "j1", "type": "resume_message", "target": "claude-code:abc", "payload": '{"prompt": "hi"}'}

    result = executor.execute_resume_message(job, allowed_projects=[str(project)])

    assert result["status"] == "failed"
    assert "missing permission profile" in result["result_text"]
    assert ".claude/settings.json" in result["result_text"]
    assert "cmd" not in captured  # the CLI was never invoked


def test_resume_message_refuses_cursor_project_without_permission_profile(tmp_path, monkeypatch):
    project = _bare_project(tmp_path)
    monkeypatch.setattr("agent.cursor_source.get_project_path", lambda raw_id: str(project))
    captured = _capture_cmd(monkeypatch)
    job = {"id": "j1", "type": "resume_message", "target": "cursor:abc", "payload": '{"prompt": "hi"}'}

    result = executor.execute_resume_message(job, allowed_projects=[str(project)])

    assert result["status"] == "failed"
    assert "missing permission profile" in result["result_text"]
    assert ".cursor/cli.json" in result["result_text"]
    assert "cmd" not in captured


def test_new_session_refuses_project_without_permission_profile(tmp_path, monkeypatch):
    project = _bare_project(tmp_path)
    captured = _capture_cmd(monkeypatch)
    job = {
        "id": "j1",
        "type": "new_session",
        "target": str(project),
        "payload": '{"prompt": "hi", "tool": "claude-code"}',
    }

    result = executor.execute_new_session(job, allowed_projects=[str(project)])

    assert result["status"] == "failed"
    assert "missing permission profile" in result["result_text"]
    assert "cmd" not in captured


def test_new_session_refuses_profile_with_empty_permissions(tmp_path, monkeypatch):
    project = _bare_project(tmp_path)
    _write_profile(project, "claude-code", {"permissions": {"allow": [], "deny": []}})
    job = {
        "id": "j1",
        "type": "new_session",
        "target": str(project),
        "payload": '{"prompt": "hi", "tool": "claude-code"}',
    }

    result = executor.execute_new_session(job, allowed_projects=[str(project)])

    assert result["status"] == "failed"
    assert "no permissions.allow / permissions.deny" in result["result_text"]


def test_new_session_refuses_unparseable_profile(tmp_path, monkeypatch):
    project = _bare_project(tmp_path)
    _write_profile(project, "claude-code", "{not json")
    job = {
        "id": "j1",
        "type": "new_session",
        "target": str(project),
        "payload": '{"prompt": "hi", "tool": "claude-code"}',
    }

    result = executor.execute_new_session(job, allowed_projects=[str(project)])

    assert result["status"] == "failed"
    assert "unreadable permission profile" in result["result_text"]


def test_new_session_runs_when_project_has_a_permission_profile(tmp_path, monkeypatch):
    project = _bare_project(tmp_path)
    _write_profile(project, "claude-code")
    job = {
        "id": "j1",
        "type": "new_session",
        "target": str(project),
        "payload": '{"prompt": "hi", "tool": "claude-code"}',
    }

    result = executor.execute_new_session(job, allowed_projects=[str(project)])

    assert result["status"] == "done"


def test_execute_resume_message_rejects_non_allowlisted_project(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent.claude_code_source.get_project_path", lambda raw_id: "/Users/jan/source/not-allowed"
    )
    job = {"id": "j1", "type": "resume_message", "target": "claude-code:abc", "payload": '{"prompt": "hi"}'}

    result = executor.execute_resume_message(job, allowed_projects=["/Users/jan/source/demo"])

    assert result["status"] == "failed"
    assert "not allow-listed" in result["result_text"]


def test_execute_resume_message_runs_claude_and_reports_success(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.claude_code_source.get_project_path", lambda raw_id: str(tmp_path))
    job = {"id": "j1", "type": "resume_message", "target": "claude-code:abc", "payload": '{"prompt": "keep going"}'}

    result = executor.execute_resume_message(job, allowed_projects=[str(tmp_path)])

    assert result["status"] == "done"
    assert "--resume" in result["result_text"]
    assert "abc" in result["result_text"]
    assert "keep going" in result["result_text"]


def test_execute_resume_message_runs_cursor_and_reports_success(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.cursor_source.get_project_path", lambda raw_id: str(tmp_path))
    job = {"id": "j1", "type": "resume_message", "target": "cursor:abc", "payload": '{"prompt": "keep going"}'}

    result = executor.execute_resume_message(job, allowed_projects=[str(tmp_path)])

    assert result["status"] == "done"
    assert "--resume" in result["result_text"]
    assert "abc" in result["result_text"]
    assert "--workspace" in result["result_text"]
    assert "keep going" in result["result_text"]


def test_execute_resume_message_reports_failure_on_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLI_BEHAVIOR", "fail")
    monkeypatch.setattr("agent.claude_code_source.get_project_path", lambda raw_id: str(tmp_path))
    job = {"id": "j1", "type": "resume_message", "target": "claude-code:abc", "payload": '{"prompt": "hi"}'}

    result = executor.execute_resume_message(job, allowed_projects=[str(tmp_path)])

    assert result["status"] == "failed"
    assert "simulated CLI failure" in result["result_text"]


def test_execute_resume_message_times_out_and_kills_process_group(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLI_BEHAVIOR", "hang")
    monkeypatch.setattr("agent.claude_code_source.get_project_path", lambda raw_id: str(tmp_path))
    job = {"id": "j1", "type": "resume_message", "target": "claude-code:abc", "payload": '{"prompt": "hi"}'}

    result = executor.execute_resume_message(job, allowed_projects=[str(tmp_path)])

    assert result["status"] == "failed"
    assert "timed out" in result["result_text"]
    assert "about to hang" in result["result_text"]


def test_execute_resume_message_reports_failure_when_cli_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "CLAUDE_CLI", ["/nonexistent/binary-xyz"])
    monkeypatch.setattr("agent.claude_code_source.get_project_path", lambda raw_id: str(tmp_path))
    job = {"id": "j1", "type": "resume_message", "target": "claude-code:abc", "payload": '{"prompt": "hi"}'}

    result = executor.execute_resume_message(job, allowed_projects=[str(tmp_path)])

    assert result["status"] == "failed"
    assert "failed to launch subprocess" in result["result_text"]


def test_execute_new_session_runs_cursor_agent(tmp_path, monkeypatch):
    job = {
        "id": "j1",
        "type": "new_session",
        "target": str(tmp_path),
        "payload": '{"prompt": "start fresh", "tool": "cursor"}',
    }

    result = executor.execute_new_session(job, allowed_projects=[str(tmp_path)])

    assert result["status"] == "done"
    assert "--workspace" in result["result_text"]
    assert "start fresh" in result["result_text"]


def test_execute_new_session_rejects_non_allowlisted_project(tmp_path):
    job = {
        "id": "j1",
        "type": "new_session",
        "target": "/Users/jan/source/not-allowed",
        "payload": '{"prompt": "hi", "tool": "claude-code"}',
    }

    result = executor.execute_new_session(job, allowed_projects=[str(tmp_path)])

    assert result["status"] == "failed"
    assert "not allow-listed" in result["result_text"]


# A profile whose entries use the *other* tool's permission types is worse than no
# profile: the presence check passes, so the operator believes the project is confined,
# while `cursor-agent --force` (allow unless explicitly denied) matches none of Claude
# Code's Bash(...)/Edit(...) rules and runs unconstrained.
CLAUDE_SHAPED_PROFILE = {
    "permissions": {
        "allow": ["Read(**)", "Edit(**)", "Bash(pytest:*)"],
        "deny": ["Bash(rm -rf:*)", "Bash(curl:*)"],
    }
}
CURSOR_SHAPED_PROFILE = {
    "permissions": {
        "allow": ["Read(**)", "Write(**)", "Shell(pytest:*)"],
        "deny": ["Shell(rm)", "Mcp(datadog:*)"],
    }
}


def test_cursor_job_refuses_a_claude_shaped_profile(tmp_path, monkeypatch):
    project = _bare_project(tmp_path)
    _write_profile(project, "cursor", CLAUDE_SHAPED_PROFILE)
    monkeypatch.setattr("agent.cursor_source.get_project_path", lambda raw_id: str(project))
    captured = _capture_cmd(monkeypatch)
    job = {"id": "j1", "type": "resume_message", "target": "cursor:abc", "payload": '{"prompt": "hi"}'}

    result = executor.execute_resume_message(job, allowed_projects=[str(project)])

    assert result["status"] == "failed"
    assert "Bash" in result["result_text"]
    assert "cursor" in result["result_text"].lower()
    assert "cmd" not in captured


def test_claude_job_refuses_a_cursor_shaped_profile(tmp_path, monkeypatch):
    project = _bare_project(tmp_path)
    _write_profile(project, "claude-code", CURSOR_SHAPED_PROFILE)
    job = {
        "id": "j1",
        "type": "new_session",
        "target": str(project),
        "payload": '{"prompt": "hi", "tool": "claude-code"}',
    }

    result = executor.execute_new_session(job, allowed_projects=[str(project)])

    assert result["status"] == "failed"
    assert "Shell" in result["result_text"]


def test_each_shipped_template_is_accepted_for_its_own_tool(tmp_path):
    repo_root = Path(__file__).resolve().parents[2]
    templates = {
        "claude-code": repo_root / "docs/superpowers/reference/remote-agent-permissions.claude-code.json",
        "cursor": repo_root / "docs/superpowers/reference/remote-agent-permissions.cursor-cli.json",
    }
    for tool, template in templates.items():
        project = tmp_path / f"proj-{tool}"
        project.mkdir()
        _write_profile(project, tool, template.read_text())
        assert permission_profile.missing_profile_reason(str(project), tool) is None, tool


def test_each_shipped_template_is_rejected_for_the_other_tool(tmp_path):
    repo_root = Path(__file__).resolve().parents[2]
    swapped = {
        "cursor": repo_root / "docs/superpowers/reference/remote-agent-permissions.claude-code.json",
        "claude-code": repo_root / "docs/superpowers/reference/remote-agent-permissions.cursor-cli.json",
    }
    for tool, wrong_template in swapped.items():
        project = tmp_path / f"swapped-{tool}"
        project.mkdir()
        _write_profile(project, tool, wrong_template.read_text())
        assert permission_profile.missing_profile_reason(str(project), tool) is not None, tool


def test_profile_with_no_recognizable_entries_is_refused(tmp_path):
    project = _bare_project(tmp_path)
    _write_profile(project, "claude-code", {"permissions": {"allow": ["nonsense"], "deny": []}})

    assert "recognizable" in permission_profile.missing_profile_reason(str(project), "claude-code")
