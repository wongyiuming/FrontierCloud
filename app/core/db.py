import time
import os
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.core.config import SECRET_DIR, settings
from app.core.schema_migrations import (
    CURRENT_SCHEMA_GENERATION,
    MIGRATION_HISTORY_DDL,
    MIGRATION_HISTORY_TABLE,
    migrate_schema,
    validate_migration_history,
    column_exists,
)


def create_database_engine(configuration=settings) -> AsyncEngine:
    if configuration.DB_TYPE == "sqlite":
        from app.core.sqlite_compat import create_sqlite_engine
        return create_sqlite_engine(configuration.DATABASE_URL)
    return create_async_engine(
        configuration.MYSQL_URL, pool_pre_ping=True, pool_recycle=1800,
        pool_size=5, max_overflow=10,
    )


engine: AsyncEngine = create_database_engine()

SCHEMA_GENERATION = CURRENT_SCHEMA_GENERATION
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
    MIGRATION_HISTORY_TABLE,
}


async def _commit_ddl(conn: AsyncConnection, statement: str) -> None:
    """Execute one schema-initialization DDL statement."""
    await conn.execute(text(statement))
    await conn.commit()


async def _table_names(conn: AsyncConnection) -> set[str]:
    if getattr(getattr(conn, "dialect", None), "name", "mysql") == "sqlite":
        rows = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
        return {str(row[0]) for row in rows}
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
    from app.services import media_crypto_schema

    return (
        LOCAL_TABLES
        | {table.name for table in federation_schema.metadata.sorted_tables}
        | {table.name for table in karaoke_schema.metadata.sorted_tables}
        | {table.name for table in media_crypto_schema.metadata.sorted_tables}
        | {SCHEMA_MARKER_TABLE}
    )


async def _read_schema_generation(conn: AsyncConnection) -> int:
    generation = await conn.scalar(text(
        "SELECT generation FROM frontiercloud_schema WHERE singleton=1"
    ))
    if generation is None:
        raise RuntimeError("数据库 schema marker 缺少 singleton=1 记录")
    try:
        return int(generation)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"数据库 schema generation 非法：{generation!r}") from exc


async def _validate_initialized_schema(conn: AsyncConnection, tables: set[str]) -> None:
    if SCHEMA_MARKER_TABLE not in tables:
        raise RuntimeError(
            "检测到已有数据库但缺少 FrontierCloud schema marker；"
            "无法安全推断历史 schema 版本，不支持对无代际标记数据库自动迁移"
        )
    generation = await _read_schema_generation(conn)
    if generation != SCHEMA_GENERATION:
        raise RuntimeError(
            f"数据库 schema 迁移未完成或版本不兼容："
            f"expected={SCHEMA_GENERATION}, actual={generation}"
        )
    missing = sorted(_required_tables() - tables)
    if missing:
        raise RuntimeError(
            "当前代际数据库结构不完整，禁止带病启动；缺少表：" + ", ".join(missing)
        )
    await validate_migration_history(conn)
    if not await column_exists(conn, "cluster_upload_sessions", "content_encrypted"):
        raise RuntimeError("当前代际数据库结构不完整；缺少 cluster_upload_sessions.content_encrypted")


async def init_db() -> None:
    """Bootstrap an empty database or migrate an initialized database in place.

    Databases created by FrontierCloud carry a schema-generation marker. Older marked
    generations are upgraded sequentially through the migration registry under a MySQL
    advisory lock. An unmarked non-empty database is deliberately rejected because its
    historical schema cannot be inferred safely.
    """
    sqlite = getattr(getattr(engine, "dialect", None), "name", "mysql") == "sqlite"
    if sqlite:
        destination = Path(engine.url.database)
        if (not destination.exists() or destination.stat().st_size == 0) and (SECRET_DIR / "node-vault.key").exists():
            raise RuntimeError(
                "检测到历史节点身份但 SQLite 数据库缺失；禁止用空库替代历史 MySQL。"
                "旧部署请显式设置 DB_TYPE=mysql，或先恢复经过验收的完整 SQLite 备份。"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            os.chmod(destination, 0o600)
        else:
            os.close(descriptor)
    async with engine.connect() as conn:
        if sqlite:
            await conn.execution_options(fc_write_transaction=True)
        existing_tables = await _table_names(conn)
        if sqlite and not existing_tables and (SECRET_DIR / "node-vault.key").exists():
            raise RuntimeError(
                "检测到历史节点身份但 SQLite 数据库没有业务表；禁止用空库替代历史 MySQL。"
                "旧部署请显式设置 DB_TYPE=mysql，或先恢复经过验收的完整 SQLite 备份。"
            )
        if existing_tables:
            if SCHEMA_MARKER_TABLE not in existing_tables:
                await _validate_initialized_schema(conn, existing_tables)
                return

            generation = await _read_schema_generation(conn)
            if generation > SCHEMA_GENERATION:
                raise RuntimeError(
                    "数据库 schema 来自更新版本，禁止旧版本应用启动或降级："
                    f"app={SCHEMA_GENERATION}, database={generation}"
                )
            if generation < SCHEMA_GENERATION:
                await migrate_schema(conn, generation, marker_table=SCHEMA_MARKER_TABLE)
                existing_tables = await _table_names(conn)

            await _validate_initialized_schema(conn, existing_tables)
            return

        if sqlite:
            from app.core.sqlite_schema import LOCAL_DDL
            from app.services.federation.schema import metadata as federation_metadata
            from app.services.karaoke_schema import metadata as karaoke_metadata
            from app.services.media_crypto_schema import metadata as crypto_metadata
            for statement in LOCAL_DDL:
                await conn.execute(text(statement))
            await conn.run_sync(federation_metadata.create_all)
            await conn.run_sync(karaoke_metadata.create_all)
            await conn.run_sync(crypto_metadata.create_all)
            await conn.execute(text("INSERT INTO ip_security_projection(singleton) VALUES (1)"))
            await conn.execute(text("INSERT INTO frontiercloud_schema(singleton,generation,created_at) VALUES (1,:generation,:created_at)"), {"generation": SCHEMA_GENERATION, "created_at": int(time.time())})
            await conn.commit()
            await _validate_initialized_schema(conn, await _table_names(conn))
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
                preference INT NOT NULL DEFAULT 0,
                created_at DATETIME(6) NOT NULL,
                updated_at DATETIME(6) NOT NULL,
                INDEX idx_playback_sort (preference, play_score),
                INDEX idx_playback_path (media_path(191)),
                CONSTRAINT chk_media_preference CHECK (preference BETWEEN -7 AND 500000)
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

        from app.services.media_crypto_schema import metadata as crypto_metadata
        from sqlalchemy.dialects import mysql
        from sqlalchemy.schema import CreateTable
        for table in crypto_metadata.sorted_tables:
            table.dialect_options["mysql"].update(engine="InnoDB", charset="utf8mb4", collate="utf8mb4_bin")
            await _commit_ddl(conn, str(CreateTable(table, if_not_exists=True).compile(dialect=mysql.dialect())))

        await _commit_ddl(conn, MIGRATION_HISTORY_DDL)
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
