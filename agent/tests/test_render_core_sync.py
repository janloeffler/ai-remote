from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_render_core_is_identical_to_backend_copy():
    backend = REPO_ROOT / "backend" / "app" / "render_core.py"
    agent = REPO_ROOT / "agent" / "agent" / "render_core.py"
    assert agent.read_bytes() == backend.read_bytes(), (
        "agent/agent/render_core.py differs from backend/app/render_core.py: "
        "copy the backend file over (cp backend/app/render_core.py agent/agent/render_core.py)"
    )
