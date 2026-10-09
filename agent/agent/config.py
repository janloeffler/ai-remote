import os
from dataclasses import dataclass, field
from pathlib import Path

from . import ai_tools, e2e

_TRUE = ("true", "1", "yes", "on")


def _truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in _TRUE


@dataclass
class Config:
    backend_url: str
    api_key: str
    state_path: Path
    interval_seconds: int = 60
    allowed_projects: list[str] = field(default_factory=list)
    enabled_tools: tuple[str, ...] = ai_tools.ALL_TOOLS
    image_upload_enabled: bool = False
    e2e: bool = False
    e2e_key: bytes | None = None


def _load_e2e_key() -> bytes | None:
    raw = (os.environ.get("AI_REMOTE_E2E_KEY") or "").strip()
    if not raw:
        return None
    try:
        return e2e.decode_master(raw)
    except Exception as exc:
        raise RuntimeError(f"AI_REMOTE_E2E_KEY is invalid (expected base64 of a 32-byte key): {exc}") from exc


def load_config() -> Config:
    enabled_tools, _ = ai_tools.load()
    return Config(
        backend_url=os.environ["AI_REMOTE_BACKEND_URL"].rstrip("/"),
        api_key=os.environ["AI_REMOTE_API_KEY"],
        state_path=Path(
            os.environ.get("AI_REMOTE_STATE_PATH", str(Path.home() / ".ai-remote-agent" / "sync_state.json"))
        ),
        interval_seconds=int(os.environ.get("AI_REMOTE_INTERVAL_SECONDS", "60")),
        allowed_projects=[
            p.strip() for p in (os.environ.get("AI_REMOTE_ALLOWED_PROJECTS") or "").split(",") if p.strip()
        ],
        enabled_tools=enabled_tools,
        image_upload_enabled=_truthy("IMAGE_UPLOAD_ENABLED"),
        e2e=_truthy("AI_REMOTE_E2E"),
        e2e_key=_load_e2e_key(),
    )
