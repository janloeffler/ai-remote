import os
from dataclasses import dataclass, field
from pathlib import Path

from . import ai_tools


@dataclass
class Config:
    backend_url: str
    api_key: str
    state_path: Path
    interval_seconds: int = 60
    allowed_projects: list[str] = field(default_factory=list)
    enabled_tools: tuple[str, ...] = ai_tools.ALL_TOOLS


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
    )
