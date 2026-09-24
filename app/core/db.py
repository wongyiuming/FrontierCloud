import asyncio
import re
from datetime import datetime, timezone

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

MIGRATION_LOCK_NAME = "frontiercloud:schema-migrations"
MIGRATION_LOCK_TIMEOUT_SECONDS = 30


async def _commit_ddl(conn: AsyncConnection, statement: str) -> None:
    """Execute one MySQL atomic DDL statement and close SQLAlchemy's autobegin."""
    await conn.execute(text(statement))
    await conn.commit()


async def _column_exists(conn: AsyncConnection, table_name: str, column_name: str) -> bool:
    value = await conn.scalar(text("""
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema=DATABASE()
          AND table_name=:table_name
          AND column_name=:column_name
    """), {"table_name": table_name, "column_name": column_name})
    return bool(value)


async def _column_nullable(conn: AsyncConnection, table_name: str, column_name: str) -> bool:
    value = await conn.scalar(text("""
        SELECT IS_NULLABLE
        FROM information_schema.columns
        WHERE table_schema=DATABASE()
          AND table_name=:table_name
          AND column_name=:column_name
    """), {"table_name": table_name, "column_name": column_name})
    return str(value or "").upper() == "YES"


async def _index_exists(conn: AsyncConnection, table_name: str, index_name: str) -> bool:
    value = await conn.scalar(text("""
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema=DATABASE()
          AND table_name=:table_name
          AND index_name=:index_name
    """), {"table_name": table_name, "index_name": index_name})
    return bool(value)


async def _normalize_active_bans(conn: AsyncConnection) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await conn.execute(text("""
        UPDATE ip_auto_ban_events
        SET status='expired'
        WHERE status='active' AND expires_at <= :now
    """), {"now": now})
    await conn.execute(text("""
        UPDATE ip_auto_ban_events AS older
        JOIN ip_auto_ban_events AS newer
          ON newer.ip_address=older.ip_address
         AND newer.status='active'
         AND older.status='active'
         AND (
              newer.banned_at > older.banned_at
              OR (newer.banned_at=older.banned_at AND newer.id > older.id)
         )
        SET older.status='replaced',
            older.released_at=COALESCE(older.released_at, :now)
    """), {"now": now})
    await conn.commit()


async def _try_release_migration_lock(conn: AsyncConnection) -> bool:
    try:
        released = await conn.scalar(
            text("SELECT RELEASE_LOCK(:name)"),
            {"name": MIGRATION_LOCK_NAME},
        )
        await conn.commit()
        return released == 1
    except BaseException:
        try:
            await conn.rollback()
        except BaseException:
            pass
        return False


async def _try_invalidate_connection(conn: AsyncConnection) -> bool:
    try:
        await conn.invalidate()
        return True
    except BaseException:
        return False


async def _await_cleanup_task(task: asyncio.Task[bool]) -> tuple[bool, asyncio.CancelledError | None]:
    cancellation = None
    while True:
        try:
            completed = await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            cancellation = exc
            if task.done():
                completed = False if task.cancelled() else task.result()
                break
    return completed, cancellation


async def _invalidate_connection_safely(conn: AsyncConnection) -> None:
    task = asyncio.create_task(_try_invalidate_connection(conn))
    invalidated, cancellation = await _await_cleanup_task(task)
    if cancellation is not None:
        raise cancellation
    if not invalidated:
        raise RuntimeError("Could not discard the uncertain migration connection")


async def _finish_migration_lock(conn: AsyncConnection) -> None:
    release_task = asyncio.create_task(_try_release_migration_lock(conn))
    released, cancellation = await _await_cleanup_task(release_task)
    if not released:
        invalidate_task = asyncio.create_task(_try_invalidate_connection(conn))
        _invalidated, invalidation_cancellation = await _await_cleanup_task(invalidate_task)
        if cancellation is None:
            cancellation = invalidation_cancellation
    if cancellation is not None:
        raise cancellation
    if not released:
        raise RuntimeError("Could not confirm release of the schema migration lock")


