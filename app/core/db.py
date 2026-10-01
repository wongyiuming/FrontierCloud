import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import settings
from app.core.schema_migrations import (
    CURRENT_SCHEMA_GENERATION,
    MIGRATION_HISTORY_TABLE,
    migrate_schema,
)

from app.store.database import create_database_engine
from app.store.schema import schema_statements

engine = create_database_engine(settings)

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


async def init_db() -> None:
    """Bootstrap an empty database or migrate an initialized database in place.

    Databases created by FrontierCloud carry a schema-generation marker. Older marked
    generations are upgraded sequentially through the migration registry under a MySQL
    advisory lock. An unmarked non-empty database is deliberately rejected because its
    historical schema cannot be inferred safely.
    """
    if settings.DB_TYPE == "sqlite":
        from app.store.sqlite_schema import initialize_schema
        await initialize_schema(engine, SCHEMA_GENERATION)
        return
    async with engine.connect() as conn:
        existing_tables = await _table_names(conn)
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

        for statement in schema_statements("mysql", SCHEMA_GENERATION):
            await _commit_ddl(conn, statement)
        await conn.execute(text("INSERT INTO ip_security_projection(singleton) VALUES (1)"))
        await conn.commit()
        await conn.execute(text("""
            INSERT INTO frontiercloud_schema(singleton, generation, created_at)
            VALUES (1, :generation, :created_at)
        """), {"generation": SCHEMA_GENERATION, "created_at": int(time.time())})
        await conn.commit()
        await _validate_initialized_schema(conn, await _table_names(conn))


async def close_db() -> None:
    await engine.dispose()
