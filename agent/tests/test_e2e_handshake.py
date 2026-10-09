import pytest

from agent import main, state
from agent.config import Config

from e2e_harness import KEYS, Backend, config, handshake_body, session

pytestmark = pytest.mark.real_handshake


@pytest.fixture(autouse=True)
def sources(monkeypatch):
    monkeypatch.setattr("agent.claude_code_source.list_claude_code_sessions", lambda: [session()])
    monkeypatch.setattr("agent.cursor_source.list_cursor_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.enrich_with_messages", lambda s: None)


def _run(tmp_path, handshake, **cfg):
    backend = Backend(handshake=handshake)
    cfg = {"e2e_on": True, **cfg}
    result = main.run_cycle(config(tmp_path, **cfg), backend.client)
    return backend, result


STOP_CASES = {
    "server e2e, agent plain": ((200, handshake_body(True)), {"e2e_on": False}),
    "server plain, agent e2e": ((200, handshake_body(False)), {}),
    "params null": ((200, handshake_body(True, params=False)), {}),
    "wrong key check": ((200, handshake_body(True, key_check="0" * 64)), {}),
    "missing key": ((200, handshake_body(True)), {"key": None}),
    "404 in e2e": ((404, None), {}),
    "500": ((500, None), {}),
    "malformed json": ((200, None), {}),
}


@pytest.mark.parametrize("name", list(STOP_CASES))
def test_fail_closed_sends_nothing(tmp_path, name):
    handshake, cfg = STOP_CASES[name]
    if "key" in cfg:  # config() forces e2e_key=None only via e2e_on; build explicitly
        c = config(tmp_path)
        c = Config(**{**c.__dict__, "e2e_key": None})
        backend = Backend(handshake=handshake)
        assert main.run_cycle(c, backend.client) is None
    else:
        backend, result = _run(tmp_path, handshake, **cfg)
        assert result is None
    assert backend.paths() == ["/agent/handshake"]
    assert backend.forbidden_calls() == []


def test_network_error_skips_cycle(tmp_path):
    import httpx

    def boom(request):
        raise httpx.ConnectError("down")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    assert main.run_cycle(config(tmp_path), client) is None


def test_404_with_plaintext_agent_proceeds_as_today(tmp_path):
    backend, result = _run(tmp_path, (404, None), e2e_on=False)
    assert result == 30
    assert "/sync/index" in backend.paths() and "/jobs/pending" in backend.paths()
    assert not (tmp_path / "state" / "epoch.json").exists()


def test_plaintext_both_sides_proceeds_and_sends_plaintext(tmp_path):
    backend, _ = _run(tmp_path, (200, handshake_body(False)), e2e_on=False)
    assert backend.bodies("/sync/index")[0]["sessions"][0]["title"] == "T"


def test_matching_e2e_proceeds_with_ciphertext(tmp_path):
    backend, _ = _run(tmp_path, (200, handshake_body(True)))
    sent = backend.bodies("/sync/index")[0]["sessions"][0]
    assert sent["title"].startswith("e2e1:")


def test_epoch_change_resets_sync_and_image_state(tmp_path):
    cfg = config(tmp_path)
    b1 = Backend(handshake=(200, handshake_body(True, epoch="E1")))
    main.run_cycle(cfg, b1.client)
    assert len(b1.bodies("/sync/index")) == 1
    images_state = cfg.state_path.with_name("images_state.json")
    images_state.write_text('["x"]')

    # same epoch: nothing to push, image state kept
    b2 = Backend(handshake=(200, handshake_body(True, epoch="E1")))
    main.run_cycle(cfg, b2.client)
    assert b2.bodies("/sync/index") == [] and images_state.exists()
    assert state.load_synced_ids(cfg.state_path) != {}

    # new epoch: both cleared, full resync
    b3 = Backend(handshake=(200, handshake_body(True, epoch="E2")))
    main.run_cycle(cfg, b3.client)
    assert len(b3.bodies("/sync/index")) == 1
    assert not images_state.exists()
    assert cfg.state_path.with_name("epoch.json").read_text() == '{"epoch": "E2"}'


def test_first_run_resets_stale_sync_state(tmp_path):
    cfg = config(tmp_path, e2e_on=False)
    state.save_synced_ids({"claude-code:abc": "2026-01-02T00:00:00Z"}, cfg.state_path)
    b = Backend(handshake=(200, handshake_body(False)))
    main.run_cycle(cfg, b.client)
    assert len(b.bodies("/sync/index")) == 1


def test_config_e2e_env(monkeypatch):
    from agent import e2e
    from agent.config import load_config

    monkeypatch.setenv("AI_REMOTE_BACKEND_URL", "http://x")
    monkeypatch.setenv("AI_REMOTE_API_KEY", "k")
    monkeypatch.delenv("AI_REMOTE_E2E", raising=False)
    monkeypatch.delenv("AI_REMOTE_E2E_KEY", raising=False)
    c = load_config()
    assert (c.e2e, c.e2e_key) == (False, None)
    monkeypatch.setenv("AI_REMOTE_E2E", "True")
    assert load_config().e2e_key is None
    monkeypatch.setenv("AI_REMOTE_E2E_KEY", e2e.encode_master(bytes(range(32))))
    assert load_config().e2e_key == bytes(range(32))
    monkeypatch.setenv("AI_REMOTE_E2E_KEY", "garbage")
    with pytest.raises(RuntimeError, match="AI_REMOTE_E2E_KEY"):
        load_config()
