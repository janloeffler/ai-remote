CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    tool TEXT NOT NULL CHECK(tool IN ('claude-code', 'cursor')),
    entrypoint TEXT NOT NULL DEFAULT '',
    project_path TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_updated_at TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    last_message_preview TEXT NOT NULL DEFAULT '',
    full_content_synced INTEGER NOT NULL DEFAULT 0,
    loaded_message_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'idle'
);

CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL REFERENCES sessions(id),
    idx INTEGER NOT NULL,
    role TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (session_id, idx)
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    session_id UNINDEXED,
    kind UNINDEXED,
    text
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN ('fetch_full', 'resume_message', 'new_session', 'fetch_image', 'search')),
    target TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'running', 'done', 'failed')),
    result_text TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_contact_at TEXT,
    active_until TEXT
);
INSERT OR IGNORE INTO agent_status (id, last_contact_at) VALUES (1, NULL);

CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    remote_commands_paused INTEGER NOT NULL DEFAULT 0,
    poll_interval_default_seconds INTEGER,
    poll_interval_active_seconds INTEGER
);
INSERT OR IGNORE INTO settings (id, remote_commands_paused) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS images (
    session_id TEXT NOT NULL,
    path_key TEXT NOT NULL,
    source_path TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, path_key)
);

CREATE TABLE IF NOT EXISTS e2e_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT,
    salt TEXT,
    kdf TEXT,
    key_check TEXT,
    data_epoch TEXT NOT NULL
);
