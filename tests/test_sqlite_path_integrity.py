"""Real managed-path matching and durable SQLite configuration boundaries."""
import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from sqlalchemy import insert, select, text
from sqlalchemy.engine import URL

from app.core import config, db
from app.core.config import Settings
from app.core.sqlite_compat import create_sqlite_engine, normalize_statement
from app.services import media_crypto, media_directories, media_manager
from app.services.federation import routing, schema as s
from app.services.federation.catalog import Catalog
from app.services.federation.state import State


class SQLitePathConfigurationTests(unittest.TestCase):
    def test_defaults_follow_the_real_source_or_container_persistent_root(self):
        with patch.dict(os.environ):
            os.environ.pop("SQLITE_PATH", None)
            settings = Settings(_env_file=None)
        self.assertEqual(Path(settings.SQLITE_PATH).parent, config.DATA_ROOT)
        self.assertEqual(Path(settings.SQLITE_PATH).name, "frontiercloud.db")

    def test_absolute_is_insufficient_and_no_path_is_created_by_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            with patch.object(config, "DATA_ROOT", root):
                accepted = root / "database/private.sqlite"
                self.assertEqual(Settings(_env_file=None, SQLITE_PATH=str(accepted)).SQLITE_PATH, str(accepted))
                for target in (Path(directory) / "outside.db", root.parent / "database.db", root,
                               root / "media/state.db", root / "recordings/state.db", root / "tmp/state.db",
                               root / ".tmp/state.db", root / "../outside.db", root / "media/../state.db"):
                    with self.subTest(path=str(target)), self.assertRaises(ValueError):
                        Settings(_env_file=None, SQLITE_PATH=str(target))
            self.assertFalse(root.exists())

    @unittest.skipIf(os.name == "nt", "Linux persistent-volume symlink/hardlink semantics")
    def test_symlink_directory_file_and_hardlink_cannot_escape_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            root, outside = Path(directory) / "data", Path(directory) / "outside"
            root.mkdir(); outside.mkdir()
            (outside / "state.db").write_bytes(b"existing database")
            (root / "linked").symlink_to(outside, target_is_directory=True)
            (root / "alias.db").symlink_to(outside / "state.db")
            os.link(outside / "state.db", root / "hardlink.db")
            with patch.object(config, "DATA_ROOT", root):
                for target in (root / "linked/new.db", root / "alias.db", root / "hardlink.db"):
                    with self.subTest(path=str(target)), self.assertRaises(ValueError):
                        Settings(_env_file=None, SQLITE_PATH=str(target))
            self.assertEqual((outside / "state.db").read_bytes(), b"existing database")
            self.assertFalse((outside / "new.db").exists())

    def test_non_path_like_and_protected_literals_keep_existing_semantics(self):
        self.assertEqual(normalize_statement("SELECT username LIKE :query"), "SELECT username LIKE :query")
        self.assertEqual(normalize_statement("SELECT 'media_path LIKE :path' /* relative_path LIKE :p */"),
                         "SELECT 'media_path LIKE :path' /* relative_path LIKE :p */")


