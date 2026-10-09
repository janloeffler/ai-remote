"""Per-cycle handshake with the backend: decides whether a cycle may talk to it at all.

Fail-closed: whenever the agent's mode/key and the server's disagree, the cycle is
stopped *before* any `/sync/*` or `/jobs/pending` call (claiming jobs would mark them
`running` without a result ever arriving).

Epoch: the server regenerates `epoch` whenever it drops its content cache (mode switch,
passphrase rotation). When it differs from the epoch stored next to the sync state, the
agent's idea of "already synced" is void, so sync_state.json and images_state.json are
cleared. The very first run (no stored epoch) clears them too: a first-time relationship
with this epoch gives no guarantee that the server holds what the local state claims
(e.g. state left over from before the server was wiped or reinstalled). One extra full
resync is cheap; a silently missing chat is not. Plaintext mode treats the epoch identically.
"""

import hmac
import json
import sys

import httpx

from . import e2e


def fetch(config, client: httpx.Client) -> tuple[int, dict | None]:
    """GET /agent/handshake. Returns (status, json body or None). Raises httpx.HTTPError."""
    response = client.get(f"{config.backend_url}/agent/handshake", headers={"Authorization": f"Bearer {config.api_key}"})
    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError:
            return 200, None
        return 200, body if isinstance(body, dict) else None
    return response.status_code, None


def _epoch_path(config):
    return config.state_path.with_name("epoch.json")


def _stored_epoch(config) -> str | None:
    try:
        data = json.loads(_epoch_path(config).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    epoch = data.get("epoch") if isinstance(data, dict) else None
    return epoch if isinstance(epoch, str) else None


def _apply_epoch(config, epoch) -> None:
    if not isinstance(epoch, str) or not epoch:
        return
    if _stored_epoch(config) == epoch:
        return
    for path in (config.state_path, config.state_path.with_name("images_state.json")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    path = _epoch_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"epoch": epoch}))
    print("handshake: new server data epoch, local sync state reset (full resync)", file=sys.stderr)


def _stop(reason: str) -> tuple[bool, None]:
    print(f"handshake: {reason}; skipping this cycle, nothing sent", file=sys.stderr)
    return False, None


def perform(config, client: httpx.Client) -> tuple[bool, "e2e.Keys | None"]:
    """Returns (proceed, keys). keys is not None only in E2E mode."""
    try:
        status, body = fetch(config, client)
    except httpx.HTTPError as exc:
        return _stop(f"request failed ({exc})")

    if status == 404:
        if config.e2e:
            return _stop("E2E is enabled but the backend has no /agent/handshake (old server)")
        return True, None  # old server, plaintext agent: behaves as before
    if status != 200 or body is None:
        return _stop(f"unexpected response (HTTP {status})")

    server_e2e = body.get("e2e")
    if not isinstance(server_e2e, bool):
        return _stop("malformed handshake response")
    if server_e2e != config.e2e:
        return _stop(
            f"mode mismatch (backend E2E={'on' if server_e2e else 'off'}, "
            f"agent AI_REMOTE_E2E={'on' if config.e2e else 'off'})"
        )

    keys = None
    if config.e2e:
        if not (body.get("salt") and body.get("kdf") and body.get("key_check")):
            return _stop("backend has no E2E parameters yet; run setup-agent.sh")
        if config.e2e_key is None:
            return _stop("AI_REMOTE_E2E_KEY is missing; run setup-agent.sh")
        keys = e2e.Keys.from_master(config.e2e_key)
        if not isinstance(body["key_check"], str) or not hmac.compare_digest(keys.check, body["key_check"]):
            return _stop("wrong key (passphrase rotated or key mismatch); run setup-agent.sh")

    _apply_epoch(config, body.get("epoch"))
    return True, keys
