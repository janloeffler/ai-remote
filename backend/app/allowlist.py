import os

from . import settings


def _normalize(path: str) -> str | None:
    if not path or not os.path.isabs(path):
        return None
    return os.path.normpath(path)


def is_allowed(project_path: str) -> bool:
    """Subtree match by path component: an allowed root permits itself and everything
    below it. The backend cannot see the Mac's filesystem, so it only collapses `..` /
    `//` lexically (no symlink resolution); the agent re-checks against the real paths. A
    root of `/` is ignored."""
    path = _normalize(project_path)
    if path is None:
        return False
    for root in settings.ALLOWED_PROJECTS:
        normalized_root = _normalize(root)
        if normalized_root is None:
            continue
        if normalized_root == "/":  # would allow-list the whole filesystem
            continue
        if path == normalized_root or path.startswith(normalized_root.rstrip("/") + "/"):
            return True
    return False
