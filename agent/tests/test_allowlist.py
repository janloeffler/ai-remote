from agent import allowlist


def test_is_allowed_exact_match():
    assert allowlist.is_allowed("/Users/jan/source/foo", ["/Users/jan/source/foo"]) is True


def test_is_allowed_rejects_substring_lookalike():
    assert allowlist.is_allowed("/Users/jan/source/foo-evil", ["/Users/jan/source/foo"]) is False


def test_is_allowed_ignores_trailing_slash():
    assert allowlist.is_allowed("/Users/jan/source/foo", ["/Users/jan/source/foo/"]) is True


def test_is_allowed_rejects_empty_path():
    assert allowlist.is_allowed("", [""]) is False
