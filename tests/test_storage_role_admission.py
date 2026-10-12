"""Native startup must not hide a populated Standalone behind a Follower role."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import insert, text
from sqlalchemy.exc import SQLAlchemyError

import main as application
from app.api.v1 import admin_nodes, media
from app.core import db
from app.core.config import settings
from app.core.sqlite_compat import create_sqlite_engine
from app.services import health, karaoke_schema, karaoke_storage, lyrics, media_manager, media_objects, runtime_gate
from app.services.federation import protocol
from app.services.federation.state import state
import app.services.federation.state as state_module


@unittest.skipIf(os.name == "nt", "Real runtime inode leases are tested in the Linux product environment")
class StorageRoleAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="fc-storage-admission-")
        self.root = Path(self.directory.name)
        self.media = self.root / "media"
        self.recordings = self.root / "recordings"
        self.secrets = self.root / "secrets"
        self.path = self.root / "frontiercloud.db"
        for directory in (self.media, self.recordings, self.secrets):
            directory.mkdir()
        self.database = create_sqlite_engine("sqlite+aiosqlite:///" + str(self.path))
        self.recover = AsyncMock()
        self.patches = [
            patch.object(db, "engine", self.database), patch.object(db, "SECRET_DIR", self.secrets),
            patch.object(state_module, "SECRET_DIR", self.secrets),
            patch.object(state, "database", self.database), patch.object(state, "node", {}),
            patch.object(state, "_vault", None), patch.object(health, "engine", self.database),
            patch.object(health.redis_client, "ping", AsyncMock(return_value=True)),
            patch.object(media, "MEDIA_ROOT", self.media), patch.object(lyrics, "MEDIA_ROOT", self.media),
            patch.object(media_manager, "MEDIA_ROOT", self.media),
            patch.object(karaoke_storage, "ROOT", self.recordings),
            patch.object(runtime_gate, "DATA_ROOT", self.root),
            patch.object(settings, "DEPLOYMENT_MODE", "only_stroge"),
            patch.object(settings, "TLS_ENABLED", True),
            patch.object(settings, "STORAGE_ENDPOINT", "https://storage.4399.com"),
            patch.object(application, "recover_interrupted_media_deletions", self.recover),
            patch.object(application, "initialize_ip_security_cache", AsyncMock()),
            patch.object(application, "retry_edge_projection", AsyncMock()),
            patch.object(application, "announce_initial_secrets_once", Mock()),
            patch.object(application, "cleanup_stale_upload_parts", Mock()),
            patch.object(application, "run_stale_upload_cleanup", AsyncMock()),
            patch.object(application.node_runtime, "start", Mock()),
            patch.object(application.node_runtime, "stop", AsyncMock()),
        ]
        for item in self.patches:
            item.start()
        with runtime_gate.acquire():
            pass

    async def asyncTearDown(self):
        await self.database.dispose()
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    async def initialized(self):
        await db.init_db()
        await state.initialize()

    def table_snapshot(self):
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as connection:
            tables = connection.execute("SELECT name,sql FROM sqlite_schema WHERE type='table' ORDER BY name").fetchall()
            result = {}
            for name, ddl in tables:
                rows = connection.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall()
                packed = json.dumps([ddl, sorted(map(repr, rows))], ensure_ascii=False).encode()
                result[name] = hashlib.sha256(packed).hexdigest()
            return result

    def file_snapshot(self):
        return {path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for parent in (self.media, self.recordings, self.secrets)
                for path in parent.rglob("*") if path.is_file()}

    async def assert_rejected_unchanged(self):
        tables = self.table_snapshot()
        files = self.file_snapshot()
        identity = dict(state.node)
        with self.assertRaises((RuntimeError, protocol.ProtocolError, SQLAlchemyError)):
            async with application.lifespan(application.app):
                self.fail("A populated/invalid Standalone was admitted as native Storage")
        self.assertEqual(self.table_snapshot(), tables)
        self.assertEqual(self.file_snapshot(), files)
        self.assertEqual(state.node, identity)
        self.assertEqual(state.node["role"], "Standalone")
        self.recover.assert_not_awaited()
        application.node_runtime.start.assert_not_called()

    async def business_object(self, path="music/artist/retained.mp3", kind="audio"):
        async with self.database.begin() as conn:
            return await media_objects.ensure_object(conn, path, kind)

    async def test_fresh_storage_initializes_follower_and_is_ready(self):
        self.assertFalse(self.path.exists())
        async with application.lifespan(application.app):
            self.assertEqual(state.node["role"], "Follower")
            self.assertEqual(state.node["endpoint"], settings.STORAGE_ENDPOINT)
            self.assertEqual((await health.readiness_response()).status_code, 200)
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text("SELECT role FROM node_identity")), "Follower")
            self.assertEqual(await conn.scalar(text("SELECT COUNT(*) FROM node_audit WHERE action='promote'")), 1)
        self.assertFalse((self.media / lyrics.DEFAULT_LYRIC_PATH).exists())

    async def test_empty_initialized_standalone_preserves_identity_and_control_state(self):
        await self.initialized()
        fallback = self.media / lyrics.DEFAULT_LYRIC_PATH
        fallback.parent.mkdir()
        fallback.write_text(lyrics.DEFAULT_LYRIC_CONTENT, encoding="utf-8")
        await self.business_object(lyrics.DEFAULT_LYRIC_PATH, "lyric")
        async with self.database.begin() as conn:
            await conn.execute(text("INSERT INTO admin_audit_log(action,result,created_at) VALUES ('login','success',:now)"),
                               {"now": "2026-10-12 00:00:00"})
        node_id, private_key = state.node["node_id"], state.node["private_key"]
        files = self.file_snapshot()
        async with application.lifespan(application.app):
            self.assertEqual(state.node["role"], "Follower")
        self.assertEqual(state.node["node_id"], node_id)
        self.assertEqual(state.node["private_key"], private_key)
        self.assertEqual(self.file_snapshot(), files)

    async def test_media_file_without_catalog_is_rejected_unchanged(self):
        await self.initialized()
        target = self.media / "music" / "artist" / "retained.mp3"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"preserved business bytes")
        await self.assert_rejected_unchanged()

    async def test_custom_lyric_and_recording_files_are_rejected_unchanged(self):
        await self.initialized()
        for target in (self.media / "lyrics" / "custom.lrc", self.recordings / "recording.webm"):
            with self.subTest(path=target):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"preserved business bytes")
                await self.assert_rejected_unchanged()
                target.unlink()

    async def test_modified_system_lyric_is_business_data(self):
        await self.initialized()
        target = self.media / lyrics.DEFAULT_LYRIC_PATH
        target.parent.mkdir()
        target.write_text("[00:00.00]user-owned lyrics\n", encoding="utf-8")
        await self.assert_rejected_unchanged()

    async def test_special_media_entry_is_not_treated_as_empty(self):
        await self.initialized()
        target = self.media / "unknown-operation"
        os.mkfifo(target)
        with self.assertRaises((RuntimeError, protocol.ProtocolError, SQLAlchemyError)):
            async with application.lifespan(application.app):
                self.fail("FIFO was treated as an empty media directory")
        self.assertTrue(target.exists())
        self.assertEqual(state.node["role"], "Standalone")
        self.recover.assert_not_awaited()

    async def test_pending_publication_is_not_recovered_after_fixing_follower(self):
        await self.initialized()
        pending = self.root / (".frontiercloud-media-publication-" + "a" * 32 + ".json")
        pending.write_bytes(b"retained publication evidence")
        await self.assert_rejected_unchanged()
        self.assertEqual(pending.read_bytes(), b"retained publication evidence")

    async def test_db_only_media_and_custom_lyric_are_rejected_unchanged(self):
        await self.initialized()
        for path, kind in (("music/artist/missing.mp3", "audio"), ("lyrics/custom.lrc", "lyric")):
            with self.subTest(kind=kind):
                await self.business_object(path, kind)
                await self.assert_rejected_unchanged()
                async with self.database.begin() as conn:
                    await conn.execute(text("DELETE FROM media_objects"))

    async def test_db_only_lyric_relation_is_rejected_unchanged(self):
        await self.initialized()
        async with self.database.begin() as conn:
            await conn.execute(text("""INSERT INTO media_lyric_links
                (media_id,media_path,lyric_id,lyric_path,created_at,updated_at)
                VALUES (:media,:path,:lyric,:lyric_path,:now,:now)"""),
                {"media": "a" * 64, "path": "music/artist/missing.mp3", "lyric": "b" * 64,
                 "lyric_path": lyrics.DEFAULT_LYRIC_PATH, "now": "2026-10-12 00:00:00"})
        await self.assert_rejected_unchanged()

    async def test_karaoke_user_recording_and_audit_are_rejected_unchanged(self):
        await self.initialized()
        now, user = int(time.time()), uuid.uuid4().hex
        cases = (
            (karaoke_schema.users, dict(user_id=user, username="retained", username_key="retained",
                password_hash="opaque-fixture", status="active", quota_bytes=1000,
                used_bytes=0, created_at=now, updated_at=now)),
            (karaoke_schema.recordings, dict(recording_id=uuid.uuid4().hex, user_id=user,
                storage_member_id=state.node["node_id"], filename="retained.webm", content_type="audio/webm",
                size_bytes=50, sha256="a" * 64, state="ready", title="retained", lyrics=[],
                created_at=now, updated_at=now)),
            (karaoke_schema.audit, dict(user_id=None, action="register", result="failed",
                client_ip="203.0.113.1", webrtc_addresses=[], detail={}, created_at=now)),
            (karaoke_schema.registration_daily, dict(client_ip="203.0.113.1", day_key="20261012",
                failure_count=1, success_count=0, updated_at=now)),
        )
        for table, values in cases:
            with self.subTest(table=table.name):
                async with self.database.begin() as conn:
                    await conn.execute(insert(table).values(**values))
                await self.assert_rejected_unchanged()
                async with self.database.begin() as conn:
                    await conn.execute(table.delete())

    async def test_known_follower_with_owned_media_is_not_reclassified(self):
        await self.initialized()
        await state.promote("Follower", settings.STORAGE_ENDPOINT, "fixture")
        await self.business_object()
        (self.recordings / "owned.webm").write_bytes(b"owned storage recording")
        tables, files = self.table_snapshot(), self.file_snapshot()
        async with application.lifespan(application.app):
            self.assertEqual(state.node["role"], "Follower")
            self.assertEqual((await health.readiness_response()).status_code, 200)
        self.assertEqual(self.table_snapshot(), tables)
        self.assertEqual(self.file_snapshot(), files)

    async def test_old_partial_and_unavailable_schema_never_counts_as_empty(self):
        await self.initialized()
        async with self.database.begin() as conn:
            await conn.execute(text("UPDATE frontiercloud_schema SET generation=3"))
        await self.assert_rejected_unchanged()
        async with self.database.begin() as conn:
            await conn.execute(text("UPDATE frontiercloud_schema SET generation=4"))
            await conn.execute(text("DROP TABLE karaoke_audit_log"))
        await self.assert_rejected_unchanged()

    async def test_tls_disabled_rejects_before_persisting_role(self):
        await self.initialized()
        with patch.object(settings, "TLS_ENABLED", False):
            await self.assert_rejected_unchanged()

    async def test_missing_identity_table_is_not_treated_as_fresh_storage(self):
        await self.initialized()
        async with self.database.begin() as conn:
            await conn.execute(text("DROP TABLE node_identity"))
        await self.assert_rejected_unchanged()

    async def test_unreadable_existing_database_is_not_reinitialized(self):
        await self.initialized()
        await self.database.dispose()
        original_files, identity = self.file_snapshot(), dict(state.node)
        self.path.write_bytes(b"invalid SQLite database\n" * 128)
        damaged = self.path.read_bytes()
        with self.assertRaises(SQLAlchemyError):
            async with application.lifespan(application.app):
                self.fail("Unreadable data was replaced with a new storage database")
        self.assertEqual(self.path.read_bytes(), damaged)
        self.assertEqual(self.file_snapshot(), original_files)
        self.assertEqual(state.node, identity)
        self.recover.assert_not_awaited()

    async def test_existing_wal_business_row_is_observed_before_init(self):
        await self.initialized()
        await self.business_object()
        self.assertTrue(self.path.with_name(self.path.name + "-wal").exists())
        await self.assert_rejected_unchanged()

    async def test_admin_promotion_uses_same_admission_and_accepts_control_only_state(self):
        await self.initialized()
        app = FastAPI()
        app.include_router(admin_nodes.router)
        app.dependency_overrides[admin_nodes.require_session] = lambda: "fixture-admin"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://storage.4399.com") as client:
            with patch.object(admin_nodes.transport, "identity", AsyncMock()):
                await self.business_object()
                before = self.table_snapshot()
                denied = await client.post("/nodes/promote", json={"role": "Follower", "endpoint": settings.STORAGE_ENDPOINT})
                self.assertEqual(denied.status_code, 409)
                self.assertEqual(self.table_snapshot(), before)
                async with self.database.begin() as conn:
                    await conn.execute(text("DELETE FROM media_objects"))
                accepted = await client.post("/nodes/promote", json={"role": "Follower", "endpoint": settings.STORAGE_ENDPOINT})
                self.assertEqual(accepted.status_code, 200, accepted.text)
                self.assertEqual(state.node["role"], "Follower")


if __name__ == "__main__":
    unittest.main()
