"""Schema and media-registry checks against an isolated real database."""
import asyncio
import sys

from sqlalchemy import text

from app.core import db, schema_migrations
from app.services import media_objects
from app.store.database import write_transaction


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "verify"
    try:
        await db.init_db()
        if mode == "generation-one-fixture":
            async with write_transaction(db.engine) as connection:
                await connection.execute(text("DROP TABLE frontiercloud_schema_migrations"))
                await connection.execute(text("UPDATE frontiercloud_schema SET generation=1 WHERE singleton=1"))
            print("generation-one fixture prepared")
            return

        paths = [("music/互操作/A.mp3", "audio"), ("music/互操作/a.mp3", "audio")]
        identities = await media_objects.ensure_objects(paths)
        assert identities[paths[0][0]] != identities[paths[1][0]], "media paths lost case sensitivity"
        assert identities == await media_objects.ensure_objects(paths), "media identity changed on restart"
        async with db.engine.connect() as connection:
            generation = await connection.scalar(text("SELECT generation FROM frontiercloud_schema WHERE singleton=1"))
            assert generation == 2
            checksum = await connection.scalar(text("SELECT checksum FROM frontiercloud_schema_migrations WHERE generation=2"))
            if checksum is not None:
                assert checksum == schema_migrations.MIGRATIONS[2].checksum, "migration identity differs between runtimes"
        print(f"Python {db.settings.DB_TYPE}: shared schema and media registry passed")
    finally:
        await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())
