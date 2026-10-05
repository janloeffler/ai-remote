import pytest

from agent import main, state


def test_run_cycle_pushes_deltas_and_executes_pending_jobs(tmp_path, monkeypatch):
    calls = []

    monkeypatch.setattr(
        "agent.claude_code_source.list_claude_code_sessions",
        lambda: [{"id": "claude-code:abc", "last_updated_at": "t1"}],
    )
    monkeypatch.setattr("agent.cursor_source.list_cursor_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.enrich_with_messages", lambda sessions: None)

    def fake_push_sync(base_url, api_key, sessions, client):
        calls.append(("push", sessions))
        return True

    def fake_fetch_pending_jobs(base_url, api_key, client):
        return {"jobs": [{"id": "j1", "type": "fetch_full", "target": "claude-code:abc"}], "poll_interval_seconds": 60}

    def fake_execute_fetch_full(job):
        return {"status": "done", "result_text": "", "messages": []}

    def fake_report_job_result(base_url, api_key, job_id, status, client, result_text="", messages=None, is_complete=False):
        calls.append(("report", job_id, status))

    monkeypatch.setattr("agent.uploader.push_sync", fake_push_sync)
    monkeypatch.setattr("agent.jobs.fetch_pending_jobs", fake_fetch_pending_jobs)
    monkeypatch.setattr("agent.jobs.report_job_result", fake_report_job_result)
    monkeypatch.setattr("agent.executor.execute_fetch_full", fake_execute_fetch_full)

    config = main.Config(
        backend_url="http://backend.example",
        api_key="test-key",
        state_path=tmp_path / "sync_state.json",
        interval_seconds=60,
    )
    main.run_cycle(config, client=None)

    assert ("push", [{"id": "claude-code:abc", "last_updated_at": "t1"}]) in calls
    assert ("report", "j1", "done") in calls
    assert state.load_synced_ids(config.state_path) == {"claude-code:abc": "t1"}


def test_run_cycle_enriches_only_changed_cursor_sessions(tmp_path, monkeypatch):
    """Verify cursor sessions are delta-filtered BEFORE enrichment: enrich_with_messages
    must only ever see the changed subset, not the full list of cursor sessions.
    This is the efficiency fix — enrichment queries cursorDiskKV and is expensive."""
    state_path = tmp_path / "sync_state.json"
    unchanged_session = {"id": "cursor:unchanged", "last_updated_at": "t1"}
    changed_session = {"id": "cursor:changed", "last_updated_at": "t2-new"}

    # Pre-populate synced state so "unchanged_session" already matches what's on disk.
    state.save_synced_ids({"cursor:unchanged": "t1", "cursor:changed": "t2-old"}, state_path)

    enrich_calls = []

    monkeypatch.setattr("agent.claude_code_source.list_claude_code_sessions", lambda: [])
    monkeypatch.setattr(
        "agent.cursor_source.list_cursor_sessions",
        lambda: [unchanged_session, changed_session],
    )

    def fake_enrich(sessions):
        enrich_calls.append(sessions)

    monkeypatch.setattr("agent.cursor_source.enrich_with_messages", fake_enrich)
    monkeypatch.setattr("agent.uploader.push_sync", lambda base_url, api_key, sessions, client: True)
    monkeypatch.setattr(
        "agent.jobs.fetch_pending_jobs", lambda base_url, api_key, client: {"jobs": [], "poll_interval_seconds": 60}
    )

    config = main.Config(
        backend_url="http://backend.example",
        api_key="test-key",
        state_path=state_path,
        interval_seconds=60,
    )
    main.run_cycle(config, client=None)

    assert len(enrich_calls) == 1
    assert enrich_calls[0] == [changed_session]


def test_run_cycle_continues_after_job_failure(tmp_path, monkeypatch):
    """Verify that if report_job_result raises on one job, run_cycle continues to process remaining jobs."""
    calls = []

    monkeypatch.setattr(
        "agent.claude_code_source.list_claude_code_sessions",
        lambda: [{"id": "claude-code:abc", "last_updated_at": "t1"}],
    )
    monkeypatch.setattr("agent.cursor_source.list_cursor_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.enrich_with_messages", lambda sessions: None)

    def fake_push_sync(base_url, api_key, sessions, client):
        return True

    def fake_fetch_pending_jobs(base_url, api_key, client):
        return {
            "jobs": [
                {"id": "j1", "type": "fetch_full", "target": "claude-code:abc"},
                {"id": "j2", "type": "fetch_full", "target": "claude-code:def"},
            ],
            "poll_interval_seconds": 60,
        }

    def fake_execute_fetch_full(job):
        return {"status": "done", "result_text": "", "messages": []}

    def fake_report_job_result(base_url, api_key, job_id, status, client, result_text="", messages=None, is_complete=False):
        calls.append(("report", job_id, status))
        # Raise exception on first call (job j1), succeed on second (job j2)
        if len([c for c in calls if c[0] == "report"]) == 1:
            raise RuntimeError("network error simulating failure")

    monkeypatch.setattr("agent.uploader.push_sync", fake_push_sync)
    monkeypatch.setattr("agent.jobs.fetch_pending_jobs", fake_fetch_pending_jobs)
    monkeypatch.setattr("agent.jobs.report_job_result", fake_report_job_result)
    monkeypatch.setattr("agent.executor.execute_fetch_full", fake_execute_fetch_full)

    config = main.Config(
        backend_url="http://backend.example",
        api_key="test-key",
        state_path=tmp_path / "sync_state.json",
        interval_seconds=60,
    )
    # Should not raise, despite report_job_result failing for j1
    main.run_cycle(config, client=None)

    # Verify that both jobs were attempted to be reported (even though j1 failed)
    assert ("report", "j1", "done") in calls
    assert ("report", "j2", "done") in calls


