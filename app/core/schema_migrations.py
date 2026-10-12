"""Versioned, resumable schema migrations for an initialized FrontierCloud database.

MySQL DDL performs implicit commits, so schema migrations cannot promise a transaction
rollback of already-applied DDL. Instead every migration must be idempotent: the schema
generation marker advances only after one generation finishes successfully. A failed
migration therefore remains on the previous generation and is safe to retry on startup.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.schema import CreateTable


BASE_SCHEMA_GENERATION = 1
MIGRATION_HISTORY_TABLE = "frontiercloud_schema_migrations"
MIGRATION_LOCK_TIMEOUT_SECONDS = 60
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

MIGRATION_HISTORY_DDL = """
    CREATE TABLE IF NOT EXISTS frontiercloud_schema_migrations (
        generation INT UNSIGNED NOT NULL PRIMARY KEY,
        migration_name VARCHAR(128) NOT NULL,
        checksum CHAR(64) NOT NULL,
        applied_at BIGINT UNSIGNED NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

MigrationCallable = Callable[[AsyncConnection], Awaitable[None]]


@dataclass(frozen=True)
class SchemaMigration:
    target_generation: int
    name: str
    signature: str
    apply: MigrationCallable

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.signature.encode("utf-8")).hexdigest()


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"unsafe SQL identifier: {value!r}")
    return value


async def table_exists(conn: AsyncConnection, table_name: str) -> bool:
    if _sqlite(conn):
        return bool(await conn.scalar(text("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=:name"), {"name": table_name}))
    value = await conn.scalar(text("""
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_schema=DATABASE()
          AND TABLE_TYPE='BASE TABLE'
          AND table_name=:table_name
    """), {"table_name": table_name})
    return bool(value)


async def column_exists(conn: AsyncConnection, table_name: str, column_name: str) -> bool:
    if _sqlite(conn):
        rows = await conn.execute(text(f"PRAGMA table_info(`{_safe_identifier(table_name)}`)"))
        return any(str(row[1]) == column_name for row in rows)
    value = await conn.scalar(text("""
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema=DATABASE()
          AND table_name=:table_name
          AND column_name=:column_name
    """), {"table_name": table_name, "column_name": column_name})
    return bool(value)


async def index_exists(conn: AsyncConnection, table_name: str, index_name: str) -> bool:
    if _sqlite(conn):
        return bool(await conn.scalar(text("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND tbl_name=:table AND name=:name"), {"table": table_name, "name": index_name}))
    value = await conn.scalar(text("""
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema=DATABASE()
          AND table_name=:table_name
          AND index_name=:index_name
    """), {"table_name": table_name, "index_name": index_name})
    return bool(value)


async def execute_ddl(conn: AsyncConnection, statement: str) -> None:
    """Execute one idempotent DDL operation and make its implicit boundary explicit."""
    await conn.execute(text(statement))
    if not _sqlite(conn):
        await conn.commit()


def _sqlite(conn: AsyncConnection) -> bool:
    return getattr(getattr(conn, "dialect", None), "name", "mysql") == "sqlite"


