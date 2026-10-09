"""Which AI tools are switched on (CLAUDE_CODE_ENABLED / CURSOR_ENABLED / DEFAULT_AI).

The backend and the agent each carry their own copy of this module (they are separate
deployables sharing no code) — keep ``agent/agent/ai_tools.py`` identical.
"""

import os
from collections.abc import Mapping

CLAUDE_CODE = "claude-code"
CURSOR = "cursor"
ALL_TOOLS = (CLAUDE_CODE, CURSOR)

_ENABLE_VARS = {CLAUDE_CODE: "CLAUDE_CODE_ENABLED", CURSOR: "CURSOR_ENABLED"}
_DEFAULT_ALIASES = {"claude": CLAUDE_CODE, "claude-code": CLAUDE_CODE, "cursor": CURSOR}
_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}

NONE_ENABLED_MESSAGE = (
    "All AI tools are disabled: set CLAUDE_CODE_ENABLED=true and/or CURSOR_ENABLED=true in .env."
)


def _bool_env(env: Mapping[str, str], name: str) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if raw == "":
        return True
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    # Failing loudly beats guessing: an operator who typed "flase" believes the tool is off.
    raise RuntimeError(f"{name} must be true or false, got {env.get(name)!r}")


def load(env: Mapping[str, str] | None = None) -> tuple[tuple[str, ...], str | None]:
    """Returns (enabled tools, default tool). The default is None when nothing is enabled."""
    env = os.environ if env is None else env
    enabled = tuple(tool for tool in ALL_TOOLS if _bool_env(env, _ENABLE_VARS[tool]))

    raw_default = (env.get("DEFAULT_AI") or "").strip().lower()
    if raw_default == "":
        preferred = CLAUDE_CODE
    elif raw_default in _DEFAULT_ALIASES:
        preferred = _DEFAULT_ALIASES[raw_default]
    else:
        raise RuntimeError(f"DEFAULT_AI must be one of claude, cursor, got {env.get('DEFAULT_AI')!r}")

    if not enabled:
        return enabled, None
    # A lone enabled tool always wins over DEFAULT_AI, whatever the .env says.
    return enabled, preferred if preferred in enabled else enabled[0]