def test_run_cycle_dispatches_resume_message_and_new_session_jobs(tmp_path, monkeypatch):
    calls = []

    monkeypatch.setattr("agent.claude_code_source.list_claude_code_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.list_cursor_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.enrich_with_messages", lambda sessions: None)
    monkeypatch.setattr("agent.uploader.push_sync", lambda base_url, api_key, sessions, client: True)

    def fake_fetch_pending_jobs(base_url, api_key, client):
        return {
            "jobs": [
                {"id": "j1", "type": "resume_message", "target": "claude-code:abc", "payload": "{}"},
                {"id": "j2", "type": "new_session", "target": "/tmp/demo", "payload": "{}"},
            ],
            "poll_interval_seconds": 60,
        }

    def fake_execute_resume_message(job, allowed_projects):
        calls.append(("resume_message", job["id"], allowed_projects))
        return {"status": "done", "result_text": "", "messages": []}

    def fake_execute_new_session(job, allowed_projects):
        calls.append(("new_session", job["id"], allowed_projects))
        return {"status": "done", "result_text": "", "messages": []}

    def fake_report_job_result(base_url, api_key, job_id, status, client, result_text="", messages=None, is_complete=False):
        calls.append(("report", job_id, status))

    monkeypatch.setattr("agent.jobs.fetch_pending_jobs", fake_fetch_pending_jobs)
    monkeypatch.setattr("agent.jobs.report_job_result", fake_report_job_result)
    monkeypatch.setattr("agent.executor.execute_resume_message", fake_execute_resume_message)
    monkeypatch.setattr("agent.executor.execute_new_session", fake_execute_new_session)

    config = main.Config(
        backend_url="http://backend.example",
        api_key="test-key",
        state_path=tmp_path / "sync_state.json",
        interval_seconds=60,
        allowed_projects=["/tmp/demo"],
    )
    main.run_cycle(config, client=None)

    assert ("resume_message", "j1", ["/tmp/demo"]) in calls
    assert ("new_session", "j2", ["/tmp/demo"]) in calls
    assert ("report", "j1", "done") in calls
    assert ("report", "j2", "done") in calls


def test_run_cycle_returns_poll_interval_from_response(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.claude_code_source.list_claude_code_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.list_cursor_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.enrich_with_messages", lambda sessions: None)
    monkeypatch.setattr("agent.uploader.push_sync", lambda base_url, api_key, sessions, client: True)
    monkeypatch.setattr(
        "agent.jobs.fetch_pending_jobs",
        lambda base_url, api_key, client: {"jobs": [], "poll_interval_seconds": 10},
    )

    config = main.Config(
        backend_url="http://backend.example",
        api_key="test-key",
        state_path=tmp_path / "sync_state.json",
        interval_seconds=60,
    )
    result = main.run_cycle(config, client=None)
    assert result == 10


def test_run_cycle_returns_none_when_fetch_pending_jobs_fails(tmp_path, monkeypatch):
    import httpx as httpx_module

    monkeypatch.setattr("agent.claude_code_source.list_claude_code_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.list_cursor_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.enrich_with_messages", lambda sessions: None)
    monkeypatch.setattr("agent.uploader.push_sync", lambda base_url, api_key, sessions, client: True)

    def raise_http_error(base_url, api_key, client):
        raise httpx_module.HTTPError("boom")

    monkeypatch.setattr("agent.jobs.fetch_pending_jobs", raise_http_error)

    config = main.Config(
        backend_url="http://backend.example",
        api_key="test-key",
        state_path=tmp_path / "sync_state.json",
        interval_seconds=60,
    )
    result = main.run_cycle(config, client=None)
    assert result is None


def test_main_falls_back_to_config_interval_when_run_cycle_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_REMOTE_BACKEND_URL", "http://backend.example")
    monkeypatch.setenv("AI_REMOTE_API_KEY", "test-key")
    monkeypatch.setenv("AI_REMOTE_STATE_PATH", str(tmp_path / "sync_state.json"))
    monkeypatch.setenv("AI_REMOTE_INTERVAL_SECONDS", "60")

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise SystemExit  # stop the infinite loop after the first iteration

    monkeypatch.setattr("agent.main.time.sleep", fake_sleep)
    monkeypatch.setattr("agent.main.run_cycle", lambda config, client: None)

    with pytest.raises(SystemExit):
        main.main()

    assert sleep_calls == [60]


def test_main_uses_returned_interval_when_run_cycle_succeeds(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_REMOTE_BACKEND_URL", "http://backend.example")
    monkeypatch.setenv("AI_REMOTE_API_KEY", "test-key")
    monkeypatch.setenv("AI_REMOTE_STATE_PATH", str(tmp_path / "sync_state.json"))
    monkeypatch.setenv("AI_REMOTE_INTERVAL_SECONDS", "60")

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise SystemExit

    monkeypatch.setattr("agent.main.time.sleep", fake_sleep)
    monkeypatch.setattr("agent.main.run_cycle", lambda config, client: 10)

    with pytest.raises(SystemExit):
        main.main()

    assert sleep_calls == [10]
