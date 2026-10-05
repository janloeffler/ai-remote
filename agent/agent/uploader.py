import sys

import httpx


def push_sync(base_url: str, api_key: str, sessions: list[dict], client: httpx.Client) -> bool:
    if not sessions:
        return True
    try:
        response = client.post(
            f"{base_url}/sync/index",
            json={"sessions": sessions},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        print(f"push_sync failed: {exc}", file=sys.stderr)
        return False
