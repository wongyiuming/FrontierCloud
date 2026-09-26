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
from sqlalchemy.ext.asyncio import AsyncConnection


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
    value = await conn.scalar(text("""
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_schema=DATABASE()
          AND TABLE_TYPE='BASE TABLE'
          AND table_name=:table_name
    """), {"table_name": table_name})
    return bool(value)


async def column_exists(conn: AsyncConnection, table_name: str, column_name: str) -> bool:
    value = await conn.scalar(text("""
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema=DATABASE()
          AND table_name=:table_name
          AND column_name=:column_name
    """), {"table_name": table_name, "column_name": column_name})
    return bool(value)


async def index_exists(conn: AsyncConnection, table_name: str, index_name: str) -> bool:
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
    await conn.commit()


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
        f"ALTER TABLE `{table_name}` ADD INDEX `{index_name}` ({columns_sql})",
    )


async def _migration_1_to_2(conn: AsyncConnection) -> None:
    # Generation 2 introduces the migration journal itself. Existing generation-1
    # databases otherwise already have the same business tables, so this is a safe
    # framework migration and proves that an installed database can advance in place.
    await execute_ddl(conn, MIGRATION_HISTORY_DDL)


MIGRATIONS: dict[int, SchemaMigration] = {
    2: SchemaMigration(
        target_generation=2,
        name="create-schema-migration-journal",
        signature="2:create-schema-migration-journal:v1:" + MIGRATION_HISTORY_DDL.strip(),
        apply=_migration_1_to_2,
    ),
}

CURRENT_SCHEMA_GENERATION = max({BASE_SCHEMA_GENERATION, *MIGRATIONS})


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
