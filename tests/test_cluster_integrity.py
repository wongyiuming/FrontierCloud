import io
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile

from app.api.v1 import admin_cluster_integrity as cluster
from app.api.v1 import admin_upload_guard
from app.services.federation.state import state as node_state


def effective_routes(routes):
    """Flatten FastAPI 0.137+ preserved include-router trees for assertions."""
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            yield from effective_routes(candidates())
        else:
            yield route


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
        from main import app

        expected = {
            "/api/v1/media/admin/tree": "app.api.v1.admin_cluster_integrity",
            "/api/v1/media/admin/tree/search": "app.api.v1.admin_cluster_integrity",
            "/api/v1/media/admin/storage-pool": "app.api.v1.admin_masterlocal_recovery",
            "/api/v1/media/admin/upload/session": "app.api.v1.admin_masterlocal_recovery",
            "/api/v1/media/admin/upload/session/{upload_id}/bytes": "app.api.v1.admin_cluster_integrity",
            "/api/v1/media/admin/upload/session/{upload_id}/finalize": "app.api.v1.admin_cluster_integrity",
            "/api/v1/media/admin/upload/item": "app.api.v1.admin_upload_guard",
            "/api/v1/media/admin/hide": "app.api.v1.admin_cluster_integrity",
            "/api/v1/media/admin/download": "app.api.v1.admin_cluster_integrity",
            "/api/v1/media/admin/nodes/{identifier}/revoke": "app.api.v1.admin_cluster_integrity",
        }
        leaves = list(effective_routes(app.routes))
        for path, module in expected.items():
            routes = [route for route in leaves if getattr(route, "path", None) == path]
            self.assertEqual(len(routes), 1, path)
            self.assertEqual(routes[0].endpoint.__module__, module)

    def test_follower_storage_integrity_routes_have_single_owner(self):
        from main import app

        leaves = list(effective_routes(app.routes))
        expected = {
            ("/internal/v1/storage/{original}", "PUT"),
            ("/internal/v1/storage/{original}/stat", "POST"),
        }
        for path, method in expected:
            routes = [
                route for route in leaves
                if getattr(route, "path", None) == path
                and method in (getattr(route, "methods", None) or set())
            ]
            self.assertEqual(len(routes), 1, path)
            self.assertEqual(routes[0].endpoint.__module__, "app.api.internal_storage_integrity")

    def test_karaoke_mutation_routes_have_single_recoverable_owner(self):
        from main import app

        leaves = list(effective_routes(app.routes))
        expected = {
            ("/api/v1/karaoke/account/status", "GET"): "app.api.v1.karaoke_integrity",
            ("/api/v1/karaoke/account/recordings", "GET"): "app.api.v1.karaoke_integrity",
            ("/api/v1/karaoke/account/recordings/ticket", "POST"): "app.api.v1.karaoke_integrity",
            ("/api/v1/karaoke/account/recordings/{recording_id}/pending", "DELETE"): "app.api.v1.karaoke_integrity",
            ("/api/v1/karaoke/account/recordings/{recording_id}", "DELETE"): "app.api.v1.karaoke_integrity",
            ("/api/v1/karaoke/account", "DELETE"): "app.api.v1.karaoke_integrity",
            ("/api/v1/media/admin/users", "GET"): "app.api.v1.admin_karaoke_integrity",
            ("/api/v1/media/admin/users/{user_id}", "POST"): "app.api.v1.admin_karaoke_integrity",
        }
        for (path, method), module in expected.items():
            routes = [
                route for route in leaves
                if getattr(route, "path", None) == path
                and method in (getattr(route, "methods", None) or set())
            ]
            self.assertEqual(len(routes), 1, f"{method} {path}")
            self.assertEqual(routes[0].endpoint.__module__, module)

    async def test_master_legacy_upload_is_rejected_before_disk_write(self):
        upload = UploadFile(filename="song.mp3", file=io.BytesIO(b"ID3payload"))
        with patch.object(node_state, "node", {"role": "Master"}), \
             patch.object(admin_upload_guard.admin_service, "audit", new=AsyncMock()):
            with self.assertRaises(HTTPException) as raised:
                await admin_upload_guard.upload_item(
                    object(), upload, "music/artist", None, "actor"
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertTrue(upload.file.closed)


if __name__ == "__main__":
    unittest.main()
