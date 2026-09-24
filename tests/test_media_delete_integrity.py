import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import insert, select, text

from app.api.v1 import admin_cluster_integrity as cluster
from app.api.v1 import admin_delete_integrity as integrity
from app.services.federation import schema as s
from tests.test_federation import Database


class MediaDeleteIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = Database()
        self.member_id = "a" * 32
        self.state = SimpleNamespace(
            database=self.database,
            node={"role": "Master", "node_id": self.member_id},
        )
        async with self.database.begin() as conn:
            await conn.execute(insert(s.storage_members).values(
                member_id=self.member_id, relationship_id=None, member_kind="MasterLocal",
                transport="Local", storage_enabled=1, allocated_bytes=1024 ** 3,
                used_bytes=0, reserved_bytes=0, physical_free_bytes=1024 ** 3,
                health="online", writable=1, updated_at=int(time.time()),
            ))

    async def asyncTearDown(self):
        self.database.engine.dispose()

    async def test_completed_session_cannot_keep_path_locator_lease(self):
        path = "vido/artist/show.mp4"
        locator = hashlib.sha256(path.encode()).hexdigest()
        async with self.database.begin() as conn:
            await conn.execute(insert(s.upload_sessions).values(
                upload_id="b" * 32, storage_member_id=self.member_id, media_id="c" * 64,
                media_path=path, path_locator=locator, object_kind="video",
                expected_bytes=1024, state="complete", expires_at=int(time.time()) - 1,
                created_at=1, updated_at=1,
            ))
        with patch.object(integrity, "node_state", self.state):
            await integrity.reconcile_upload_path(path)
        async with self.database.connect() as conn:
            value = await conn.scalar(select(s.upload_sessions.c.path_locator).where(
                s.upload_sessions.c.upload_id == "b" * 32))
        self.assertIsNone(value)

    async def test_pending_masterlocal_delete_converges_before_reupload(self):
        path = "vido/artist/show.mp4"
        locator = hashlib.sha256(path.encode()).hexdigest()
        media_id = "d" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / path
            target.parent.mkdir(parents=True)
            target.write_bytes(b"video")
            async with self.database.begin() as conn:
                await conn.execute(text("UPDATE cluster_storage_members SET used_bytes=5 WHERE member_id=:id"),
                                   {"id": self.member_id})
                await conn.execute(insert(s.global_media).values(
                    media_id=media_id, storage_member_id=self.member_id, object_id=media_id,
                    media_path=path, path_locator=locator, object_kind="video", size_bytes=5,
                    etag='"test"', state="pending_delete", created_at=1, updated_at=1,
                ))
                await conn.execute(text("""
                    INSERT INTO media_objects
                    (media_id, object_kind, media_path, path_locator, created_at, updated_at)
                    VALUES (:id, 'video', :path, :locator, 'now', 'now')
                """), {"id": media_id, "path": path, "locator": locator})
            with patch.object(integrity, "node_state", self.state), \
                 patch.object(integrity, "MEDIA_ROOT", root):
                await integrity.reconcile_upload_path(path)
            self.assertFalse(target.exists())
            async with self.database.connect() as conn:
                self.assertIsNone(await conn.scalar(select(s.global_media.c.media_id).where(
                    s.global_media.c.media_id == media_id)))
                used = await conn.scalar(select(s.storage_members.c.used_bytes).where(
                    s.storage_members.c.member_id == self.member_id))
            self.assertEqual(int(used), 0)

    async def test_live_reservation_is_reported_instead_of_hidden_integrity_error(self):
        path = "vido/artist/show.mp4"
        locator = hashlib.sha256(path.encode()).hexdigest()
        async with self.database.begin() as conn:
            await conn.execute(insert(s.upload_sessions).values(
                upload_id="e" * 32, storage_member_id=self.member_id, media_id="f" * 64,
                media_path=path, path_locator=locator, object_kind="video",
                expected_bytes=1024, state="reserved", expires_at=int(time.time()) + 600,
                created_at=1, updated_at=1,
            ))
        with patch.object(integrity, "node_state", self.state):
            with self.assertRaises(HTTPException) as raised:
                await integrity.reconcile_upload_path(path)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("已有上传正在进行", str(raised.exception.detail))

    async def test_cancel_endpoint_cleans_failed_reservation(self):
        with patch.object(cluster, "_cleanup_upload_session", new=AsyncMock(return_value=True)), \
             patch.object(integrity.admin_service, "audit", new=AsyncMock()):
            result = await integrity.cancel_upload_session("1" * 32, object(), "actor")
        self.assertEqual(result, {"status": "cancelled"})


if __name__ == "__main__":
    unittest.main()
