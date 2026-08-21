from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from app.core.config import settings

engine: AsyncEngine = create_async_engine(
    settings.MYSQL_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=5,
    max_overflow=10,
)


async def init_db() -> None:
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS admin_token_history (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                token_hash CHAR(64) NOT NULL UNIQUE,
                created_at DATETIME(6) NOT NULL,
                first_used_at DATETIME(6) NULL,
                last_used_at DATETIME(6) NULL,
                expires_at DATETIME(6) NULL,
                use_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
                last_ip VARCHAR(45) NULL,
                last_user_agent VARCHAR(512) NULL,
                failed_attempts BIGINT UNSIGNED NOT NULL DEFAULT 0,
                status VARCHAR(32) NOT NULL DEFAULT 'active',
                invalidated_reason VARCHAR(255) NULL,
                INDEX idx_admin_token_created_at (created_at),
                INDEX idx_admin_token_status (status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS media_visibility (
                relative_path VARCHAR(255) NOT NULL PRIMARY KEY,
                hidden TINYINT(1) NOT NULL DEFAULT 1,
                updated_at DATETIME(6) NOT NULL,
                INDEX idx_media_visibility_hidden (hidden)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        await conn.execute(text("""
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
        """))
        await conn.execute(text("""
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
                INDEX idx_ip_ban_ip_time (ip_address, banned_at),
                INDEX idx_ip_ban_recent (banned_at, status),
                INDEX idx_ip_ban_expiry (expires_at, status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        for column_name, definition in (
            ("ban_kind", "VARCHAR(16) NOT NULL DEFAULT 'auto'"),
            ("reason", "VARCHAR(255) NULL"),
            ("created_by_session_hash", "CHAR(64) NULL"),
        ):
            exists = await conn.scalar(text("""
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_schema=DATABASE()
                  AND table_name='ip_auto_ban_events'
                  AND column_name=:column_name
            """), {"column_name": column_name})
            if not exists:
                await conn.execute(text(
                    f"ALTER TABLE ip_auto_ban_events ADD COLUMN {column_name} {definition}"
                ))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ip_permanent_whitelist (
                ip_address VARCHAR(45) NOT NULL PRIMARY KEY,
                created_at DATETIME(6) NOT NULL,
                created_by_session_hash CHAR(64) NULL,
                note VARCHAR(255) NULL,
                INDEX idx_ip_whitelist_created_at (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS media_playback_stats (
                media_id CHAR(64) NOT NULL PRIMARY KEY,
                media_path VARCHAR(1024) NOT NULL,
                play_score BIGINT UNSIGNED NOT NULL DEFAULT 0,
                preference TINYINT NOT NULL DEFAULT 0,
                created_at DATETIME(6) NOT NULL,
                updated_at DATETIME(6) NOT NULL,
                INDEX idx_playback_sort (preference, play_score),
                CONSTRAINT chk_media_preference CHECK (preference BETWEEN -2 AND 2)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS media_playback_events (
                playback_session_id CHAR(36) NOT NULL,
                media_id CHAR(64) NOT NULL,
                counted_at DATETIME(6) NOT NULL,
                expires_at DATETIME(6) NOT NULL,
                PRIMARY KEY (playback_session_id, media_id),
                INDEX idx_playback_event_expiry (expires_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
