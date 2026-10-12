from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import federation_mode_integrity, resource_pool
from app.services.federation import protocol as p


class ModeChangeUploadFenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reserved_upload_blocks_transport_change(self):
        store = SimpleNamespace(
            relationship=AsyncMock(return_value={"mode": "Relay", "peer_id": "f" * 32, "peer_endpoint": "https://storage.example.com"}),
        )
        original = AsyncMock()
        with patch.object(
            federation_mode_integrity,
            "_reserved_upload_count",
            new=AsyncMock(return_value=1),
        ):
            with self.assertRaises(p.ProtocolError):
                await federation_mode_integrity._set_mode_with_upload_fence(
                    original, store, "r" * 32, "Direct", "admin",
                )
        original.assert_not_awaited()

    async def test_transport_change_runs_under_storage_write_lock_after_reservation_clears(self):
        store = SimpleNamespace(
            relationship=AsyncMock(return_value={"mode": "Relay", "peer_id": "f" * 32, "peer_endpoint": "https://storage.example.com"}),
        )
        observed = []

        async def original(current_store, identifier, mode, actor):
            observed.append(resource_pool.storage_write_lock.locked())
            return {"mode": mode}

        with patch.object(
            federation_mode_integrity,
            "_reserved_upload_count",
            new=AsyncMock(return_value=0),
        ):
            result = await federation_mode_integrity._set_mode_with_upload_fence(
                original, store, "r" * 32, "Direct", "admin",
            )
        self.assertEqual(result, {"mode": "Direct"})
        self.assertEqual(observed, [True])

    async def test_noop_mode_selection_does_not_wait_for_upload_completion(self):
        store = SimpleNamespace(
            relationship=AsyncMock(return_value={"mode": "Direct", "peer_id": "f" * 32, "peer_endpoint": "https://storage.example.com"}),
        )
        original = AsyncMock(return_value=None)
        with patch.object(
            federation_mode_integrity,
            "_reserved_upload_count",
            new=AsyncMock(return_value=1),
        ) as reserved:
            await federation_mode_integrity._set_mode_with_upload_fence(
                original, store, "r" * 32, "Direct", "admin",
            )
        reserved.assert_not_awaited()
        original.assert_awaited_once_with(store, "r" * 32, "Direct", "admin")

    async def test_private_relay_endpoint_cannot_be_published_as_direct(self):
        store = SimpleNamespace(relationship=AsyncMock(return_value={
            "mode": "Relay", "peer_id": "f" * 32,
            "peer_endpoint": "https://storage.example.com:8443",
        }))
        original = AsyncMock()
        with self.assertRaisesRegex(p.ProtocolError, "443"):
            await federation_mode_integrity._set_mode_with_upload_fence(
                original, store, "r" * 32, "Direct", "admin",
            )
        original.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
