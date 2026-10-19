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

Because an allow-list entry covers its whole subtree, a Claude Code job in a subdirectory
may be governed by a profile further up (at most as far as the allow-list entry itself,
and the root-most valid one wins so its denies always apply);
Cursor cannot be pointed at another file, so it needs its profile in the workspace.

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
import os
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


def _validate(profile: Path, tool: str) -> str | None:
    """None when the file at `profile` is a usable permission profile for `tool`,
    otherwise the reason it is not."""
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


def missing_profile_reason(project_path: str, tool: str) -> str | None:
    """Returns None when `project_path` itself carries a usable permission profile for
    `tool`, otherwise a human-readable reason the job must be refused."""
    relative = PROFILE_PATHS.get(tool)
    if relative is None:
        return f"unknown tool: {tool}"
    return _validate(Path(project_path).joinpath(*relative), tool)


def resolve_profile(project_path: str, tool: str, root: str) -> tuple[Path | None, str | None]:
    """Decides whether a job in `project_path` has a governing permission profile.

    Returns `(settings_file, None)` when the job is accepted, or `(None, reason)` when it
    must be refused. `settings_file` is the profile the executor must hand to Claude Code
    with `--settings`, or None when nothing extra is needed (Cursor, or the project's own
    file is already the governing one).

    Claude Code always auto-loads `<cwd>/.claude/settings.json` (no parent fallback) and
    `--settings` adds a higher-precedence layer; permission arrays merge and deny wins.
    Hence: a cwd file that exists but is invalid is refused outright (Claude would load it
    anyway); the file passed via `--settings` is the *root-most* valid profile between the
    project and `root` (the matching allow-list entry, never above it), so the root's
    denies cannot be dropped by a nearer profile; invalid profiles in intermediate
    directories are skipped, since Claude does not load them. The user-scope
    `~/.claude/settings.json` is never treated as a project profile. Cursor can only read
    `<workspace>/.cursor/cli.json`, so for it the project directory is the only place.
    """
    relative = PROFILE_PATHS.get(tool)
    if relative is None:
        return None, f"unknown tool: {tool}"

    project = Path(os.path.realpath(project_path))
    if tool == "cursor":
        profile = project.joinpath(*relative)
        reason = _validate(profile, tool)
        if reason is None:
            return None, None
        return None, (
            f"{reason} Cursor can only read the profile from the workspace itself, so "
            f"{profile} must exist; inheriting a profile from an allow-listed parent "
            f"directory only works for Claude Code."
        )

    top = Path(os.path.realpath(root))
    if project != top and top not in project.parents:
        return None, f"project path {project} is outside the allow-listed root {top}"

    user_scope = Path(os.path.realpath(Path.home())).joinpath(*relative)
    own = project.joinpath(*relative)
    valid: list[Path] = []  # nearest first
    problems = []
    for directory in (project, *project.parents):
        profile = directory.joinpath(*relative)
        if profile != user_scope and profile.exists():
            reason = _validate(profile, tool)
            if reason is None:
                valid.append(profile)
            elif profile == own:
                return None, f"invalid permission profile in the project itself (Claude Code would load it): {reason}"
            else:
                problems.append(reason)
        if directory == top:
            break
    if not valid:
        searched = f"searched {project} up to and including {top}"
        detail = " ".join(problems) if problems else _setup_hint(tool)
        return None, f"missing permission profile: no valid {'/'.join(relative)} found ({searched}). {detail}"
    governing = valid[-1]
    return (None if governing == own else governing), None
