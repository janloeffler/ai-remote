import sys
import time

import httpx

from . import ai_tools, claude_code_source, cursor_source, executor, images, jobs, state, uploader
from .config import Config, load_config


def _upload_images(config: Config, client: httpx.Client, session_messages: dict[str, list[dict]]) -> None:
    # Images are a nicety: whatever goes wrong here must not break syncing or a job.
    try:
        images.upload_pasted_images(config, client, session_messages)
    except Exception as exc:
        print(f"image upload failed: {exc}", file=sys.stderr)


def run_cycle(config: Config, client: httpx.Client) -> int | None:
    if not config.enabled_tools:
        print(ai_tools.NONE_ENABLED_MESSAGE, file=sys.stderr)
        return None

    claude_sessions = (
        claude_code_source.list_claude_code_sessions() if ai_tools.CLAUDE_CODE in config.enabled_tools else []
    )
    cursor_sessions = cursor_source.list_cursor_sessions() if ai_tools.CURSOR in config.enabled_tools else []

    synced = state.load_synced_ids(config.state_path)

    changed_cursor_sessions = state.compute_deltas(cursor_sessions, synced)
    cursor_source.enrich_with_messages(changed_cursor_sessions)

    all_sessions = claude_sessions + changed_cursor_sessions
    deltas = state.compute_deltas(all_sessions, synced)
    if uploader.push_sync(config.backend_url, config.api_key, deltas, client=client):
        for s in deltas:
            synced[s["id"]] = s["last_updated_at"]
        state.save_synced_ids(synced, config.state_path)
        if config.image_upload_enabled:
            _upload_images(config, client, {s["id"]: s.get("recent_messages", []) for s in deltas})

    next_interval = None
    try:
        response = jobs.fetch_pending_jobs(config.backend_url, config.api_key, client)
        pending_jobs = response["jobs"]
        next_interval = response.get("poll_interval_seconds")
    except httpx.HTTPError as exc:
        print(f"fetch_pending_jobs failed: {exc}", file=sys.stderr)
        pending_jobs = []

    for job in pending_jobs:
        try:
            if job["type"] == "fetch_full":
                result = executor.execute_fetch_full(job, config.enabled_tools)
            elif job["type"] == "resume_message":
                result = executor.execute_resume_message(job, config.allowed_projects, config.enabled_tools)
            elif job["type"] == "new_session":
                result = executor.execute_new_session(job, config.allowed_projects, config.enabled_tools)
            elif job["type"] == "fetch_image" and config.image_upload_enabled:
                result = images.execute_fetch_image(job, config, client)
            else:
                result = {"status": "failed", "result_text": f"job type {job['type']} not supported in this version"}
            jobs.report_job_result(
                config.backend_url,
                config.api_key,
                job["id"],
                result["status"],
                client,
                result.get("result_text", ""),
                result.get("messages"),
                is_complete=result.get("is_complete", False),
            )
            if job["type"] == "fetch_full" and result["status"] == "done" and config.image_upload_enabled:
                _upload_images(config, client, {job["target"]: result.get("messages") or []})
        except Exception as exc:
            print(f"job {job.get('id')} failed: {exc}", file=sys.stderr)
            continue

    return next_interval


def main() -> None:
    config = load_config()
    with httpx.Client(timeout=10) as client:
        while True:
            try:
                next_interval = run_cycle(config, client)
            except Exception as exc:
                print(f"cycle failed: {exc}", file=sys.stderr)
                next_interval = None
            time.sleep(next_interval if next_interval is not None else config.interval_seconds)


if __name__ == "__main__":
    main()