async def _acquire_migration_lock(conn: AsyncConnection) -> None:
    acquisition_state = "unknown"
    try:
        acquired = await conn.scalar(
            text("SELECT GET_LOCK(:name, :timeout)"),
            {"name": MIGRATION_LOCK_NAME, "timeout": MIGRATION_LOCK_TIMEOUT_SECONDS},
        )
        acquisition_state = "held" if acquired == 1 else "not-held"
        if acquisition_state != "held":
            await conn.rollback()
            raise RuntimeError("Could not acquire the schema migration lock")
        await conn.commit()
    except BaseException:
        if acquisition_state == "held":
            await _finish_migration_lock(conn)
        elif acquisition_state == "unknown":
            await _invalidate_connection_safely(conn)
        raise


async def init_db() -> None:
    """Create and migrate the MySQL schema under a cross-instance DDL lock.

    MySQL DDL implicitly commits, so this deliberately uses independently atomic
    DDL statements instead of presenting the whole migration as one transaction.
    """
    async with engine.connect() as conn:
        await _acquire_migration_lock(conn)
        try:
            await _commit_ddl(conn, """
                CREATE TABLE IF NOT EXISTS media_visibility (
                    relative_path VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
                    hidden TINYINT(1) NOT NULL DEFAULT 1,
                    updated_at DATETIME(6) NOT NULL,
                    INDEX idx_media_visibility_hidden (hidden)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            visibility_collation = await conn.scalar(text("""
                SELECT COLLATION_NAME
                FROM information_schema.columns
                WHERE table_schema=DATABASE()
                  AND table_name='media_visibility'
                  AND column_name='relative_path'
            """))
            await conn.commit()
            if str(visibility_collation or "").lower() != "utf8mb4_bin":
                await _commit_ddl(conn, """
                    ALTER TABLE media_visibility
                    MODIFY relative_path VARCHAR(255)
                    CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL
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
                    INDEX idx_ip_security_timeline (ip_address, created_at, id)
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
            summary_rows = await conn.scalar(text("SELECT COUNT(*) FROM ip_security_summary"))
            await conn.commit()
            if not summary_rows:
                await conn.execute(text("""
                    INSERT INTO ip_security_summary (ip_address, attack_count, last_attack_at)
                    SELECT ip_address, COUNT(*), MAX(created_at)
                    FROM ip_security_audit_log
                    WHERE action='invalid_api'
                    GROUP BY ip_address
                    ON DUPLICATE KEY UPDATE
                        attack_count=VALUES(attack_count),
                        last_attack_at=VALUES(last_attack_at)
                """))
                await conn.commit()
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
            await conn.execute(text("INSERT IGNORE INTO ip_security_projection(singleton) VALUES (1)"))
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
            for column_name, definition in (
                ("ban_kind", "VARCHAR(16) NOT NULL DEFAULT 'auto'"),
                ("reason", "VARCHAR(255) NULL"),
                ("created_by_session_hash", "CHAR(64) NULL"),
            ):
                exists = await _column_exists(conn, "ip_auto_ban_events", column_name)
                await conn.commit()
                if not exists:
                    await _commit_ddl(
                        conn,
                        f"ALTER TABLE ip_auto_ban_events ADD COLUMN {column_name} {definition}",
                    )

            active_column_exists = await _column_exists(
                conn, "ip_auto_ban_events", "active_ip_address"
            )
            await conn.commit()
            active_index_exists = await _index_exists(
                conn, "ip_auto_ban_events", "uq_ip_ban_active_ip"
            )
            await conn.commit()
            if not active_column_exists or not active_index_exists:
                await _normalize_active_bans(conn)
                clauses = []
                if not active_column_exists:
                    clauses.append("""
                        ADD COLUMN active_ip_address VARCHAR(45)
                        GENERATED ALWAYS AS (
                            CASE WHEN status='active' THEN ip_address ELSE NULL END
                        ) STORED
                    """)
                if not active_index_exists:
                    clauses.append("ADD UNIQUE INDEX uq_ip_ban_active_ip (active_ip_address)")
                await _commit_ddl(
                    conn,
                    "ALTER TABLE ip_auto_ban_events " + ", ".join(clauses),
                )

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
                    INDEX idx_media_objects_kind (object_kind)
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
                    CONSTRAINT chk_media_preference CHECK (preference BETWEEN -7 AND 500)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            preference_check = await conn.scalar(text("""
                SELECT CHECK_CLAUSE
                FROM information_schema.CHECK_CONSTRAINTS
                WHERE CONSTRAINT_SCHEMA=DATABASE()
                  AND CONSTRAINT_NAME='chk_media_preference'
            """))
            preference_type = await conn.scalar(text("""
                SELECT DATA_TYPE
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA=DATABASE()
                  AND TABLE_NAME='media_playback_stats'
                  AND COLUMN_NAME='preference'
            """))
            await conn.commit()
            normalized_check = re.sub(r"[`\s()]", "", str(preference_check or "")).lower()
            normalized_check = normalized_check.replace("media_playback_stats.", "")
            expected_check = "preferencebetween-7and500"
            clauses = []
            if normalized_check != expected_check and preference_check:
                clauses.append("DROP CHECK chk_media_preference")
            if str(preference_type or "").lower() != "smallint":
                clauses.append("MODIFY COLUMN preference SMALLINT NOT NULL DEFAULT 0")
            if normalized_check != expected_check:
                clauses.append(
                    "ADD CONSTRAINT chk_media_preference CHECK (preference BETWEEN -7 AND 500)"
                )
            if clauses:
                await _commit_ddl(
                    conn,
                    "ALTER TABLE media_playback_stats " + ", ".join(clauses),
                )
            await _commit_ddl(conn, """
                CREATE TABLE IF NOT EXISTS media_playback_events (
                    playback_session_id CHAR(36) NOT NULL,
                    media_id CHAR(64) NOT NULL,
                    counted_at DATETIME(6) NOT NULL,
                    expires_at DATETIME(6) NOT NULL,
                    PRIMARY KEY (playback_session_id, media_id),
                    INDEX idx_playback_event_expiry (expires_at)
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
                    INDEX idx_media_lyric_updated_at (updated_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            if not await _column_exists(conn, "ip_security_locks", "projection_dirty"):
                await _commit_ddl(conn, "ALTER TABLE ip_security_locks ADD COLUMN projection_dirty TINYINT NOT NULL DEFAULT 0")
            for table_name in ("admin_audit_log", "ip_security_audit_log"):
                for column_name, definition in (("request_id", "VARCHAR(128) NULL"), ("trace_id", "CHAR(32) NULL")):
                    exists = await _column_exists(conn, table_name, column_name)
                    await conn.commit()
                    if not exists:
                        await _commit_ddl(conn, f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
            for table_name, index_name, columns in (
                ("media_playback_events", "idx_playback_event_media", "media_id"),
                ("media_objects", "idx_media_objects_path", "media_path(191)"),
                ("media_playback_stats", "idx_playback_path", "media_path(191)"),
                ("media_lyric_links", "idx_media_lyric_path", "media_path(191)"),
                ("media_lyric_links", "idx_lyric_path", "lyric_path(191)"),
                ("ip_security_locks", "idx_ip_projection_dirty", "projection_dirty, ip_address"),
                ("ip_security_audit_log", "idx_ip_security_violation_count", "ip_address, action, created_at, id"),
            ):
                exists = await _index_exists(conn, table_name, index_name)
                await conn.commit()
                if not exists:
                    await _commit_ddl(conn, f"CREATE INDEX {index_name} ON {table_name} ({columns})")
            from app.services.federation.schema import migration_statements
            for statement in migration_statements():
                await _commit_ddl(conn, statement)
            from app.services.karaoke_schema import migration_statements as karaoke_migrations
            for statement in karaoke_migrations():
                await _commit_ddl(conn, statement)
            await conn.commit()
        except BaseException:
            try:
                await conn.rollback()
            except BaseException:
                pass
            raise
        finally:
            await _finish_migration_lock(conn)


async def close_db() -> None:
    await engine.dispose()
