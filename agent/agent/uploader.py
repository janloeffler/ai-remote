import sys

import httpx


def mode_headers(api_key: str, e2e: bool) -> dict:
    return {"Authorization": f"Bearer {api_key}", "X-AI-Remote-E2E": "1" if e2e else "0"}


def push_sync(
    base_url: str, api_key: str, sessions: list[dict], client: httpx.Client, *, e2e: bool = False
) -> bool:
    if not sessions:
        return True
    try:
        response = client.post(
            f"{base_url}/sync/index",
            json={"sessions": sessions},
            headers=mode_headers(api_key, e2e),
        )
        response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        print(f"push_sync failed: {exc}", file=sys.stderr)
        return False
