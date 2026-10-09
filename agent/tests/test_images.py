import json
from pathlib import Path
from types import SimpleNamespace

from agent import claude_code_source, images

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class FakeClient:
    def __init__(self, status=200):
        self.status = status
        self.posts = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append(json)
        return SimpleNamespace(status_code=self.status)


def _config(tmp_path, allowed=()):
    return SimpleNamespace(
        backend_url="http://b",
        api_key="k",
        state_path=tmp_path / "state" / "sync_state.json",
        allowed_projects=list(allowed),
        enabled_tools=("claude-code", "cursor"),
    )


def test_pasted_paths_come_from_user_messages_only():
    messages = [
        {"role": "user", "content": "x [Image: source: /a/one.png] y"},
        {"role": "assistant", "content": "[Image: source: /etc/secret.png]"},
    ]
    assert images.pasted_image_paths(messages) == ["/a/one.png"]


def test_read_image_accepts_png_and_rejects_text_svg_and_big(tmp_path):
    good = tmp_path / "a.png"
    good.write_bytes(PNG)
    assert images.read_image(str(good)) == PNG
    fake = tmp_path / "fake.png"
    fake.write_text("not an image")
    assert images.read_image(str(fake)) is None
    svg = tmp_path / "x.svg"
    svg.write_bytes(PNG)
    assert images.read_image(str(svg)) is None
    big = tmp_path / "big.png"
    big.write_bytes(PNG + b"\x00" * images.MAX_IMAGE_BYTES)
    assert images.read_image(str(big)) is None
    assert images.read_image(str(tmp_path / "missing.png")) is None


def test_read_image_with_roots_blocks_symlink_escape(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    (project / "link.png").symlink_to(outside)
    (project / "ok.png").write_bytes(PNG)
    roots = [project.resolve()]
    assert images.read_image(str(project / "ok.png"), allowed_roots=roots) == PNG
    assert images.read_image(str(project / "link.png"), allowed_roots=roots) is None


def _job(path):
    return {"target": "claude-code:abc", "payload": json.dumps({"path": path})}


def test_fetch_image_uploads_a_pasted_image(tmp_path, monkeypatch):
    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG)
    monkeypatch.setattr(
        claude_code_source, "get_full_messages", lambda raw: [{"role": "user", "content": f"[Image: source: {shot}]"}]
    )
    client = FakeClient()
    result = images.execute_fetch_image(_job(str(shot)), _config(tmp_path), client)
    assert result["status"] == "done"
    assert client.posts[0]["session_id"] == "claude-code:abc" and client.posts[0]["path"] == str(shot)


def test_fetch_image_refuses_paths_the_session_never_mentions(tmp_path, monkeypatch):
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    monkeypatch.setattr(claude_code_source, "get_full_messages", lambda raw: [{"role": "user", "content": "hi"}])
    client = FakeClient()
    result = images.execute_fetch_image(_job(str(secret)), _config(tmp_path), client)
    assert result["status"] == "failed" and client.posts == []


def test_fetch_image_assistant_mentioned_path_needs_allowlisted_project(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    inside = project / "chart.png"
    inside.write_bytes(PNG)
    outside = tmp_path / "other.png"
    outside.write_bytes(PNG)
    monkeypatch.setattr(
        claude_code_source,
        "get_full_messages",
        lambda raw: [{"role": "assistant", "content": f"see {inside} and {outside}"}],
    )
    config = _config(tmp_path, allowed=[str(project)])
    assert images.execute_fetch_image(_job(str(inside)), config, FakeClient())["status"] == "done"
    assert images.execute_fetch_image(_job(str(outside)), config, FakeClient())["status"] == "failed"


def test_fetch_image_rejects_disabled_tool_and_bad_payload(tmp_path):
    config = _config(tmp_path)
    config.enabled_tools = ("cursor",)
    assert images.execute_fetch_image(_job("/a.png"), config, FakeClient())["status"] == "failed"
    assert images.execute_fetch_image({"target": "cursor:x", "payload": "nope"}, config, FakeClient())["status"] == "failed"


def test_upload_pasted_images_is_remembered_and_not_repeated(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG)
    config = _config(tmp_path)
    messages = {"claude-code:abc": [{"role": "user", "content": f"[Image: source: {shot}]"}]}
    client = FakeClient()
    images.upload_pasted_images(config, client, messages)
    images.upload_pasted_images(config, client, messages)
    assert len(client.posts) == 1


def test_upload_stops_and_does_not_remember_when_backend_has_images_off(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG)
    config = _config(tmp_path)
    messages = {"claude-code:abc": [{"role": "user", "content": f"[Image: source: {shot}]"}]}
    client = FakeClient(status=403)
    images.upload_pasted_images(config, client, messages)
    client.status = 200
    images.upload_pasted_images(config, client, messages)
    assert len(client.posts) == 2
