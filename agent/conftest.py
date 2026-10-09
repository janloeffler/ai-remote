import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "real_handshake: run the real handshake instead of the legacy-server stub")


@pytest.fixture(autouse=True)
def _legacy_handshake(request, monkeypatch):
    """Cycle tests written before the handshake exist call run_cycle(client=None). Stub the
    handshake as a legacy backend (404 → plaintext proceeds as before) unless a test opts
    into the real one with @pytest.mark.real_handshake."""
    if request.node.get_closest_marker("real_handshake"):
        return
    monkeypatch.setattr("agent.handshake.fetch", lambda config, client: (404, None))
