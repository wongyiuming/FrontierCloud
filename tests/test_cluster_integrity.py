import io
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile

from app.api.v1 import admin_cluster_integrity as cluster
from app.services.federation.state import state as node_state


class ClusterUploadPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_folder_upload_from_media_root_is_validated_after_join(self):
        payload = cluster.ClusterUploadReservation(
            target_dir="music",
            relative_path="黄耀明/大型演出/达明一派_1996年万岁.mp3",
            filename="ignored.mp3",
            size_bytes=1024,
        )
        with patch.object(cluster.node_catalog, "resources", new=AsyncMock(return_value=[])):
            self.assertEqual(
                await cluster._upload_logical_path(payload),
                "music/黄耀明/大型演出/达明一派_1996年万岁.mp3",
            )

    async def test_folder_upload_from_category_is_valid(self):
        payload = cluster.ClusterUploadReservation(
            target_dir="music/黄耀明",
            relative_path="大型演出/song.mp3",
            filename="ignored.mp3",
            size_bytes=1024,
        )
        with patch.object(cluster.node_catalog, "resources", new=AsyncMock(return_value=[])):
            self.assertEqual(
                await cluster._upload_logical_path(payload),
                "music/黄耀明/大型演出/song.mp3",
            )

    async def test_single_file_upload_requires_a_category(self):
        payload = cluster.ClusterUploadReservation(
            target_dir="music", filename="song.mp3", size_bytes=1024,
        )
        with patch.object(cluster.node_catalog, "resources", new=AsyncMock(return_value=[])):
            with self.assertRaises(HTTPException):
                await cluster._upload_logical_path(payload)

    async def test_too_deep_folder_upload_is_rejected(self):
        payload = cluster.ClusterUploadReservation(
            target_dir="music",
            relative_path="artist/album/disc/song.mp3",
            filename="ignored.mp3",
            size_bytes=1024,
        )
        with patch.object(cluster.node_catalog, "resources", new=AsyncMock(return_value=[])):
            with self.assertRaises(HTTPException):
                await cluster._upload_logical_path(payload)

    def test_auto_placement_is_not_a_real_member_id(self):
        self.assertIsNone(cluster._preferred_member(None))
        self.assertIsNone(cluster._preferred_member(""))
        self.assertIsNone(cluster._preferred_member("auto"))
        self.assertEqual(cluster._preferred_member("a" * 32), "a" * 32)
        with self.assertRaises(HTTPException):
            cluster._preferred_member("not-a-member")


class ClusterRouteIntegrityTests(unittest.IsolatedAsyncioTestCase):
    def test_master_admin_routes_have_single_integrity_owner(self):
        from app.api.v1.endpoints import router

        expected = {
            "/media/admin/tree",
            "/media/admin/tree/search",
            "/media/admin/storage-pool",
            "/media/admin/upload/session",
            "/media/admin/upload/session/{upload_id}/bytes",
            "/media/admin/upload/session/{upload_id}/finalize",
            "/media/admin/upload/item",
            "/media/admin/hide",
            "/media/admin/download",
            "/media/admin/nodes/{identifier}/revoke",
        }
        for path in expected:
            routes = [route for route in router.routes if route.path == path]
            self.assertEqual(len(routes), 1, path)
            self.assertEqual(routes[0].endpoint.__module__, "app.api.v1.admin_cluster_integrity")

    def test_follower_storage_put_has_single_crash_safe_owner(self):
        from app.api import internal_nodes
        routes = [route for route in internal_nodes.router.routes
                  if route.path == "/internal/v1/storage/{original}" and "PUT" in (route.methods or set())]
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].endpoint.__module__, "app.api.internal_storage_integrity")

    async def test_master_legacy_upload_is_rejected_before_disk_write(self):
        upload = UploadFile(filename="song.mp3", file=io.BytesIO(b"ID3payload"))
        with patch.object(node_state, "node", {"role": "Master"}), \
             patch.object(cluster.admin_service, "audit", new=AsyncMock()):
            with self.assertRaises(HTTPException) as raised:
                await cluster.upload_item(object(), upload, "music/artist", None, "actor")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertTrue(upload.file.closed)


if __name__ == "__main__":
    unittest.main()
