import sys
import time

import httpx

from . import ai_tools, claude_code_source, cursor_source, executor, handshake, images, jobs, search, seal, state, uploader
from .config import Config, load_config


def _upload_images(config: Config, client: httpx.Client, session_messages: dict[str, list[dict]], keys=None) -> None:
    # Images are a nicety: whatever goes wrong here must not break syncing or a job.
    try:
        images.upload_pasted_images(config, client, session_messages, keys)
    except Exception as exc:
        print(f"image upload failed: {exc}", file=sys.stderr)


def _execute_job(job: dict, config: Config, client: httpx.Client, keys) -> dict:
    if job["type"] == "fetch_full":
        return executor.execute_fetch_full(job, config.enabled_tools)
    if job["type"] == "resume_message":
        if keys is not None:
            job = seal.open_job_prompt(job, keys, config.state_path)
        return executor.execute_resume_message(job, config.allowed_projects, config.enabled_tools)
    if job["type"] == "new_session":
        if keys is not None:
            job = seal.open_job_prompt(job, keys, config.state_path)
        return executor.execute_new_session(job, config.allowed_projects, config.enabled_tools)
    if job["type"] == "fetch_image" and config.image_upload_enabled:
        return images.execute_fetch_image(job, config, client, keys)
    if job["type"] == "search":
        return search.execute_search(job, keys, config.enabled_tools)
    return {"status": "failed", "result_text": f"job type {job['type']} not supported in this version"}


# A full resync (first run, new server epoch) is hundreds of sessions. Sent as one request
# it outlived the client timeout and was retried whole every cycle, so it never finished.
# Batches are saved as they land; whatever exceeds the per-cycle cap goes next cycle, so
# pending jobs are never stuck behind a long resync.
SYNC_BATCH_SIZE = 20
MAX_SYNC_BATCHES_PER_CYCLE = 5


def _push_in_batches(config: Config, client: httpx.Client, deltas: list[dict], synced: dict, keys) -> None:
    for start in range(0, min(len(deltas), SYNC_BATCH_SIZE * MAX_SYNC_BATCHES_PER_CYCLE), SYNC_BATCH_SIZE):
        batch = deltas[start:start + SYNC_BATCH_SIZE]
        if keys is None:
            outgoing, pushed = batch, batch
        else:
            outgoing, pushed = [], []
            for s in batch:
                try:
                    outgoing.append(seal.seal_session(s, keys, config.image_upload_enabled))
                    pushed.append(s)
                except Exception as exc:
                    # Never log content: the exception text may quote it.
                    print(f"session {s.get('id')}: cannot seal ({type(exc).__name__})", file=sys.stderr)
        if not uploader.push_sync(config.backend_url, config.api_key, outgoing, client=client, e2e=config.e2e):
            return
        for s in pushed:
            synced[s["id"]] = s["last_updated_at"]
        state.save_synced_ids(synced, config.state_path)
        if config.image_upload_enabled:
            _upload_images(config, client, {s["id"]: s.get("recent_messages", []) for s in pushed}, keys)


def run_cycle(config: Config, client: httpx.Client) -> int | None:
    if not config.enabled_tools:
        print(ai_tools.NONE_ENABLED_MESSAGE, file=sys.stderr)
        return None

    proceed, keys = handshake.perform(config, client)
    if not proceed:
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
    _push_in_batches(config, client, deltas, synced, keys)

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
            plain_messages = None
            try:
                result = _execute_job(job, config, client, keys)
            except seal.PromptError as exc:
                result = {"status": "failed", "result_text": str(exc)}
            report_messages = result.get("messages")
            result_text = result.get("result_text", "")
            if keys is not None:
                plain_messages = report_messages
                if report_messages:
                    try:
                        report_messages = seal.seal_messages(
                            job["target"], report_messages, keys, config.image_upload_enabled
                        )
                    except Exception as exc:
                        print(f"job {job.get('id')}: cannot seal ({type(exc).__name__})", file=sys.stderr)
                        result = {"status": "failed", "result_text": "cannot render messages"}
                        result_text = result["result_text"]
                        report_messages = None
                        plain_messages = None
                result_text = seal.seal_result_text(job["id"], result_text, keys)
            jobs.report_job_result(
                config.backend_url,
                config.api_key,
                job["id"],
                result["status"],
                client,
                result_text,
                report_messages,
                is_complete=result.get("is_complete", False),
                e2e=config.e2e,
            )
            if job["type"] == "fetch_full" and result["status"] == "done" and config.image_upload_enabled:
                uploads = plain_messages if keys is not None else result.get("messages")
                _upload_images(config, client, {job["target"]: uploads or []}, keys)
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
