from agent import state


def test_load_synced_ids_missing_file_returns_empty_dict(tmp_path):
    assert state.load_synced_ids(tmp_path / "missing.json") == {}


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "sync_state.json"
    state.save_synced_ids({"claude-code:abc": "2026-08-01T10:00:00Z"}, path)
    assert state.load_synced_ids(path) == {"claude-code:abc": "2026-08-01T10:00:00Z"}


def test_compute_deltas_returns_new_and_changed_sessions_only():
    sessions = [
        {"id": "a", "last_updated_at": "t1"},
        {"id": "b", "last_updated_at": "t2"},
        {"id": "c", "last_updated_at": "t3"},
    ]
    synced = {"a": "t1", "b": "old-t2"}  # a unchanged, b changed, c new
    deltas = state.compute_deltas(sessions, synced)
    assert {s["id"] for s in deltas} == {"b", "c"}
