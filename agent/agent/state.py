import json
from pathlib import Path

DEFAULT_STATE_PATH = Path.home() / ".ai-remote-agent" / "sync_state.json"


def load_synced_ids(state_path: Path = DEFAULT_STATE_PATH) -> dict:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_synced_ids(synced: dict, state_path: Path = DEFAULT_STATE_PATH) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(synced))


def compute_deltas(sessions: list[dict], synced: dict) -> list[dict]:
    return [s for s in sessions if synced.get(s["id"]) != s["last_updated_at"]]