@unittest.skipIf(os.name == "nt", "Linux media paths distinguish Rock and rock")
class SQLitePathRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=media_manager.MEDIA_ROOT.parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.media = self.root / "media"
        for folder in ("music", "vido", "lyrics"):
            (self.media / folder).mkdir(parents=True)
        self.database = create_sqlite_engine(URL.create("sqlite+aiosqlite", database=str(self.root / "state.db")))
        self.patches = [patch.object(db, "engine", self.database), patch.object(db, "SECRET_DIR", self.root / "secrets"),
                        patch.object(media_manager, "MEDIA_ROOT", self.media), patch.object(media_directories, "MEDIA_ROOT", self.media),
                        patch.object(media_directories, "invalidate_media_catalog", new=AsyncMock())]
        for item in self.patches:
            item.start()
        await db.init_db()
        self.store = State(self.database, Fernet.generate_key())
        await self.store.initialize()
        with patch("app.api.v1.media.MEDIA_ROOT", self.media):
            await self.store.promote("Master", "https://master.4399.com", "admin", 2 * 1024 ** 3)
        self.catalog = Catalog(self.store)
        self.patches.extend([patch.object(media_directories, "node_state", self.store),
                             patch.object(media_directories, "node_catalog", self.catalog),
                             patch.object(routing, "state", self.store), patch.object(routing, "catalog", self.catalog)])
        for item in self.patches[-4:]:
            item.start()
        self.paths = ("music/Rock/song.mp3", "music/rock/song.mp3", "music/Rock_100%[x]*?/song.mp3")
        async with self.database.begin() as conn:
            for index, path in enumerate(self.paths, 1):
                physical = self.media / path
                physical.parent.mkdir(parents=True)
                content = b"ID3" + bytes([index]) * 97
                physical.write_bytes(content)
                identifier = hashlib.sha256(("object:" + path).encode()).hexdigest()
                await conn.execute(insert(s.global_media).values(media_id=identifier, storage_member_id=self.store.node["node_id"],
                    object_id=identifier, media_path=path, path_locator=hashlib.sha256(path.encode()).hexdigest(),
                    object_kind="audio", size_bytes=len(content), etag=hashlib.sha256(content).hexdigest(),
                    state="active", created_at=1, updated_at=1))
                await conn.execute(text("INSERT INTO media_visibility(relative_path,hidden,updated_at) VALUES(:path,1,:now)"),
                                   {"path": path, "now": "2026-10-12 00:00:00"})

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.database.dispose()

    async def test_catalog_and_browser_download_plan_scope_are_case_sensitive(self):
        for path in self.paths:
            directory = path.rsplit("/", 1)[0]
            with self.subTest(directory=directory):
                self.assertEqual([row["path"] for row in await self.catalog.resources(directory=directory)], [path])
                self.assertEqual([row["path"] for row in await media_crypto.browser_download_plan([directory])], [path])

    async def test_master_rename_only_moves_exact_case_and_completed_upload_proof(self):
        original = "1" * 32
        async with self.database.begin() as conn:
            await conn.execute(insert(s.upload_sessions).values(upload_id=original, storage_member_id=self.store.node["node_id"],
                media_id="1" * 64, media_path=self.paths[0], path_locator=None, object_kind="audio", expected_bytes=100,
                content_encrypted=0, state="complete", expires_at=1, created_at=1, updated_at=1))
            await conn.execute(insert(s.upload_sessions).values(upload_id="2" * 32, storage_member_id=self.store.node["node_id"],
                media_id="2" * 64, media_path=self.paths[1], path_locator=hashlib.sha256(self.paths[1].encode()).hexdigest(),
                object_kind="audio", expected_bytes=100, content_encrypted=0, state="reserved", expires_at=int(time.time())+300,
                created_at=1, updated_at=int(time.time())))
        result = await media_directories.rename_directory("music/Rock", "Jazz")
        self.assertEqual(result["media_count"], 1)
        self.assertTrue((self.media / "music/Jazz/song.mp3").is_file())
        self.assertTrue((self.media / self.paths[1]).is_file())
        self.assertFalse((self.media / "music/Rock").exists())
        async with self.database.connect() as conn:
            paths = set((await conn.execute(select(s.global_media.c.media_path))).scalars())
            self.assertEqual(paths, {"music/Jazz/song.mp3", self.paths[1], self.paths[2]})
            session_paths = dict((await conn.execute(select(s.upload_sessions.c.upload_id, s.upload_sessions.c.media_path))).all())
            self.assertEqual(session_paths[original], "music/Jazz/song.mp3")
            self.assertEqual(session_paths["2" * 32], self.paths[1])
        self.assertEqual([row["path"] for row in await media_crypto.browser_download_plan(["music/Jazz"])], ["music/Jazz/song.mp3"])

    async def test_raw_path_like_escape_and_delete_never_cross_case_or_wildcards(self):
        async with self.database.begin() as conn:
            pattern = "music/Rock/" + "%"
            rows = (await conn.execute(text("SELECT relative_path FROM media_visibility WHERE relative_path LIKE :pattern ESCAPE '!'"), {"pattern": pattern})).scalars().all()
            self.assertEqual(rows, [self.paths[0]])
            await conn.execute(text("DELETE FROM media_visibility WHERE relative_path LIKE :pattern ESCAPE '!'"), {"pattern": pattern})
            remaining = set((await conn.execute(text("SELECT relative_path FROM media_visibility"))).scalars())
            self.assertEqual(remaining, {self.paths[1], self.paths[2]})
            self.assertEqual(await conn.scalar(text("SELECT 'Rock' LIKE 'rock'")), 1)


if __name__ == "__main__":
    unittest.main()
