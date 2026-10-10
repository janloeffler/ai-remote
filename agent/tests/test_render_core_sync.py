import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RENDER_PACKAGES = ("markdown", "bleach", "beautifulsoup4", "pygments")


def test_render_core_is_identical_to_backend_copy():
    backend = REPO_ROOT / "backend" / "app" / "render_core.py"
    agent = REPO_ROOT / "agent" / "agent" / "render_core.py"
    assert agent.read_bytes() == backend.read_bytes(), (
        "agent/agent/render_core.py differs from backend/app/render_core.py: "
        "copy the backend file over (cp backend/app/render_core.py agent/agent/render_core.py)"
    )


def _pins(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s\;]+)", line)
        if m:
            pins[m.group(1).lower().replace("_", "-")] = m.group(2)
    return pins


def test_render_dependencies_are_pinned_identically():
    backend = _pins(REPO_ROOT / "backend" / "requirements.txt")
    agent = _pins(REPO_ROOT / "agent" / "requirements.txt")
    for name in RENDER_PACKAGES:
        assert name in backend, f"{name} missing from backend/requirements.txt"
        assert name in agent, f"{name} missing from agent/requirements.txt"
        assert backend[name] == agent[name], (
            f"{name}: backend pins {backend[name]}, agent pins {agent[name]}; "
            "render output must stay identical, so keep both in lockstep"
        )
