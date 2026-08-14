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
