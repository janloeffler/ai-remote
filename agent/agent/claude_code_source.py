import json
import re
import sys
from pathlib import Path

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Claude Code names transcripts after the session UUID; nothing legitimate needs a
# separator, a dot, or an empty id.
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")

RECENT_MESSAGES_LIMIT = 10

# Process-lifetime cache: avoids re-parsing every .jsonl file on every sync cycle.
# Keyed by absolute file path; invalidated per-file when its mtime changes.
# Reset on agent restart, which is acceptable for a long-running daemon.
_file_cache: dict[Path, tuple[float, dict]] = {}


def list_claude_code_sessions(projects_dir: Path = CLAUDE_PROJECTS_DIR) -> list[dict]:
    sessions = []
    if not projects_dir.exists():
        return sessions
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):  # non-recursive: skips subagents/*.jsonl
            try:
                session = _parse_session_file_cached(jsonl_file)
            except Exception as exc:
                # One bad file (rotated mid-glob, permissions, mid-write decode error)
                # must not take down the whole sync. Filename only — never content.
                print(f"claude_code_source: skipping {jsonl_file.name}: {exc}", file=sys.stderr)
                continue
            if session:
                sessions.append(session)
    return sessions


def _parse_session_file_cached(path: Path) -> dict | None:
    mtime = path.stat().st_mtime
    cached = _file_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    result = _parse_session_file(path)
    if result is not None:
        _file_cache[path] = (mtime, result)
    else:
        _file_cache.pop(path, None)
    return result


def _extract_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts).strip()


def _parse_session_file(path: Path) -> dict | None:
    session_id = path.stem
    ai_title = None
    last_prompt = None
    first_timestamp = None
    last_timestamp = None
    last_assistant_text = None
    last_user_text = None
    cwd = ""
    entrypoint = ""
    message_count = 0
    text_messages = []
    text_message_count = 0

    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            event_type = event.get("type")
            timestamp = event.get("timestamp")

            if event_type == "ai-title":
                ai_title = event.get("aiTitle")
            elif event_type == "last-prompt":
                last_prompt = event.get("lastPrompt")
            elif event_type == "user":
                message_count += 1
                cwd = event.get("cwd", cwd)
                entrypoint = event.get("entrypoint", entrypoint)
                text = _extract_text(event.get("message", {}))
                if text:
                    last_user_text = text
                    text_messages.append(
                        {"idx": text_message_count, "role": "user", "timestamp": timestamp or "", "content": text}
                    )
                    text_message_count += 1
                if timestamp:
                    if first_timestamp is None:
                        first_timestamp = timestamp
                    last_timestamp = timestamp
            elif event_type == "assistant":
                message_count += 1
                text = _extract_text(event.get("message", {}))
                if text:
                    last_assistant_text = text
                    text_messages.append(
                        {"idx": text_message_count, "role": "assistant", "timestamp": timestamp or "", "content": text}
                    )
                    text_message_count += 1
                if timestamp:
                    if first_timestamp is None:
                        first_timestamp = timestamp
                    last_timestamp = timestamp

    if first_timestamp is None:
        return None

    title = ai_title or (last_prompt[:80] if last_prompt else "(untitled)")
    preview = last_assistant_text or last_user_text or ""

    return {
        "id": f"claude-code:{session_id}",
        "tool": "claude-code",
        "entrypoint": entrypoint or "cli",
        "project_path": cwd,
        "title": title,
        "created_at": first_timestamp,
        "last_updated_at": last_timestamp,
        "message_count": message_count,
        "last_message_preview": preview[:500],
        "status": "idle",
        "recent_messages": text_messages[-RECENT_MESSAGES_LIMIT:],
    }


def _find_session_file(raw_session_id: str, projects_dir: Path) -> Path | None:
    """Resolves a session id to its transcript file, or None.

    The id arrives from the backend, i.e. from outside this machine, and is
    concatenated into a filesystem path — so it is validated first (SEC-005). Without
    that, an absolute id replaced `projects_dir` outright (pathlib discards the left
    operand when the right is absolute) and `../` walked out of it, making this an
    arbitrary-`.jsonl` read primitive. The containment check after the join is
    belt-and-braces for anything the pattern might still let through.
    """
    if not SESSION_ID_PATTERN.fullmatch(raw_session_id or ""):
        return None
    root = projects_dir.resolve()
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{raw_session_id}.jsonl"
        if candidate.exists() and candidate.resolve().is_relative_to(root):
            return candidate
    return None


def get_full_messages(raw_session_id: str, projects_dir: Path = CLAUDE_PROJECTS_DIR) -> list[dict]:
    path = _find_session_file(raw_session_id, projects_dir)
    if path is None:
        return []
    messages = []
    idx = 0
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") not in ("user", "assistant"):
                continue
            text = _extract_text(event.get("message", {}))
            if not text:
                continue
            messages.append(
                {"idx": idx, "role": event["type"], "timestamp": event.get("timestamp", ""), "content": text}
            )
            idx += 1
    return messages


def get_project_path(raw_session_id: str, projects_dir: Path = CLAUDE_PROJECTS_DIR) -> str | None:
    if not projects_dir.exists():
        return None
    path = _find_session_file(raw_session_id, projects_dir)
    if path is None:
        return None
    session = _parse_session_file_cached(path)
    return session["project_path"] if session else None
