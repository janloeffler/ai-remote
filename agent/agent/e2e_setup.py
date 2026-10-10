"""Passphrase setup for E2E: python -m agent.e2e_setup [--rotate].

stdout carries only the base64 master key (setup-agent.sh captures it); prompts go to
the TTY via getpass and every message goes to stderr.
"""

import argparse
import getpass
import os
import sys

import httpx

from . import e2e

KDF = e2e.DEFAULT_KDF
MIN_PASSPHRASE_LEN = 16


class SetupError(Exception):
    pass


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _handshake(client: httpx.Client, base: str, headers: dict) -> dict:
    try:
        resp = client.get(f"{base}/agent/handshake", headers=headers)
    except httpx.HTTPError as exc:
        raise SetupError(f"Cannot reach the backend: {exc}") from exc
    if resp.status_code == 404:
        raise SetupError("The backend does not support E2E (404 on /agent/handshake). Update the server.")
    if resp.status_code in (401, 403):
        raise SetupError("The backend rejected AI_REMOTE_API_KEY.")
    if resp.status_code != 200:
        raise SetupError(f"Handshake failed: HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise SetupError("Handshake failed: the backend did not return JSON (wrong URL or proxy?).") from exc
    if not isinstance(data, dict):
        raise SetupError("Handshake failed: unexpected response from the backend.")
    return data


def _new_passphrase(getpass_fn) -> str:
    first = getpass_fn("New passphrase (min 16 characters): ")
    if len(first) < MIN_PASSPHRASE_LEN:
        raise SetupError(f"Passphrase must be at least {MIN_PASSPHRASE_LEN} characters.")
    if getpass_fn("Repeat passphrase: ") != first:
        raise SetupError("Passphrases do not match.")
    return first


def _put_params(client: httpx.Client, base: str, headers: dict, passphrase: str, reset: bool) -> bytes:
    salt = e2e.new_salt()
    master = e2e.derive_master(passphrase, salt, KDF)
    body = {
        "salt": e2e.b64e(salt),
        "kdf": KDF,
        "key_check": e2e.Keys.from_master(master).check,
        "reset": reset,
    }
    try:
        resp = client.put(f"{base}/agent/e2e-params", headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise SetupError(f"Cannot reach the backend: {exc}") from exc
    if resp.status_code == 409:
        raise SetupError(
            "The backend refused the parameters (409): it is not in E2E mode, or a passphrase is "
            "already set. Re-run without a new passphrase, or use --rotate to replace it "
            "(this wipes the server cache)."
        )
    if resp.status_code != 200:
        raise SetupError(f"Setting E2E parameters failed: HTTP {resp.status_code}")
    return master


def _run(args, env: dict, client: httpx.Client, getpass_fn) -> str:
    try:
        base = env["AI_REMOTE_BACKEND_URL"].rstrip("/")
        headers = {"Authorization": f"Bearer {env['AI_REMOTE_API_KEY']}"}
    except KeyError as exc:
        raise SetupError(f"Missing environment variable {exc.args[0]}.") from exc

    hs = _handshake(client, base, headers)
    if not hs.get("e2e"):
        raise SetupError("The backend is not in E2E mode (set E2E_ENCRYPTION=true on the server).")

    params_set = bool(hs.get("salt") and hs.get("kdf") and hs.get("key_check"))

    existing = (env.get("AI_REMOTE_E2E_KEY") or "").strip()
    if existing and params_set and not args.rotate:
        try:
            if e2e.Keys.from_master(e2e.decode_master(existing)).check == hs["key_check"]:
                _err("Existing key matches the server; nothing to do.")
                return existing
        except ValueError:
            pass
        _err("Existing AI_REMOTE_E2E_KEY does not match the server; asking for the passphrase.")

    if args.rotate:
        master = _put_params(client, base, headers, _new_passphrase(getpass_fn), reset=True)
        _err("Passphrase rotated; the server wiped its cache and the agent will resync.")
    elif not params_set:
        master = _put_params(client, base, headers, _new_passphrase(getpass_fn), reset=False)
        _err("Passphrase set.")
    else:
        passphrase = getpass_fn("Passphrase: ")
        try:
            master = e2e.derive_master(passphrase, e2e.b64d(hs["salt"]), hs["kdf"])
        except ValueError as exc:
            raise SetupError(f"Server sent unusable parameters: {exc}") from exc
        if e2e.Keys.from_master(master).check != hs["key_check"]:
            raise SetupError("Wrong passphrase.")
    return e2e.encode_master(master)


def main(argv=None, env=None, client=None, getpass_fn=getpass.getpass) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent.e2e_setup", description=__doc__)
    parser.add_argument("--rotate", action="store_true", help="set a new passphrase (wipes the server cache)")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    own_client = client is None
    client = client or httpx.Client(timeout=30)
    try:
        key = _run(args, env, client, getpass_fn)
    except SetupError as exc:
        _err(f"error: {exc}")
        return 1
    except (EOFError, KeyboardInterrupt):
        # getpass without a terminal (e.g. a non-interactive shell) reads EOF.
        _err("error: the passphrase prompt needs an interactive terminal — run ./setup-agent.sh in Terminal.")
        return 1
    finally:
        if own_client:
            client.close()
    print(key)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
