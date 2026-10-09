"""Shared fixtures for the E2E cycle tests: a recording fake backend and fake local sources."""

import json

import httpx

from agent import e2e
from agent.config import Config

MASTER = bytes(range(32))
KEYS = e2e.Keys.from_master(MASTER)
CANARY = "CANARY-7f3a"
PNG = b"\x89PNG\r\n\x1a\n"


def handshake_body(server_e2e=True, epoch="E1", key_check=None, params=True):
    body = {"e2e": server_e2e, "epoch": epoch, "salt": None, "kdf": None, "key_check": None}
    if server_e2e and params:
        body.update(salt="c2FsdHNhbHRzYWx0c2FsdA==", kdf=e2e.DEFAULT_KDF, key_check=key_check or KEYS.check)
    return body


class Backend:
    """MockTransport backend that records every request."""

    def __init__(self, handshake=(200, None), jobs=()):
        self.handshake = handshake
        self.jobs = list(jobs)
        self.requests: list[httpx.Request] = []
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/agent/handshake":
            status, body = self.handshake
            return httpx.Response(status, json=body) if body is not None else httpx.Response(status)
        if path == "/jobs/pending":
            jobs, self.jobs = self.jobs, []
            return httpx.Response(200, json={"jobs": jobs, "poll_interval_seconds": 30})
        return httpx.Response(200, json={})

    def paths(self):
        return [r.url.path for r in self.requests]

    def forbidden_calls(self):
        return [p for p in self.paths() if p.startswith("/sync/") or p == "/jobs/pending" or p.startswith("/jobs/")]

    def bodies(self, path):
        return [json.loads(r.content) for r in self.requests if r.url.path == path]


def config(tmp_path, e2e_on=True, key=MASTER, **kw):
    return Config(
        backend_url="http://backend.example",
        api_key="k",
        state_path=tmp_path / "state" / "sync_state.json",
        enabled_tools=("claude-code",),
        e2e=e2e_on,
        e2e_key=key if e2e_on else None,
        **kw,
    )


def session(sid="claude-code:abc", updated="2026-01-02T00:00:00Z", title="T", preview="P", messages=()):
    return {
        "id": sid,
        "tool": "claude-code",
        "entrypoint": "cli",
        "project_path": "/proj",
        "title": title,
        "created_at": "2026-01-01T00:00:00Z",
        "last_updated_at": updated,
        "message_count": len(messages),
        "last_message_preview": preview,
        "status": "idle",
        "recent_messages": list(messages),
    }


def msg(idx, content, role="user"):
    return {"idx": idx, "role": role, "timestamp": "t", "content": content}
