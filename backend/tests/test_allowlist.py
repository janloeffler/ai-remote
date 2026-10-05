def test_is_allowed_exact_match(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key-1234567890-abcdefgh")
    monkeypatch.setenv("SECRET_KEY", "test-secret-value-1234567890abcd")
    from app import settings, allowlist

    monkeypatch.setattr(settings, "ALLOWED_PROJECTS", ["/Users/jan/source/foo", "/Users/jan/source/bar"])
    assert allowlist.is_allowed("/Users/jan/source/foo") is True


def test_is_allowed_rejects_substring_lookalike(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key-1234567890-abcdefgh")
    monkeypatch.setenv("SECRET_KEY", "test-secret-value-1234567890abcd")
    from app import settings, allowlist

    monkeypatch.setattr(settings, "ALLOWED_PROJECTS", ["/Users/jan/source/foo"])
    assert allowlist.is_allowed("/Users/jan/source/foo-evil") is False


def test_is_allowed_ignores_trailing_slash(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key-1234567890-abcdefgh")
    monkeypatch.setenv("SECRET_KEY", "test-secret-value-1234567890abcd")
    from app import settings, allowlist

    monkeypatch.setattr(settings, "ALLOWED_PROJECTS", ["/Users/jan/source/foo/"])
    assert allowlist.is_allowed("/Users/jan/source/foo") is True


def test_is_allowed_rejects_empty_path(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key-1234567890-abcdefgh")
    monkeypatch.setenv("SECRET_KEY", "test-secret-value-1234567890abcd")
    from app import settings, allowlist

    monkeypatch.setattr(settings, "ALLOWED_PROJECTS", [""])
    assert allowlist.is_allowed("") is False
