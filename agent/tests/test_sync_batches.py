from agent import main, state


def _setup(tmp_path, monkeypatch, count, fail_on_batch=None):
    sessions = [{"id": f"claude-code:s{i:03d}", "last_updated_at": "t1"} for i in range(count)]
    monkeypatch.setattr("agent.claude_code_source.list_claude_code_sessions", lambda: sessions)
    monkeypatch.setattr("agent.cursor_source.list_cursor_sessions", lambda: [])
    monkeypatch.setattr("agent.cursor_source.enrich_with_messages", lambda s: None)
    pushes = []

    def fake_push_sync(base_url, api_key, batch, client, **kw):
        pushes.append([s["id"] for s in batch])
        return fail_on_batch is None or len(pushes) != fail_on_batch

    jobs_polled = []
    monkeypatch.setattr("agent.uploader.push_sync", fake_push_sync)
    monkeypatch.setattr(
        "agent.jobs.fetch_pending_jobs",
        lambda *a: jobs_polled.append(True) or {"jobs": [], "poll_interval_seconds": 60},
    )
    config = main.Config(backend_url="http://b", api_key="k", state_path=tmp_path / "sync_state.json")
    return config, pushes, jobs_polled


def test_full_resync_is_split_into_batches_and_capped_per_cycle(tmp_path, monkeypatch):
    config, pushes, jobs_polled = _setup(tmp_path, monkeypatch, count=130)
    main.run_cycle(config, client=None)
    assert [len(p) for p in pushes] == [20] * 5
    assert len(state.load_synced_ids(config.state_path)) == 100
    assert jobs_polled  # jobs are still served during a long resync

    pushes.clear()
    main.run_cycle(config, client=None)
    assert [len(p) for p in pushes] == [20, 10]
    assert len(state.load_synced_ids(config.state_path)) == 130


def test_failed_batch_keeps_earlier_batches_and_stops_pushing(tmp_path, monkeypatch):
    config, pushes, jobs_polled = _setup(tmp_path, monkeypatch, count=60, fail_on_batch=2)
    main.run_cycle(config, client=None)
    assert len(pushes) == 2
    synced = state.load_synced_ids(config.state_path)
    assert set(synced) == set(pushes[0])
    assert jobs_polled
