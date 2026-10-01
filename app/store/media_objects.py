"""Media registry repository; services never select SQL dialects."""
from sqlalchemy import text


class MediaObjects:
    exact_path = "BINARY media_path=BINARY :media_path"
    share_lock = " FOR SHARE"
    insert_prefix = "INSERT IGNORE"

    def __init__(self, connection):
        self.connection = connection

    async def find_path(self, locator, path, *, current=False):
        return await self.connection.scalar(text(
            "SELECT media_id FROM media_objects WHERE path_locator=:path_locator AND "
            + self.exact_path + (self.share_lock if current else "")
        ), {"path_locator": locator, "media_path": path})

    async def legacy_has_data(self, kind, media_id):
        statement = "SELECT EXISTS(SELECT 1 FROM media_lyric_links WHERE lyric_id=:media_id)"
        if kind != "lyric":
            statement = """SELECT (
                EXISTS(SELECT 1 FROM media_playback_stats WHERE media_id=:media_id)
                OR EXISTS(SELECT 1 FROM media_playback_events WHERE media_id=:media_id)
                OR EXISTS(SELECT 1 FROM media_lyric_links WHERE media_id=:media_id))"""
        return bool(await self.connection.scalar(text(statement), {"media_id": media_id}))

    async def register(self, values):
        await self.connection.execute(text(self.insert_prefix + """ INTO media_objects
            (media_id, object_kind, media_path, path_locator, created_at, updated_at)
            VALUES (:media_id, :object_kind, :media_path, :path_locator, :now, :now)
        """), values)

    async def find_locators(self, locators, *, current=False):
        placeholders = ", ".join(f":locator_{index}" for index in range(len(locators)))
        parameters = {f"locator_{index}": value for index, value in enumerate(locators)}
        result = await self.connection.execute(text(
            "SELECT media_id, media_path, path_locator FROM media_objects "
            f"WHERE path_locator IN ({placeholders}) ORDER BY path_locator"
            + (self.share_lock if current else "")
        ), parameters)
        return result.mappings().all()

    async def legacy_business_ids(self, ids):
        placeholders = ", ".join(f":media_{index}" for index in range(len(ids)))
        parameters = {f"media_{index}": value for index, value in enumerate(ids)}
        result = await self.connection.execute(text(f"""
            SELECT media_id AS object_id FROM media_playback_stats WHERE media_id IN ({placeholders})
            UNION SELECT media_id FROM media_playback_events WHERE media_id IN ({placeholders})
            UNION SELECT media_id FROM media_lyric_links WHERE media_id IN ({placeholders})
            UNION SELECT lyric_id FROM media_lyric_links WHERE lyric_id IN ({placeholders})
        """), parameters)
        return {str(row[0]) for row in result.fetchall()}

    async def find_id(self, media_id):
        result = await self.connection.execute(text("""
            SELECT media_id, object_kind, media_path FROM media_objects WHERE media_id=:media_id
        """), {"media_id": media_id})
        row = result.mappings().first()
        return dict(row) if row else None


class SQLiteMediaObjects(MediaObjects):
    exact_path = "media_path=:media_path COLLATE BINARY"
    share_lock = ""
    insert_prefix = "INSERT OR IGNORE"

    async def register(self, values):
        from datetime import datetime
        def timestamps(row):
            row = dict(row)
            if isinstance(row["now"], datetime):
                row["now"] = row["now"].isoformat(sep=" ", timespec="microseconds")
            return row
        await super().register([timestamps(row) for row in values] if isinstance(values, list) else timestamps(values))


def media_objects_repository(connection):
    backend = getattr(getattr(connection, "dialect", None), "name", "mysql")
    return (SQLiteMediaObjects if backend == "sqlite" else MediaObjects)(connection)
