"""Fail-closed check for a project's AI-CLI permission profile.

Remote jobs run the AI CLI in an auto-approving, non-interactive mode
(`claude --permission-mode dontAsk`, `cursor-agent --force`), so the *only* thing that
tells the CLI what it may do unattended is the target project's own permission profile.
That profile used to be a documented-but-unenforced manual step (SEC-003): a project
added to the allow-list without one ran under whatever the global default happened to
be, silently. This module turns it into a precondition — no profile, no job.

Profile locations are the ones each vendor defines for project-scoped permissions:

* Claude Code — `<project>/.claude/settings.json`
* Cursor CLI  — `<project>/.cursor/cli.json`

Both wrap their rules in `{"permissions": {"allow": [...], "deny": [...]}}`, but the
rule *types* inside are NOT interchangeable, and that difference is a security trap
rather than a cosmetic one. Cursor uses `Shell(...)` / `Mcp(...)`; Claude Code uses
`Bash(...)` / `Edit(...)`. Since `cursor-agent --force` allows commands unless
explicitly denied, a Claude-shaped profile dropped into `.cursor/cli.json` denies
nothing at all — the presence check would pass and the operator would believe the
project was confined while the job ran unconstrained. So the profile's rule types are
checked against the tool that will actually read them.
"""

import json
import re
from pathlib import Path

PROFILE_PATHS: dict[str, tuple[str, ...]] = {
    "claude-code": (".claude", "settings.json"),
    "cursor": (".cursor", "cli.json"),
}

TEMPLATES: dict[str, str] = {
    "claude-code": "docs/superpowers/reference/remote-agent-permissions.claude-code.json",
    "cursor": "docs/superpowers/reference/remote-agent-permissions.cursor-cli.json",
}

# Rule types each tool understands. Read/Write/WebFetch are common to both; the rest are
# what distinguishes a profile written for the wrong tool.
_KNOWN_TYPES: dict[str, set[str]] = {
    "claude-code": {
        "Bash", "Read", "Edit", "MultiEdit", "Write", "NotebookEdit",
        "WebFetch", "WebSearch", "Glob", "Grep", "Task", "TodoWrite", "SlashCommand",
    },
    "cursor": {"Shell", "Read", "Write", "WebFetch", "Mcp"},
}

# Types that belong unambiguously to the *other* tool. Their presence means the wrong
# template was copied in, so the rules will silently match nothing.
_FOREIGN_TYPES: dict[str, set[str]] = {
    "claude-code": {"Shell", "Mcp"},
    "cursor": {"Bash", "Edit", "MultiEdit", "NotebookEdit", "WebSearch", "Glob", "Grep"},
}

_RULE_TYPE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _rule_types(permissions: dict) -> list[str]:
    types = []
    for section in ("allow", "deny", "ask"):
        for entry in permissions.get(section) or []:
            if not isinstance(entry, str):
                continue
            match = _RULE_TYPE.match(entry.strip())
            if match:
                types.append(match.group(1))
    return types


def _setup_hint(tool: str) -> str:
    return (
        f"Remote commands run auto-approved, so this file is required before the agent "
        f"will run anything in this project. Start from {TEMPLATES[tool]} — note the "
        f"Claude Code and Cursor templates use different rule types and are not "
        f"interchangeable — and narrow it to this project."
    )


def missing_profile_reason(project_path: str, tool: str) -> str | None:
    """Returns None when `project_path` carries a usable permission profile for `tool`,
    otherwise a human-readable reason the job must be refused."""
    relative = PROFILE_PATHS.get(tool)
    if relative is None:
        return f"unknown tool: {tool}"

    profile = Path(project_path).joinpath(*relative)
    if not profile.is_file():
        return f"missing permission profile: {profile} does not exist. {_setup_hint(tool)}"

    try:
        data = json.loads(profile.read_text())
    except (OSError, ValueError) as exc:
        return f"unreadable permission profile {profile}: {exc}"

    permissions = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(permissions, dict) or not (permissions.get("allow") or permissions.get("deny")):
        return (
            f"permission profile {profile} has no permissions.allow / permissions.deny "
            f"entries. {_setup_hint(tool)}"
        )

    types = _rule_types(permissions)
    foreign = sorted({t for t in types if t in _FOREIGN_TYPES[tool]})
    if foreign:
        return (
            f"permission profile {profile} uses rule types {', '.join(foreign)}, which "
            f"{tool} does not understand — this looks like a profile written for the "
            f"other AI CLI, and its rules would match nothing. {_setup_hint(tool)}"
        )
    if not any(t in _KNOWN_TYPES[tool] for t in types):
        return (
            f"permission profile {profile} has no recognizable {tool} rules "
            f"(expected entries like {sorted(_KNOWN_TYPES[tool])[0]}(...)). {_setup_hint(tool)}"
        )
    return None
