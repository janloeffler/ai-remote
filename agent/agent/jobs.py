import httpx

from .uploader import mode_headers


def fetch_pending_jobs(base_url: str, api_key: str, client: httpx.Client) -> dict:
    response = client.get(f"{base_url}/jobs/pending", headers={"Authorization": f"Bearer {api_key}"})
    response.raise_for_status()
    return response.json()


def report_job_result(
    base_url: str,
    api_key: str,
    job_id: str,
    status: str,
    client: httpx.Client,
    result_text: str = "",
    messages: list[dict] | None = None,
    is_complete: bool = False,
    *,
    e2e: bool = False,
) -> None:
    body = {"status": status, "result_text": result_text, "messages": messages or [], "is_complete": is_complete}
    response = client.post(
        f"{base_url}/jobs/{job_id}/complete",
        json=body,
        headers=mode_headers(api_key, e2e),
    )
    response.raise_for_status()
