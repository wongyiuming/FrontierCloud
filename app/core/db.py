import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.core.config import settings


engine: AsyncEngine = create_async_engine(
    settings.MYSQL_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=5,
    max_overflow=10,
)

SCHEMA_GENERATION = 1
SCHEMA_MARKER_TABLE = "frontiercloud_schema"
LOCAL_TABLES = {
    "media_visibility",
    "media_delete_operations",
    "admin_audit_log",
    "webrtc_observation_events",
    "webrtc_observation_summary",
    "ip_security_audit_log",
    "ip_security_summary",
    "ip_security_locks",
    "ip_security_projection",
    "ip_auto_ban_events",
    "ip_permanent_whitelist",
    "media_objects",
    "media_playback_stats",
    "media_playback_events",
    "media_lyric_links",
}


async def _commit_ddl(conn: AsyncConnection, statement: str) -> None:
    """Execute one schema-initialization DDL statement."""
    await conn.execute(text(statement))
    await conn.commit()


async def _table_names(conn: AsyncConnection) -> set[str]:
    rows = await conn.execute(text("""
        SELECT TABLE_NAME
        FROM information_schema.tables
        WHERE table_schema=DATABASE()
          AND TABLE_TYPE='BASE TABLE'
    """))
    return {str(row[0]) for row in rows}


def _required_tables() -> set[str]:
    from app.services.federation import schema as federation_schema
    from app.services import karaoke_schema

    return (
        LOCAL_TABLES
        | {table.name for table in federation_schema.metadata.sorted_tables}
        | {table.name for table in karaoke_schema.metadata.sorted_tables}
        | {SCHEMA_MARKER_TABLE}
    )


async def _validate_initialized_schema(conn: AsyncConnection, tables: set[str]) -> None:
    if SCHEMA_MARKER_TABLE not in tables:
        raise RuntimeError(
            "检测到已有数据库，但它不是由当前版本的新节点初始化流程创建；"
            "FrontierCloud 不支持数据库迁移，请按新节点流程重新初始化数据库"
        )
    generation = await conn.scalar(text(
        "SELECT generation FROM frontiercloud_schema WHERE singleton=1"
    ))
    if generation != SCHEMA_GENERATION:
        raise RuntimeError(
            f"数据库初始化代际不兼容：expected={SCHEMA_GENERATION}, actual={generation!r}；"
            "FrontierCloud 不支持数据库迁移，请按新节点流程重新初始化数据库"
        )
    missing = sorted(_required_tables() - tables)
    if missing:
        raise RuntimeError(
            "当前节点数据库不完整，禁止自动补表或迁移；缺少表：" + ", ".join(missing)
        )


