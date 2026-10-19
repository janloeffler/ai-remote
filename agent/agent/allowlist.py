"""Subtree allow-list: an `AI_REMOTE_ALLOWED_PROJECTS` entry allows the directory itself
and everything below it.

Matching is by path *component*, never by string prefix (`/x/source` must not allow
`/x/source-evil`), and both sides are symlink-resolved first, so a symlink inside an
allowed tree that points outside it — or a `..` hop — is judged by where it really leads.
Roots that resolve to `/`, `$HOME` or an ancestor of `$HOME` are ignored: they would
allow-list the whole home directory (and with it every other project and credential).
"""

import os
from pathlib import Path


def matching_root(project_path: str, allowed_projects: list[str]) -> str | None:
    """Returns the (resolved) allowed root that contains `project_path`, else None."""
    if not project_path or not os.path.isabs(project_path):
        return None
    resolved = os.path.realpath(project_path)
    home = Path.home().resolve()
    for root in allowed_projects:
        if not root or not os.path.isabs(root):
            continue
        resolved_root = os.path.realpath(root)
        if resolved_root == "/" or home == Path(resolved_root) or Path(resolved_root) in home.parents:
            continue
        if resolved == resolved_root or resolved.startswith(resolved_root.rstrip("/") + "/"):
            return resolved_root
    return None


def is_allowed(project_path: str, allowed_projects: list[str]) -> bool:
    return matching_root(project_path, allowed_projects) is not None
