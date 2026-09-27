from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.api.v1 import admin_master_mutation_integrity as integrity


class _Lock:
    def __init__(self):
        self.exclusive = False
        self.shared_count = 0

    async def __aenter__(self):
        self.exclusive = True
        return self

    async def __aexit__(self, *_args):
        self.exclusive = False
        return False

    @asynccontextmanager
    async def shared(self):
        self.shared_count += 1
        try:
            yield self
        finally:
            self.shared_count -= 1


class MasterMutationFenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_master_rename_holds_exclusive_lock_for_entire_delegate(self):
        lock = _Lock()

        async def original(path, new_name):
            self.assertTrue(lock.exclusive)
            self.assertEqual((path, new_name), ("music/old", "new"))
            return {"status": "renamed"}

        with (
            patch.object(integrity, "media_mutation_lock", lock),
            patch.object(integrity, "node_state", SimpleNamespace(node={"role": "Master"})),
            patch.object(integrity, "ensure_media_mutations_ready"),
        ):
            result = await integrity._rename_with_master_lock(original, "music/old", "new")

        self.assertEqual(result["status"], "renamed")
        self.assertFalse(lock.exclusive)

    async def test_master_upload_reservation_fences_existing_recovery_wrapper(self):
        lock = _Lock()
        storage_lock = asyncio.Lock()

        async def delegate(payload, request, session_hash):
            self.assertEqual(lock.shared_count, 1)
            self.assertTrue(storage_lock.locked())
            self.assertEqual(payload.storage_member_id, "m" * 32)
            self.assertEqual(payload.target_dir, "music/artist")
            return {"upload_id": "u1", "transport": "Local"}

        payload = integrity.SiteTypeUploadReservation(
            site_type="primary",
            target_dir="music/artist",
            filename="song.mp3",
            size_bytes=3,
        )
        choose_member = AsyncMock(return_value={"member_id": "m" * 32})
        direct_cluster = AsyncMock(side_effect=AssertionError("fence must not bypass recovery wrapper"))
        with (
            patch.object(integrity, "media_mutation_lock", lock),
            patch.object(integrity.resource_pool, "storage_write_lock", storage_lock),
            patch.object(
                integrity,
                "node_state",
                SimpleNamespace(node={"role": "Master"}, database=object()),
            ),
            patch.object(integrity, "ensure_media_mutations_ready"),
            patch.object(integrity.upload_site_routing, "choose_member", new=choose_member),
            patch.object(integrity.masterlocal, "create_upload_session", new=AsyncMock(side_effect=delegate)),
            patch.object(integrity.cluster, "create_upload_session", new=direct_cluster),
        ):
            result = await integrity.create_upload_session(payload, object(), "session")

        self.assertEqual(result["upload_id"], "u1")
        self.assertEqual(result["site_type"], "primary")
        self.assertEqual(lock.shared_count, 0)
        self.assertFalse(storage_lock.locked())
        choose_member.assert_awaited_once_with("primary", 3, integrity.node_state.database)
        direct_cluster.assert_not_awaited()

    async def test_master_delete_and_visibility_mutations_use_shared_fence(self):
        lock = _Lock()
        payload = {"paths": ["music/album"]}

        async def delete_delegate(request, value, session_hash):
            self.assertEqual(lock.shared_count, 1)
            return {"deleted": 1}

        async def hide_delegate(request, value, session_hash):
            self.assertEqual(lock.shared_count, 1)
            return {"status": "ok"}

        with (
            patch.object(integrity, "media_mutation_lock", lock),
            patch.object(integrity, "node_state", SimpleNamespace(node={"role": "Master"})),
            patch.object(integrity, "ensure_media_mutations_ready"),
            patch.object(integrity.deletion, "delete_objects", new=AsyncMock(side_effect=delete_delegate)),
            patch.object(integrity.cluster, "hide_objects", new=AsyncMock(side_effect=hide_delegate)),
        ):
            deleted = await integrity.delete_objects(object(), payload, "session")
            hidden = await integrity.hide_objects(object(), payload, "session")

        self.assertEqual(deleted["deleted"], 1)
        self.assertEqual(hidden["status"], "ok")
        self.assertEqual(lock.shared_count, 0)

    def test_mutation_router_owns_overridden_paths_once(self):
        from pathlib import Path

        endpoints = Path(__file__).resolve().parents[1].joinpath(
            "app/api/v1/endpoints.py"
        ).read_text(encoding="utf-8")
        self.assertIn("install_master_mutation_integrity()", endpoints)
        self.assertIn("_MASTERLOCAL_OVERRIDE_PATHS = {\"/upload/session\"}", endpoints)
        self.assertIn("_DELETE_OVERRIDE_PATHS = {\"/delete\"}", endpoints)
        self.assertIn('"/hide"', endpoints)
        self.assertIn(
            "_include_admin(_without_paths(masterlocal_recovery_router, _MASTERLOCAL_OVERRIDE_PATHS))",
            endpoints,
        )
        self.assertIn(
            "_include_admin(_without_paths(delete_integrity_router, _DELETE_OVERRIDE_PATHS))",
            endpoints,
        )


if __name__ == "__main__":
    unittest.main()