async def init_db() -> None:
    """Initialize one empty database, or validate an already initialized node.

    Existing databases are never altered, backfilled, normalized, or upgraded.
    Schema changes require a new-node initialization with an empty database.
    """
    async with engine.connect() as conn:
        existing_tables = await _table_names(conn)
        if existing_tables:
            await _validate_initialized_schema(conn, existing_tables)
            return

        await _commit_ddl(conn, """
            CREATE TABLE IF NOT EXISTS media_visibility (
                relative_path VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
                hidden TINYINT(1) NOT NULL DEFAULT 1,
                updated_at DATETIME(6) NOT NULL,
                INDEX idx_media_visibility_hidden (hidden)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        await _commit_ddl(conn, """
            CREATE TABLE IF NOT EXISTS media_delete_operations (
                operation_id CHAR(32) NOT NULL PRIMARY KEY,
                state VARCHAR(16) NOT NULL,
                manifest JSON NOT NULL,
                created_at DATETIME(6) NOT NULL,
                INDEX idx_media_delete_state_created (state, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        await _commit_ddl(conn, """
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                session_id_hash CHAR(64) NULL,
                action VARCHAR(64) NOT NULL,
                target_count INT NOT NULL DEFAULT 0,
                source_summary TEXT NULL,
                result VARCHAR(32) NOT NULL,
                detail TEXT NULL,
                client_ip VARCHAR(45) NULL,
                user_agent VARCHAR(512) NULL,
                request_id VARCHAR(128) NULL,
                trace_id CHAR(32) NULL,
                created_at DATETIME(6) NOT NULL,
                INDEX idx_audit_created_at (created_at),
                INDEX idx_audit_action (action)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        await _commit_ddl(conn, """
            CREATE TABLE IF NOT EXISTS webrtc_observation_events (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                client_ip VARCHAR(45) NOT NULL,
                webrtc_ip VARCHAR(45) NULL,
                outcome VARCHAR(32) NOT NULL,
                matches_verified TINYINT(1) NOT NULL,
                observed_at DATETIME(6) NOT NULL,
                INDEX idx_webrtc_client_time (client_ip, observed_at, id),
                INDEX idx_webrtc_observed_time (webrtc_ip, observed_at, id),
                INDEX idx_webrtc_recent (observed_at, id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        await _commit_ddl(conn, """
            CREATE TABLE IF NOT EXISTS webrtc_observation_summary (
                client_ip VARCHAR(45) NOT NULL,
                webrtc_ip_key VARCHAR(45) NOT NULL,
                webrtc_ip VARCHAR(45) NULL,
                observation_count BIGINT UNSIGNED NOT NULL,
                matching_count BIGINT UNSIGNED NOT NULL,
                first_seen DATETIME(6) NOT NULL,
                last_seen DATETIME(6) NOT NULL,
                last_outcome VARCHAR(32) NOT NULL,
                PRIMARY KEY (client_ip, webrtc_ip_key),
                INDEX idx_webrtc_summary_observed (webrtc_ip, last_seen),
                INDEX idx_webrtc_summary_recent (last_seen)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        await _commit_ddl(conn, """
            CREATE TABLE IF NOT EXISTS ip_security_audit_log (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                ip_address VARCHAR(45) NOT NULL,
                action VARCHAR(32) NOT NULL,
                detail JSON NOT NULL,
                session_id_hash CHAR(64) NULL,
                request_id VARCHAR(128) NULL,
                trace_id CHAR(32) NULL,
                created_at DATETIME(6) NOT NULL,
                INDEX idx_ip_security_timeline (ip_address, created_at, id),
                INDEX idx_ip_security_violation_count (ip_address, action, created_at, id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        await _commit_ddl(conn, """
            CREATE TABLE IF NOT EXISTS ip_security_summary (
                ip_address VARCHAR(45) NOT NULL PRIMARY KEY,
                attack_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
                last_attack_at DATETIME(6) NULL,
                INDEX idx_ip_security_last_attack (last_attack_at, ip_address)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        await _commit_ddl(conn, """
            CREATE TABLE IF NOT EXISTS ip_security_locks (
                ip_address VARCHAR(45) NOT NULL PRIMARY KEY,
                projection_dirty TINYINT NOT NULL DEFAULT 0,
                INDEX idx_ip_projection_dirty (projection_dirty, ip_address)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        await _commit_ddl(conn, """
            CREATE TABLE IF NOT EXISTS ip_security_projection (
                singleton TINYINT NOT NULL PRIMARY KEY,
                generation BIGINT UNSIGNED NOT NULL DEFAULT 0,
                published_generation BIGINT UNSIGNED NOT NULL DEFAULT 0
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        await conn.execute(text("INSERT INTO ip_security_projection(singleton) VALUES (1)"))
        await conn.commit()
        await _commit_ddl(conn, """
            CREATE TABLE IF NOT EXISTS ip_auto_ban_events (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                ip_address VARCHAR(45) NOT NULL,
                trigger_count INT UNSIGNED NOT NULL,
                window_started_at DATETIME(6) NOT NULL,
                banned_at DATETIME(6) NOT NULL,
                expires_at DATETIME(6) NOT NULL,
                last_method VARCHAR(16) NULL,
                last_path VARCHAR(2048) NULL,
                user_agent VARCHAR(512) NULL,
                ban_kind VARCHAR(16) NOT NULL DEFAULT 'auto',
                reason VARCHAR(255) NULL,
                created_by_session_hash CHAR(64) NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'active',
                released_at DATETIME(6) NULL,
                released_by_session_hash CHAR(64) NULL,
                active_ip_address VARCHAR(45)
                    GENERATED ALWAYS AS (
                        CASE WHEN status='active' THEN ip_address ELSE NULL END
                    ) STORED,
                UNIQUE INDEX uq_ip_ban_active_ip (active_ip_address),
                INDEX idx_ip_ban_ip_time (ip_address, banned_at),
                INDEX idx_ip_ban_recent (banned_at, status),
                INDEX idx_ip_ban_expiry (expires_at, status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        await _commit_ddl(conn, """
            CREATE TABLE IF NOT EXISTS ip_permanent_whitelist (
                ip_address VARCHAR(45) NOT NULL PRIMARY KEY,
                created_at DATETIME(6) NOT NULL,
                created_by_session_hash CHAR(64) NULL,
                note VARCHAR(255) NULL,
                INDEX idx_ip_whitelist_created_at (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        await _commit_ddl(conn, """
            CREATE TABLE IF NOT EXISTS media_objects (
                media_id CHAR(64) NOT NULL PRIMARY KEY,
                object_kind VARCHAR(16) NOT NULL,
                media_path VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
                path_locator CHAR(64) NOT NULL,
                created_at DATETIME(6) NOT NULL,
                updated_at DATETIME(6) NOT NULL,
                UNIQUE INDEX uq_media_objects_path_locator (path_locator),
                INDEX idx_media_objects_kind (object_kind),
                INDEX idx_media_objects_path (media_path(191))
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        await _commit_ddl(conn, """
            CREATE TABLE IF NOT EXISTS media_playback_stats (
                media_id CHAR(64) NOT NULL PRIMARY KEY,
                media_path VARCHAR(1024) NOT NULL,
                play_score BIGINT UNSIGNED NOT NULL DEFAULT 0,
                preference SMALLINT NOT NULL DEFAULT 0,
                created_at DATETIME(6) NOT NULL,
                updated_at DATETIME(6) NOT NULL,
                INDEX idx_playback_sort (preference, play_score),
                INDEX idx_playback_path (media_path(191)),
                CONSTRAINT chk_media_preference CHECK (preference BETWEEN -7 AND 500)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        await _commit_ddl(conn, """
            CREATE TABLE IF NOT EXISTS media_playback_events (
                playback_session_id CHAR(36) NOT NULL,
                media_id CHAR(64) NOT NULL,
                counted_at DATETIME(6) NOT NULL,
                expires_at DATETIME(6) NOT NULL,
                PRIMARY KEY (playback_session_id, media_id),
                INDEX idx_playback_event_expiry (expires_at),
                INDEX idx_playback_event_media (media_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        await _commit_ddl(conn, """
            CREATE TABLE IF NOT EXISTS media_lyric_links (
                media_id CHAR(64) NOT NULL PRIMARY KEY,
                media_path VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
                lyric_id CHAR(64) NOT NULL,
                lyric_path VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
                created_at DATETIME(6) NOT NULL,
                updated_at DATETIME(6) NOT NULL,
                INDEX idx_media_lyric_lyric_id (lyric_id),
                INDEX idx_media_lyric_updated_at (updated_at),
                INDEX idx_media_lyric_path (media_path(191)),
                INDEX idx_lyric_path (lyric_path(191))
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        from app.services.federation.schema import migration_statements
        for statement in migration_statements():
            await _commit_ddl(conn, statement)
        from app.services.karaoke_schema import migration_statements as karaoke_schema_statements
        for statement in karaoke_schema_statements():
            await _commit_ddl(conn, statement)

        await _commit_ddl(conn, """
            CREATE TABLE frontiercloud_schema (
                singleton TINYINT NOT NULL PRIMARY KEY,
                generation INT UNSIGNED NOT NULL,
                created_at BIGINT UNSIGNED NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        await conn.execute(text("""
            INSERT INTO frontiercloud_schema(singleton, generation, created_at)
            VALUES (1, :generation, :created_at)
        """), {"generation": SCHEMA_GENERATION, "created_at": int(time.time())})
        await conn.commit()
        await _validate_initialized_schema(conn, await _table_names(conn))


async def close_db() -> None:
    await engine.dispose()
