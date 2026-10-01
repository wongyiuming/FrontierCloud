"""Transactional schema initialization with a file-wide SQLite writer lock."""
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.schema_migrations import MIGRATIONS
from app.store.schema import schema_statements, schema_tables


async def initialize_schema(database: AsyncEngine, generation: int) -> None:
    async with database.connect() as conn:
        # Every worker rereads the marker after acquiring the database writer lock.
        conn = await conn.execution_options(sqlite_write=True)
        async with conn.begin():
            tables = set((await conn.execute(text(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ))).scalars())
            statements = schema_statements("sqlite", generation)
            if tables:
                if "frontiercloud_schema" not in tables:
                    raise RuntimeError("已有数据库缺少 schema marker，不支持对无代际标记数据库自动迁移")
                actual = await conn.scalar(text("SELECT generation FROM frontiercloud_schema WHERE singleton=1"))
                if actual is None:
                    raise RuntimeError("数据库 schema marker 缺少 singleton=1 记录")
                actual = int(actual)
                if actual > generation:
                    raise RuntimeError("数据库 schema 来自更新版本，禁止旧版本应用启动或降级")
                if actual == 1 and generation == 2:
                    journal = next(s for s in statements if s.startswith("CREATE TABLE IF NOT EXISTS frontiercloud_schema_migrations"))
                    await conn.execute(text(journal))
                    migration = MIGRATIONS[2]
                    await conn.execute(text("DELETE FROM frontiercloud_schema_migrations WHERE generation=2"))
                    await conn.execute(text("""
                        INSERT INTO frontiercloud_schema_migrations
                        (generation, migration_name, checksum, applied_at)
                        VALUES (2, :name, :checksum, :now)
                    """), {"name": migration.name, "checksum": migration.checksum, "now": int(time.time())})
                    await conn.execute(text("UPDATE frontiercloud_schema SET generation=2 WHERE singleton=1"))
                elif actual != generation:
                    raise RuntimeError(f"没有受支持的迁移起点：generation={actual}")
            else:
                for statement in statements:
                    await conn.execute(text(statement))
                await conn.execute(text("INSERT INTO ip_security_projection(singleton) VALUES (1)"))
                await conn.execute(text("""
                    INSERT INTO frontiercloud_schema(singleton, generation, created_at)
                    VALUES (1, :generation, :now)
                """), {"generation": generation, "now": int(time.time())})

            tables = set((await conn.execute(text(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ))).scalars())
            missing = schema_tables("sqlite", generation) - tables
            if missing:
                raise RuntimeError("当前代际数据库结构不完整，禁止带病启动；缺少表：" + ", ".join(sorted(missing)))
