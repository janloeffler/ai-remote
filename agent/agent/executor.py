import json
import os
import signal
import subprocess
from collections.abc import Sequence

from . import ai_tools, allowlist, claude_code_source, cursor_source, permission_profile

COMMAND_TIMEOUT_SECONDS = 1800
RESULT_TEXT_TRUNCATE = 4000
CLAUDE_CLI = ["claude"]
CURSOR_AGENT_CLI = ["cursor-agent"]


def execute_fetch_full(job: dict, enabled_tools: Sequence[str] = ai_tools.ALL_TOOLS) -> dict:
    parts = job["target"].split(":", 1)
    if len(parts) != 2:
        return {
            "status": "failed",
            "result_text": f"malformed job target: {job['target']!r}",
            "messages": [],
            "is_complete": False,
        }
    tool, raw_id = parts
    if tool in ai_tools.ALL_TOOLS and tool not in enabled_tools:
        return {"status": "failed", "result_text": f"tool is disabled: {tool}", "messages": [], "is_complete": False}
    if tool == "claude-code":
        messages = claude_code_source.get_full_messages(raw_id)
    elif tool == "cursor":
        messages = cursor_source.get_full_messages(raw_id)
    else:
        return {"status": "failed", "result_text": f"unknown tool: {tool}", "messages": [], "is_complete": False}

    if not messages:
        return {"status": "failed", "result_text": "no messages found for session", "messages": [], "is_complete": False}

    total_available = len(messages)
    count = _extract_count(job.get("payload", ""))
    is_complete = count is None or count <= 0 or count >= total_available
    if count is not None and count > 0:
        messages = messages[-count:]

    return {"status": "done", "result_text": "", "messages": messages, "is_complete": is_complete}


def _extract_count(payload: str) -> int | None:
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    count = data.get("count")
    return count if isinstance(count, int) else None


def _parse_json_payload(payload: str) -> dict:
    if not payload:
        return {}
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _run_subprocess(cmd: list[str], cwd: str) -> tuple[int, str, bool]:
    """Runs cmd in its own process group (start_new_session=True) so that on timeout
    we can kill the whole group, not just the direct child — otherwise anything the
    CLI itself spawns would be left running as an orphan after we give up on it."""
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True
    )
    try:
        output, _ = proc.communicate(timeout=COMMAND_TIMEOUT_SECONDS)
        return proc.returncode, output, False
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        output, _ = proc.communicate()
        return proc.returncode, output, True


def _run_and_report(cmd: list[str], cwd: str) -> dict:
    try:
        returncode, output, timed_out = _run_subprocess(cmd, cwd)
    except OSError as exc:
        return {
            "status": "failed",
            "result_text": f"failed to launch subprocess: {exc}",
            "messages": [],
            "is_complete": False,
        }
    truncated = output[-RESULT_TEXT_TRUNCATE:]
    if timed_out:
        return {
            "status": "failed",
            "result_text": f"timed out after {COMMAND_TIMEOUT_SECONDS}s. Output before kill: {truncated}",
            "messages": [],
            "is_complete": False,
        }
    if returncode != 0:
        return {"status": "failed", "result_text": truncated, "messages": [], "is_complete": False}
    return {"status": "done", "result_text": truncated, "messages": [], "is_complete": False}


def _rejected(reason: str) -> dict:
    return {"status": "failed", "result_text": reason, "messages": [], "is_complete": False}


def execute_resume_message(
    job: dict, allowed_projects: list[str], enabled_tools: Sequence[str] = ai_tools.ALL_TOOLS
) -> dict:
    parts = job["target"].split(":", 1)
    if len(parts) != 2:
        return _rejected(f"malformed job target: {job['target']!r}")
    tool, raw_id = parts
    if tool in ai_tools.ALL_TOOLS and tool not in enabled_tools:
        return _rejected(f"tool is disabled: {tool}")

    if tool == "claude-code":
        project_path = claude_code_source.get_project_path(raw_id)
    elif tool == "cursor":
        project_path = cursor_source.get_project_path(raw_id)
    else:
        return _rejected(f"unknown tool: {tool}")

    if not project_path:
        return _rejected("could not resolve project path for session")
    if not allowlist.is_allowed(project_path, allowed_projects):
        return _rejected(f"project path is not allow-listed: {project_path}")

    payload = _parse_json_payload(job.get("payload", ""))
    prompt = payload.get("prompt")
    if not prompt:
        return _rejected("missing prompt in job payload")

    reason = permission_profile.missing_profile_reason(project_path, tool)
    if reason:
        return _rejected(reason)

    # `--` is load-bearing, not cosmetic: the prompt is attacker-controlled text, and
    # without an end-of-options separator a prompt starting with `-` is parsed as a CLI
    # option (`--add-dir=/`, `--append-system-prompt=…`, `--mcp-config=…`), which walks
    # straight past the project allow-list.
    if tool == "claude-code":
        cmd = CLAUDE_CLI + ["-p", "--resume", raw_id, "--permission-mode", "dontAsk", "--", prompt]
    else:
        cmd = CURSOR_AGENT_CLI + ["--resume", raw_id, "-p", "--force", "--workspace", project_path, "--", prompt]

    return _run_and_report(cmd, project_path)


def execute_new_session(
    job: dict, allowed_projects: list[str], enabled_tools: Sequence[str] = ai_tools.ALL_TOOLS
) -> dict:
    project_path = job["target"]
    if not allowlist.is_allowed(project_path, allowed_projects):
        return _rejected(f"project path is not allow-listed: {project_path}")

    payload = _parse_json_payload(job.get("payload", ""))
    prompt = payload.get("prompt")
    tool = payload.get("tool")
    if not prompt or tool not in ("claude-code", "cursor"):
        return _rejected("missing prompt/tool in job payload")
    if tool not in enabled_tools:
        return _rejected(f"tool is disabled: {tool}")

    reason = permission_profile.missing_profile_reason(project_path, tool)
    if reason:
        return _rejected(reason)

    # See execute_resume_message: `--` keeps an attacker-supplied prompt out of option
    # position.
    if tool == "claude-code":
        cmd = CLAUDE_CLI + ["-p", "--permission-mode", "dontAsk", "--", prompt]
    else:
        cmd = CURSOR_AGENT_CLI + ["-p", "--force", "--workspace", project_path, "--", prompt]

    return _run_and_report(cmd, project_path)
