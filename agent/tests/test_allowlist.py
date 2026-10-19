from agent import allowlist


def test_is_allowed_exact_match():
    assert allowlist.is_allowed("/Users/jan/source/foo", ["/Users/jan/source/foo"]) is True


def test_is_allowed_rejects_substring_lookalike():
    assert allowlist.is_allowed("/Users/jan/source/foo-evil", ["/Users/jan/source/foo"]) is False


def test_is_allowed_ignores_trailing_slash():
    assert allowlist.is_allowed("/Users/jan/source/foo", ["/Users/jan/source/foo/"]) is True


def test_is_allowed_rejects_empty_path():
    assert allowlist.is_allowed("", [""]) is False


def test_subdirectory_of_allowed_root_is_allowed(tmp_path):
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert allowlist.is_allowed(str(sub), [str(tmp_path)]) is True
    assert allowlist.matching_root(str(sub), [str(tmp_path)]) == str(tmp_path.resolve())


def test_sibling_with_shared_prefix_is_rejected(tmp_path):
    root = tmp_path / "source"
    evil = tmp_path / "source-evil"
    root.mkdir()
    evil.mkdir()
    assert allowlist.is_allowed(str(evil), [str(root)]) is False


def test_dotdot_escape_is_rejected(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    assert allowlist.is_allowed(str(root / "sub" / ".." / ".." / "other"), [str(root)]) is False


def test_symlink_inside_root_pointing_outside_is_rejected(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside)
    assert allowlist.is_allowed(str(root / "link"), [str(root)]) is False


def test_relative_path_is_rejected(tmp_path):
    assert allowlist.is_allowed("sub", [str(tmp_path)]) is False
    assert allowlist.matching_root("", [str(tmp_path)]) is None


def test_root_slash_and_home_ancestors_are_ignored(tmp_path, monkeypatch):
    home = (tmp_path / "home" / "me").resolve()
    proj = home / "proj"
    proj.mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
    for root in ("/", str(home), str(home.parent)):
        assert allowlist.matching_root(str(proj), [root]) is None, root
    assert allowlist.matching_root(str(proj), [str(proj)]) == str(proj)
