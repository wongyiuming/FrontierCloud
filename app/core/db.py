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
                    created_at DATETIME(6) NOT NULL,
                    INDEX idx_audit_created_at (created_at),
                    INDEX idx_audit_action (action)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            await _commit_ddl(conn, """
                CREATE TABLE IF NOT EXISTS ip_security_locks (
                    ip_address VARCHAR(45) NOT NULL PRIMARY KEY
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
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
                CREATE TABLE IF NOT EXISTS media_playback_stats (
                    media_id CHAR(64) NOT NULL PRIMARY KEY,
                    media_path VARCHAR(1024) NOT NULL,
                    play_score BIGINT UNSIGNED NOT NULL DEFAULT 0,
                    preference TINYINT NOT NULL DEFAULT 0,
                    created_at DATETIME(6) NOT NULL,
                    updated_at DATETIME(6) NOT NULL,
                    INDEX idx_playback_sort (preference, play_score),
                    CONSTRAINT chk_media_preference CHECK (preference BETWEEN -2 AND 7)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            preference_check = await conn.scalar(text("""
                SELECT CHECK_CLAUSE
                FROM information_schema.CHECK_CONSTRAINTS
                WHERE CONSTRAINT_SCHEMA=DATABASE()
                  AND CONSTRAINT_NAME='chk_media_preference'
            """))
            await conn.commit()
            normalized_check = re.sub(r"[`\s()]", "", str(preference_check or "")).lower()
            normalized_check = normalized_check.replace("media_playback_stats.", "")
            expected_check = "preferencebetween-2and7"
            if normalized_check != expected_check:
                if preference_check:
                    await _commit_ddl(conn, """
                        ALTER TABLE media_playback_stats
                        DROP CHECK chk_media_preference,
                        ADD CONSTRAINT chk_media_preference
                        CHECK (preference BETWEEN -2 AND 7)
                    """)
                else:
                    await _commit_ddl(conn, """
                        ALTER TABLE media_playback_stats
                        ADD CONSTRAINT chk_media_preference
                        CHECK (preference BETWEEN -2 AND 7)
                    """)
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
