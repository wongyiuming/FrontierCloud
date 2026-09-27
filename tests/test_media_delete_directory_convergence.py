from __future__ import annotations

import hashlib
import unittest

from sqlalchemy import delete, insert, select, text

from app.services import media_delete_convergence as convergence
from app.services.federation import schema as s
from tests.test_federation import Database


class DirectoryDeleteConvergenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = Database()
        async with self.database.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS media_visibility (
                    relative_path TEXT PRIMARY KEY,
                    hidden INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT
                )
            """))

    async def asyncTearDown(self):
        self.database.engine.dispose()

    async def _directory(self, path: str, preference: int = 9):
        media_id = hashlib.sha256(("directory:" + path).encode()).hexdigest()
        locator = hashlib.sha256(path.encode()).hexdigest()
        async with self.database.begin() as conn:
            await conn.execute(text("""
                INSERT INTO media_objects
                (media_id, object_kind, media_path, path_locator, created_at, updated_at)
                VALUES (:id, 'directory', :path, :locator, 'now', 'now')
            """), {"id": media_id, "path": path, "locator": locator})
            await conn.execute(text("""
                INSERT INTO media_playback_stats
                (media_id, media_path, play_score, preference, created_at, updated_at)
                VALUES (:id, :path, 0, :preference, 'now', 'now')
            """), {"id": media_id, "path": path, "preference": preference})
            await conn.execute(text("""
                INSERT INTO media_visibility(relative_path, hidden, updated_at)
                VALUES (:path, 1, 'now')
            """), {"path": path})
        return media_id

    async def _global_media(self, media_id: str, path: str, state: str = "active"):
        async with self.database.begin() as conn:
            await conn.execute(insert(s.global_media).values(
                media_id=media_id,
                storage_member_id="a" * 32,
                object_id=media_id,
                media_path=path,
                path_locator=hashlib.sha256(path.encode()).hexdigest(),
                object_kind="audio",
                size_bytes=1,
                etag='"1"',
                state=state,
                created_at=1,
                updated_at=1,
            ))

    async def _exists(self, table: str, column: str, value: str) -> bool:
        async with self.database.connect() as conn:
            return bool(await conn.scalar(text(
                f"SELECT COUNT(*) FROM {table} WHERE {column}=:value"
            ), {"value": value}))

    async def test_directory_metadata_is_kept_while_managed_media_remains(self):
        await self._directory("music/artist")
        await self._directory("music/artist/album")
        await self._global_media("b" * 64, "music/artist/album/song.wav")

        cleaned = await convergence.cleanup_empty_ancestors(
            "music/artist/album/song.wav", self.database,
        )

        self.assertEqual(cleaned, 0)
        self.assertTrue(await self._exists("media_objects", "media_path", "music/artist/album"))
        self.assertTrue(await self._exists("media_visibility", "relative_path", "music/artist"))

    async def test_last_media_removes_subdir_and_category_priority_and_visibility(self):
        await self._directory("music/artist")
        await self._directory("music/artist/album")
        await self._global_media("c" * 64, "music/artist/album/song.wav")
        async with self.database.begin() as conn:
            await conn.execute(delete(s.global_media).where(s.global_media.c.media_id == "c" * 64))

        cleaned = await convergence.cleanup_empty_ancestors(
            "music/artist/album/song.wav", self.database,
        )

        self.assertEqual(cleaned, 2)
        for path in ("music/artist", "music/artist/album"):
            self.assertFalse(await self._exists("media_objects", "media_path", path))
            self.assertFalse(await self._exists("media_playback_stats", "media_path", path))
            self.assertFalse(await self._exists("media_visibility", "relative_path", path))

    async def test_parent_metadata_survives_when_sibling_media_remains(self):
        await self._directory("music/artist")
        await self._directory("music/artist/album")
        await self._directory("music/artist/live")
        await self._global_media("d" * 64, "music/artist/album/song.wav")
        await self._global_media("e" * 64, "music/artist/live/song.wav")
        async with self.database.begin() as conn:
            await conn.execute(delete(s.global_media).where(s.global_media.c.media_id == "d" * 64))

        cleaned = await convergence.cleanup_empty_ancestors(
            "music/artist/album/song.wav", self.database,
        )

        self.assertEqual(cleaned, 1)
        self.assertFalse(await self._exists("media_objects", "media_path", "music/artist/album"))
        self.assertTrue(await self._exists("media_objects", "media_path", "music/artist"))
        self.assertTrue(await self._exists("media_objects", "media_path", "music/artist/live"))

    def test_convergence_is_installed_at_api_bootstrap(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        endpoints = (root / "app/api/v1/endpoints.py").read_text(encoding="utf-8")
        self.assertIn("install_media_delete_convergence()", endpoints)


if __name__ == "__main__":
    unittest.main()
