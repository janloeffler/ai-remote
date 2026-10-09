import os

from . import ai_tools


def _require_env(name: str, min_length: int = 32) -> str:
    """Fails fast at import when a required secret is absent or too short.

    The 32-character floor is deliberate: API_KEY is both the agent's bearer token and
    the human login password, and it is the only thing standing between the internet and
    unattended command execution on the machine running the agent. Generate it, don't
    invent it (`openssl rand -hex 32`).
    """
    value = os.environ.get(name, "")
    if len(value) < min_length:
        raise RuntimeError(f"{name} must be set to a value at least {min_length} characters long")
    return value


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    """Like the bare `int(...)` calls below, but with a message that names the variable.

    Silently coercing a malformed value to its default would be worse here than
    crashing: the operator would see the setting present in `.env` and believe it took
    effect. That matters most for TRUSTED_PROXY_HOPS, where the belief is "per-client
    login throttling is active".
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}, got {value}")
    return value


API_KEY = _require_env("API_KEY")
SECRET_KEY = _require_env("SECRET_KEY")

# Login-throttle bucketing. X-Forwarded-For is client-supplied, so it is honored only
# when the operator states how many trailing hops their own proxy appends AND which peer
# addresses that proxy actually presents. Configuring one without the other is a
# misconfiguration that would leave throttling weaker than the operator thinks, so it
# fails fast rather than degrading quietly (SEC-001).
TRUSTED_PROXY_HOPS = _int_env("TRUSTED_PROXY_HOPS", 0)
TRUSTED_PROXIES = [p.strip() for p in (os.environ.get("TRUSTED_PROXIES") or "").split(",") if p.strip()]
if TRUSTED_PROXY_HOPS > 0 and not TRUSTED_PROXIES:
    raise RuntimeError(
        "TRUSTED_PROXY_HOPS is set but TRUSTED_PROXIES is empty — X-Forwarded-For would "
        "be trusted from any peer, including a client reaching the container directly. "
        "Set TRUSTED_PROXIES to your reverse proxy's peer address(es), or leave "
        "TRUSTED_PROXY_HOPS at 0."
    )
if TRUSTED_PROXIES and TRUSTED_PROXY_HOPS == 0:
    raise RuntimeError(
        "TRUSTED_PROXIES is set but TRUSTED_PROXY_HOPS is 0, so X-Forwarded-For is "
        "ignored entirely. Set TRUSTED_PROXY_HOPS to the number of hops your proxy "
        "appends (normally 1), or unset TRUSTED_PROXIES."
    )
LOCAL_HOME_DIR = (os.environ.get("LOCAL_HOME_DIR") or "/Users/yourname").rstrip("/")
CHAT_HISTORY_PAGE_SIZE = int(os.environ.get("CHAT_HISTORY_PAGE_SIZE") or "10")
AI_REMOTE_INTERVAL_SECONDS = int(os.environ.get("AI_REMOTE_INTERVAL_SECONDS") or "60")
AI_REMOTE_ACTIVE_INTERVAL_SECONDS = int(os.environ.get("AI_REMOTE_ACTIVE_INTERVAL_SECONDS") or "10")
ACTIVE_INTERVAL_DURATION_MIN = int(os.environ.get("ACTIVE_INTERVAL_DURATION_MIN") or "5")
ALLOWED_PROJECTS = [
    p.strip() for p in (os.environ.get("AI_REMOTE_ALLOWED_PROJECTS") or "").split(",") if p.strip()
]

# Bounds for the intervals editable in the UI. The floor keeps a mis-set value from
# turning the agent into a request flood against the backend.
STANDARD_INTERVAL_RANGE = (10, 3600)
ACTIVE_INTERVAL_RANGE = (5, 300)

# Tool switches. None enabled is not fatal: the UI shows an error instead, so the
# operator sees what to fix rather than a container that restart-loops.
ENABLED_TOOLS, DEFAULT_TOOL = ai_tools.load()
