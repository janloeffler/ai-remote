import os

from agent.config import load_config


def test_allowed_projects_default_empty(monkeypatch):
    monkeypatch.setenv("AI_REMOTE_BACKEND_URL", "http://example.com")
    monkeypatch.setenv("AI_REMOTE_API_KEY", "key")
    monkeypatch.delenv("AI_REMOTE_ALLOWED_PROJECTS", raising=False)
    config = load_config()
    assert config.allowed_projects == []


def test_allowed_projects_parses_comma_separated_list(monkeypatch):
    monkeypatch.setenv("AI_REMOTE_BACKEND_URL", "http://example.com")
    monkeypatch.setenv("AI_REMOTE_API_KEY", "key")
    monkeypatch.setenv("AI_REMOTE_ALLOWED_PROJECTS", "/Users/jan/source/foo, /Users/jan/source/bar ,")
    config = load_config()
    assert config.allowed_projects == ["/Users/jan/source/foo", "/Users/jan/source/bar"]
