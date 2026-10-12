"""Fixed SQLite bootstrap DDL for FrontierCloud-owned local tables.

These statements are source-controlled schema, not a runtime MySQL DDL translator.
Path text uses SQLite BINARY comparison; timestamps retain Python datetime values.
The schema generation and journals are created by the common bootstrap coordinator.
"""

LOCAL_DDL = (
    """CREATE TABLE IF NOT EXISTS media_visibility (
    relative_path TEXT  NOT NULL PRIMARY KEY,
    hidden INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMP NOT NULL
)""",
    """CREATE INDEX IF NOT EXISTS idx_media_visibility_hidden ON media_visibility (hidden)""",
    """CREATE TABLE IF NOT EXISTS media_delete_operations (
    operation_id TEXT NOT NULL PRIMARY KEY,
    state TEXT NOT NULL,
    manifest TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL
)""",
    """CREATE INDEX IF NOT EXISTS idx_media_delete_state_created ON media_delete_operations (state, created_at)""",
    """CREATE TABLE IF NOT EXISTS admin_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id_hash TEXT NULL,
    action TEXT NOT NULL,
    target_count INTEGER NOT NULL DEFAULT 0,
    source_summary TEXT NULL,
    result TEXT NOT NULL,
    detail TEXT NULL,
    client_ip TEXT NULL,
    user_agent TEXT NULL,
    request_id TEXT NULL,
    trace_id TEXT NULL,
    created_at TIMESTAMP NOT NULL
)""",
    """CREATE INDEX IF NOT EXISTS idx_audit_created_at ON admin_audit_log (created_at)""",
    """CREATE INDEX IF NOT EXISTS idx_audit_action ON admin_audit_log (action)""",
    """CREATE TABLE IF NOT EXISTS webrtc_observation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_ip TEXT NOT NULL,
    webrtc_ip TEXT NULL,
    outcome TEXT NOT NULL,
    matches_verified INTEGER NOT NULL,
    observed_at TIMESTAMP NOT NULL
)""",
    """CREATE INDEX IF NOT EXISTS idx_webrtc_client_time ON webrtc_observation_events (client_ip, observed_at, id)""",
    """CREATE INDEX IF NOT EXISTS idx_webrtc_observed_time ON webrtc_observation_events (webrtc_ip, observed_at, id)""",
    """CREATE INDEX IF NOT EXISTS idx_webrtc_recent ON webrtc_observation_events (observed_at, id)""",
    """CREATE TABLE IF NOT EXISTS webrtc_observation_summary (
    client_ip TEXT NOT NULL,
    webrtc_ip_key TEXT NOT NULL,
    webrtc_ip TEXT NULL,
    observation_count INTEGER NOT NULL,
    matching_count INTEGER NOT NULL,
    first_seen TIMESTAMP NOT NULL,
    last_seen TIMESTAMP NOT NULL,
    last_outcome TEXT NOT NULL,
    PRIMARY KEY (client_ip, webrtc_ip_key)
)""",
    """CREATE INDEX IF NOT EXISTS idx_webrtc_summary_observed ON webrtc_observation_summary (webrtc_ip, last_seen)""",
    """CREATE INDEX IF NOT EXISTS idx_webrtc_summary_recent ON webrtc_observation_summary (last_seen)""",
    """CREATE TABLE IF NOT EXISTS ip_security_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_address TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    session_id_hash TEXT NULL,
    request_id TEXT NULL,
    trace_id TEXT NULL,
    created_at TIMESTAMP NOT NULL
)""",
    """CREATE INDEX IF NOT EXISTS idx_ip_security_timeline ON ip_security_audit_log (ip_address, created_at, id)""",
    """CREATE INDEX IF NOT EXISTS idx_ip_security_violation_count ON ip_security_audit_log (ip_address, action, created_at, id)""",
    """CREATE TABLE IF NOT EXISTS ip_security_summary (
    ip_address TEXT NOT NULL PRIMARY KEY,
    attack_count INTEGER NOT NULL DEFAULT 0,
    last_attack_at TIMESTAMP NULL
)""",
    """CREATE INDEX IF NOT EXISTS idx_ip_security_last_attack ON ip_security_summary (last_attack_at, ip_address)""",
    """CREATE TABLE IF NOT EXISTS ip_security_locks (
    ip_address TEXT NOT NULL PRIMARY KEY,
    projection_dirty INTEGER NOT NULL DEFAULT 0
)""",
    """CREATE INDEX IF NOT EXISTS idx_ip_projection_dirty ON ip_security_locks (projection_dirty, ip_address)""",
    """CREATE TABLE IF NOT EXISTS ip_security_projection (
    singleton INTEGER NOT NULL PRIMARY KEY,
    generation INTEGER NOT NULL DEFAULT 0,
    published_generation INTEGER NOT NULL DEFAULT 0
)""",
    """CREATE TABLE IF NOT EXISTS ip_auto_ban_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_address TEXT NOT NULL,
    trigger_count INTEGER NOT NULL,
    window_started_at TIMESTAMP NOT NULL,
    banned_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    last_method TEXT NULL,
    last_path TEXT NULL,
    user_agent TEXT NULL,
    ban_kind TEXT NOT NULL DEFAULT 'auto',
    reason TEXT NULL,
    created_by_session_hash TEXT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    released_at TIMESTAMP NULL,
    released_by_session_hash TEXT NULL,
    active_ip_address TEXT
                    GENERATED ALWAYS AS (
                        CASE WHEN status='active' THEN ip_address ELSE NULL END
                    ) STORED
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_ip_ban_active_ip ON ip_auto_ban_events (active_ip_address)""",
    """CREATE INDEX IF NOT EXISTS idx_ip_ban_ip_time ON ip_auto_ban_events (ip_address, banned_at)""",
    """CREATE INDEX IF NOT EXISTS idx_ip_ban_recent ON ip_auto_ban_events (banned_at, status)""",
    """CREATE INDEX IF NOT EXISTS idx_ip_ban_expiry ON ip_auto_ban_events (expires_at, status)""",
    """CREATE TABLE IF NOT EXISTS ip_permanent_whitelist (
    ip_address TEXT NOT NULL PRIMARY KEY,
    created_at TIMESTAMP NOT NULL,
    created_by_session_hash TEXT NULL,
    note TEXT NULL
)""",
    """CREATE INDEX IF NOT EXISTS idx_ip_whitelist_created_at ON ip_permanent_whitelist (created_at)""",
    """CREATE TABLE IF NOT EXISTS media_objects (
    media_id TEXT NOT NULL PRIMARY KEY,
    object_kind TEXT NOT NULL,
    media_path TEXT  NOT NULL,
    path_locator TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_media_objects_path_locator ON media_objects (path_locator)""",
    """CREATE INDEX IF NOT EXISTS idx_media_objects_kind ON media_objects (object_kind)""",
    """CREATE INDEX IF NOT EXISTS idx_media_objects_path ON media_objects (media_path)""",
    """CREATE TABLE IF NOT EXISTS media_playback_stats (
    media_id TEXT NOT NULL PRIMARY KEY,
    media_path TEXT NOT NULL,
    play_score INTEGER NOT NULL DEFAULT 0,
    preference INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT chk_media_preference CHECK (preference BETWEEN -7 AND 500000)
)""",
    """CREATE INDEX IF NOT EXISTS idx_playback_sort ON media_playback_stats (preference, play_score)""",
    """CREATE INDEX IF NOT EXISTS idx_playback_path ON media_playback_stats (media_path)""",
    """CREATE TABLE IF NOT EXISTS media_playback_events (
    playback_session_id TEXT NOT NULL,
    media_id TEXT NOT NULL,
    counted_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    PRIMARY KEY (playback_session_id, media_id)
)""",
    """CREATE INDEX IF NOT EXISTS idx_playback_event_expiry ON media_playback_events (expires_at)""",
    """CREATE INDEX IF NOT EXISTS idx_playback_event_media ON media_playback_events (media_id)""",
    """CREATE TABLE IF NOT EXISTS media_lyric_links (
    media_id TEXT NOT NULL PRIMARY KEY,
    media_path TEXT  NOT NULL,
    lyric_id TEXT NOT NULL,
    lyric_path TEXT  NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
)""",
    """CREATE INDEX IF NOT EXISTS idx_media_lyric_lyric_id ON media_lyric_links (lyric_id)""",
    """CREATE INDEX IF NOT EXISTS idx_media_lyric_updated_at ON media_lyric_links (updated_at)""",
    """CREATE INDEX IF NOT EXISTS idx_media_lyric_path ON media_lyric_links (media_path)""",
    """CREATE INDEX IF NOT EXISTS idx_lyric_path ON media_lyric_links (lyric_path)""",
    """CREATE TABLE IF NOT EXISTS frontiercloud_schema (
    singleton INTEGER NOT NULL PRIMARY KEY,
    generation INTEGER NOT NULL,
    created_at INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS frontiercloud_schema_migrations (generation INTEGER NOT NULL PRIMARY KEY, migration_name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at INTEGER NOT NULL)""",
)
