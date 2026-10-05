def is_allowed(project_path: str, allowed_projects: list[str]) -> bool:
    if not project_path:
        return False
    normalized = project_path.rstrip("/")
    return normalized in {p.rstrip("/") for p in allowed_projects}
