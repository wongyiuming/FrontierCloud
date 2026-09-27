from __future__ import annotations

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

        async def delegate(payload, request, session_hash):
            self.assertEqual(lock.shared_count, 1)
            return {"upload_id": "u1"}

        payload = SimpleNamespace()
        direct_cluster = AsyncMock(side_effect=AssertionError("fence must not bypass recovery wrapper"))
        with (
            patch.object(integrity, "media_mutation_lock", lock),
            patch.object(integrity, "node_state", SimpleNamespace(node={"role": "Master"})),
            patch.object(integrity, "ensure_media_mutations_ready"),
            patch.object(integrity.masterlocal, "create_upload_session", new=AsyncMock(side_effect=delegate)),
            patch.object(integrity.cluster, "create_upload_session", new=direct_cluster),
        ):
            result = await integrity.create_upload_session(payload, object(), "session")

        self.assertEqual(result["upload_id"], "u1")
        self.assertEqual(lock.shared_count, 0)
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

    def test_override_router_precedes_recovery_delete_and_cluster_routes(self):
        from pathlib import Path

        endpoints = Path(__file__).resolve().parents[1].joinpath(
            "app/api/v1/endpoints.py"
        ).read_text(encoding="utf-8")
        self.assertIn("install_master_mutation_integrity()", endpoints)
        master = endpoints.index("_include_admin(master_mutation_router)")
        self.assertLess(master, endpoints.index("_include_admin(masterlocal_recovery_router)"))
        self.assertLess(master, endpoints.index("_include_admin(delete_integrity_router)"))
        self.assertLess(master, endpoints.index("_include_admin(_without_paths(cluster_admin_router"))


if __name__ == "__main__":
    unittest.main()
