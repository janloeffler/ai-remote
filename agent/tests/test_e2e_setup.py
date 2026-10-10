import json

import httpx
import pytest

from agent import e2e, e2e_setup

SMALL = {"alg": "argon2id", "m": 19456, "t": 2, "p": 1, "v": 19}
PASS = "a long enough passphrase"
ENV = {"AI_REMOTE_BACKEND_URL": "http://srv/", "AI_REMOTE_API_KEY": "k"}


@pytest.fixture(autouse=True)
def small_kdf(monkeypatch):
    monkeypatch.setattr(e2e_setup, "KDF", SMALL)


class Server:
    def __init__(self, handshake, put_status=200):
        self.handshake = handshake
        self.put_status = put_status
        self.puts = []
        self.auth = set()
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request):
        self.auth.add(request.headers.get("authorization"))
        if request.method == "GET" and request.url.path == "/agent/handshake":
            return httpx.Response(200, json=self.handshake)
        if request.method == "PUT" and request.url.path == "/agent/e2e-params":
            self.puts.append(json.loads(request.content))
            return httpx.Response(self.put_status, json=self.handshake)
        return httpx.Response(404)


def hs(salt=None, kdf=None, check=None, e2e_on=True):
    return {"e2e": e2e_on, "epoch": "x", "salt": salt, "kdf": kdf, "key_check": check}


def configured(passphrase=PASS):
    salt = bytes(range(16))
    master = e2e.derive_master(passphrase, salt, SMALL)
    return hs(e2e.b64e(salt), SMALL, e2e.Keys.from_master(master).check), e2e.encode_master(master)


def getpass_seq(*answers):
    it = iter(answers)
    return lambda prompt="": next(it)


def run(server, capsys, *answers, argv=(), env=ENV):
    rc = e2e_setup.main(list(argv), env, server.client, getpass_seq(*answers))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_server_not_e2e(capsys):
    s = Server(hs(e2e_on=False))
    rc, out, err = run(s, capsys)
    assert rc == 1 and out == "" and "not in E2E mode" in err


def test_fresh_params(capsys):
    s = Server(hs())
    rc, out, _ = run(s, capsys, PASS, PASS)
    assert rc == 0
    body = s.puts[0]
    assert body["reset"] is False and body["kdf"] == SMALL
    master = e2e.derive_master(PASS, e2e.b64d(body["salt"]), SMALL)
    assert out == e2e.encode_master(master) + "\n"
    assert body["key_check"] == e2e.Keys.from_master(master).check
    assert s.auth == {"Bearer k"}


def test_existing_params_correct(capsys):
    h, key = configured()
    s = Server(h)
    rc, out, _ = run(s, capsys, PASS)
    assert rc == 0 and out == key + "\n" and s.puts == []


def test_existing_params_wrong_passphrase(capsys):
    h, _ = configured()
    rc, out, err = run(Server(h), capsys, "totally different one")
    assert rc == 1 and out == "" and "Wrong passphrase" in err


def test_existing_key_reused_without_prompt(capsys):
    h, key = configured()
    s = Server(h)
    rc, out, _ = run(s, capsys, env={**ENV, "AI_REMOTE_E2E_KEY": key})
    assert rc == 0 and out == key + "\n" and s.puts == []


def test_existing_key_mismatch_falls_back_to_prompt(capsys):
    h, key = configured()
    bad = e2e.encode_master(bytes(32))
    rc, out, _ = run(Server(h), capsys, PASS, env={**ENV, "AI_REMOTE_E2E_KEY": bad})
    assert rc == 0 and out == key + "\n"


def test_rotate(capsys):
    h, old_key = configured()
    s = Server(h)
    rc, out, _ = run(s, capsys, "brand new passphrase!", "brand new passphrase!", argv=["--rotate"],
                     env={**ENV, "AI_REMOTE_E2E_KEY": old_key})
    assert rc == 0 and s.puts[0]["reset"] is True
    assert out.strip() != old_key
    assert e2e.b64d(s.puts[0]["salt"]) != bytes(range(16))


def test_passphrase_too_short(capsys):
    s = Server(hs())
    rc, out, err = run(s, capsys, "short", "short")
    assert rc == 1 and out == "" and "at least 16" in err and s.puts == []


def test_passphrase_mismatch(capsys):
    s = Server(hs())
    rc, out, err = run(s, capsys, PASS, PASS + "x")
    assert rc == 1 and out == "" and "do not match" in err and s.puts == []


def test_put_409(capsys):
    s = Server(hs(), put_status=409)
    rc, out, err = run(s, capsys, PASS, PASS)
    assert rc == 1 and out == "" and "409" in err


def test_missing_env(capsys):
    rc, _, err = run(Server(hs()), capsys, env={})
    assert rc == 1 and "AI_REMOTE_BACKEND_URL" in err


def test_old_server_404(capsys):
    s = Server(hs())
    s.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    rc, _, err = run(s, capsys)
    assert rc == 1 and "404" in err


def test_non_json_handshake_body(capsys):
    s = Server(hs())
    s.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>login</html>")))
    rc, _, err = run(s, capsys)
    assert rc == 1 and "did not return JSON" in err and "Traceback" not in err