async def add_column_if_missing(
    conn: AsyncConnection,
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    """Add one column only when it is absent.

    ``definition`` is migration-owned SQL such as ``BIGINT NULL``. Table and column
    identifiers are validated because they are interpolated into DDL.
    """
    table_name = _safe_identifier(table_name)
    column_name = _safe_identifier(column_name)
    if await column_exists(conn, table_name, column_name):
        return
    await execute_ddl(
        conn,
        f"ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` {definition}",
    )


async def add_index_if_missing(
    conn: AsyncConnection,
    table_name: str,
    index_name: str,
    columns_sql: str,
) -> None:
    """Add one index only when it is absent."""
    table_name = _safe_identifier(table_name)
    index_name = _safe_identifier(index_name)
    if await index_exists(conn, table_name, index_name):
        return
    await execute_ddl(
        conn,
        (f"CREATE INDEX `{index_name}` ON `{table_name}` ({columns_sql})" if _sqlite(conn)
         else f"ALTER TABLE `{table_name}` ADD INDEX `{index_name}` ({columns_sql})"),
    )


async def _migration_1_to_2(conn: AsyncConnection) -> None:
    # Generation 2 introduces the migration journal itself. Existing generation-1
    # databases otherwise already have the same business tables, so this is a safe
    # framework migration and proves that an installed database can advance in place.
    if _sqlite(conn):
        from app.core.sqlite_schema import LOCAL_DDL
        statement = next(sql for sql in LOCAL_DDL if sql.startswith("CREATE TABLE IF NOT EXISTS frontiercloud_schema_migrations"))
        await execute_ddl(conn, statement)
    else:
        await execute_ddl(conn, MIGRATION_HISTORY_DDL)


async def _migration_2_to_3(conn: AsyncConnection) -> None:
    """Widen preferences without rewriting identities, scores or timestamps."""
    invalid = await conn.scalar(text("SELECT COUNT(*) FROM media_playback_stats WHERE preference < -7 OR preference > 500000"))
    if int(invalid or 0):
        raise RuntimeError("存量优先级超出 -7..500000；禁止自动截断或重排业务数据")
    if _sqlite(conn):
        from app.core.sqlite_schema import LOCAL_DDL
        expected = {"media_id", "media_path", "play_score", "preference", "created_at", "updated_at"}
        columns = {str(row[1]) for row in await conn.execute(text("PRAGMA table_info(media_playback_stats)"))}
        if columns != expected:
            raise RuntimeError("存量 media_playback_stats 字段与已知代际不符，拒绝丢弃未知字段")
        # All operations below stay in one SQLite transaction. A crash exposes
        # either the complete old table or the complete widened replacement.
        await conn.execute(text("ALTER TABLE media_playback_stats RENAME TO _fc_priority_generation2"))
        await conn.execute(text(next(sql for sql in LOCAL_DDL if sql.startswith("CREATE TABLE IF NOT EXISTS media_playback_stats"))))
        await conn.execute(text("INSERT INTO media_playback_stats(media_id,media_path,play_score,preference,created_at,updated_at) SELECT media_id,media_path,play_score,preference,created_at,updated_at FROM _fc_priority_generation2"))
        await conn.execute(text("DROP TABLE _fc_priority_generation2"))
        for statement in LOCAL_DDL:
            if statement.startswith("CREATE INDEX") and " ON media_playback_stats " in statement:
                await conn.execute(text(statement))
        return
    await execute_ddl(conn, "ALTER TABLE media_playback_stats MODIFY preference INT NOT NULL DEFAULT 0")
    present = await conn.scalar(text("""
        SELECT COUNT(*) FROM information_schema.table_constraints
        WHERE constraint_schema=DATABASE() AND table_name='media_playback_stats'
          AND constraint_name='chk_media_preference' AND constraint_type='CHECK'
    """))
    if present:
        await execute_ddl(conn, "ALTER TABLE media_playback_stats DROP CHECK chk_media_preference")
    await execute_ddl(conn, "ALTER TABLE media_playback_stats ADD CONSTRAINT chk_media_preference CHECK (preference BETWEEN -7 AND 500000)")


async def _migration_3_to_4(conn: AsyncConnection) -> None:
    await add_column_if_missing(conn, "cluster_upload_sessions", "content_encrypted", "TINYINT NOT NULL DEFAULT 0 CHECK (content_encrypted IN (0,1))")
    from app.services.media_crypto_schema import metadata
    for table in metadata.sorted_tables:
        if not _sqlite(conn):
            table.dialect_options["mysql"].update(engine="InnoDB", charset="utf8mb4", collate="utf8mb4_bin")
        if _sqlite(conn):
            await conn.run_sync(table.create, checkfirst=True)
        else:
            await execute_ddl(conn, str(CreateTable(table, if_not_exists=True).compile(dialect=mysql.dialect())))
        for column in table.columns:
            if not await column_exists(conn, table.name, column.name):
                raise RuntimeError(f"加密元数据表结构不完整：{table.name}.{column.name}")
    if not _sqlite(conn):
        await conn.commit()


MIGRATIONS: dict[int, SchemaMigration] = {
    2: SchemaMigration(
        target_generation=2,
        name="create-schema-migration-journal",
        signature="2:create-schema-migration-journal:v1:" + MIGRATION_HISTORY_DDL.strip(),
        apply=_migration_1_to_2,
    ),
    3: SchemaMigration(
        target_generation=3,
        name="widen-media-priority-range",
        signature="3:widen-media-priority-range:v1:int:-7:500000:preserve-identities-scores-timestamps",
        apply=_migration_2_to_3,
    ),
    4: SchemaMigration(
        target_generation=4,
        name="browser-media-encryption-identities",
        signature="4:browser-media-encryption-identities:v1:media_encryption(object_kind,object_id,file_id-unique,descriptor_json-nullable,created_at):media_crypto_keys(singleton,key_id,created_at):cluster_upload_sessions.content_encrypted(boolean-0-1,default-0,signed-capability)",
        apply=_migration_3_to_4,
    ),
}

CURRENT_SCHEMA_GENERATION = max({BASE_SCHEMA_GENERATION, *MIGRATIONS})


async def validate_migration_history(conn: AsyncConnection) -> None:
    if not await table_exists(conn, MIGRATION_HISTORY_TABLE):
        return
    rows = await conn.execute(text(f"SELECT generation,migration_name,checksum FROM `{MIGRATION_HISTORY_TABLE}`"))
    for generation, name, checksum in rows:
        migration = MIGRATIONS.get(int(generation))
        if migration is None or str(name) != migration.name or str(checksum) != migration.checksum:
            raise RuntimeError(f"数据库迁移日志签名不匹配：generation={generation}；禁止重写历史迁移")


def validate_migration_registry() -> None:
    expected = set(range(BASE_SCHEMA_GENERATION + 1, CURRENT_SCHEMA_GENERATION + 1))
    actual = set(MIGRATIONS)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise RuntimeError(
            f"数据库迁移链不连续：missing={missing}, extra={extra}"
        )
    for generation, migration in MIGRATIONS.items():
        if migration.target_generation != generation:
            raise RuntimeError(
                f"数据库迁移注册错误：key={generation}, target={migration.target_generation}"
            )


async def _acquire_lock(conn: AsyncConnection) -> None:
    if _sqlite(conn):
        # The SQLite engine starts IMMEDIATE transactions. Acquire a new one
        # before rereading the generation; its writer lock is process-safe.
        await conn.commit()
        await conn.execution_options(fc_write_transaction=True)
        await conn.begin()
        return
    acquired = await conn.scalar(text("""
        SELECT GET_LOCK(
            CONCAT('frontiercloud:schema-migrations:', DATABASE()),
            :timeout_seconds
        )
    """), {"timeout_seconds": MIGRATION_LOCK_TIMEOUT_SECONDS})
    if int(acquired or 0) != 1:
        raise RuntimeError(
            f"等待数据库迁移锁超时（{MIGRATION_LOCK_TIMEOUT_SECONDS}s），禁止并发迁移"
        )


async def _release_lock(conn: AsyncConnection) -> None:
    if _sqlite(conn):
        return
    try:
        await conn.scalar(text("""
            SELECT RELEASE_LOCK(CONCAT('frontiercloud:schema-migrations:', DATABASE()))
        """))
    except Exception:
        # The lock is connection-scoped and is also released when the connection closes.
        pass


async def migrate_schema(
    conn: AsyncConnection,
    current_generation: int,
    *,
    marker_table: str = "frontiercloud_schema",
) -> int:
    """Migrate one initialized database to ``CURRENT_SCHEMA_GENERATION``.

    Future-generation databases are rejected to prevent an older binary from performing
    a downgrade. The generation marker is updated only after the corresponding migration
    has completed, so interrupted DDL remains retryable.
    """
    validate_migration_registry()
    marker_table = _safe_identifier(marker_table)
    current_generation = int(current_generation)

    if current_generation > CURRENT_SCHEMA_GENERATION:
        raise RuntimeError(
            "数据库 schema 来自更新版本，禁止旧版本应用启动或降级："
            f"app={CURRENT_SCHEMA_GENERATION}, database={current_generation}"
        )
    if current_generation == CURRENT_SCHEMA_GENERATION:
        return current_generation
    if current_generation < BASE_SCHEMA_GENERATION:
        raise RuntimeError(
            f"数据库 schema generation={current_generation} 没有受支持的迁移起点"
        )

    await _acquire_lock(conn)
    try:
        # Another application instance may have migrated while this one waited for the lock.
        locked_generation = await conn.scalar(text(
            f"SELECT generation FROM `{marker_table}` WHERE singleton=1"
        ))
        if locked_generation is None:
            raise RuntimeError("数据库 schema marker 缺失 singleton=1 记录")
        current_generation = int(locked_generation)
        if current_generation > CURRENT_SCHEMA_GENERATION:
            raise RuntimeError(
                "数据库 schema 来自更新版本，禁止旧版本应用启动或降级："
                f"app={CURRENT_SCHEMA_GENERATION}, database={current_generation}"
            )

        await validate_migration_history(conn)

        while current_generation < CURRENT_SCHEMA_GENERATION:
            target = current_generation + 1
            migration = MIGRATIONS.get(target)
            if migration is None:
                raise RuntimeError(
                    f"缺少数据库迁移路径：generation {current_generation} -> {target}"
                )
            try:
                await migration.apply(conn)
                await conn.execute(text(f"""
                    UPDATE `{marker_table}`
                    SET generation=:target
                    WHERE singleton=1 AND generation=:current
                """), {"target": target, "current": current_generation})
                await conn.execute(text(f"""
                    INSERT INTO `{MIGRATION_HISTORY_TABLE}`(
                        generation, migration_name, checksum, applied_at
                    ) VALUES (:generation, :migration_name, :checksum, :applied_at)
                    ON DUPLICATE KEY UPDATE
                        migration_name=VALUES(migration_name),
                        checksum=VALUES(checksum),
                        applied_at=VALUES(applied_at)
                """), {
                    "generation": target,
                    "migration_name": migration.name,
                    "checksum": migration.checksum,
                    "applied_at": int(time.time()),
                })
                await conn.commit()
            except Exception as exc:
                await conn.rollback()
                raise RuntimeError(
                    f"数据库迁移失败：generation {current_generation} -> {target} "
                    f"({migration.name})；generation marker 未推进，可修复后重试"
                ) from exc
            current_generation = target

        return current_generation
    finally:
        await _release_lock(conn)
