import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from app.api import internal_nodes

from app.services.federation import protocol


class CapabilityHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_list_is_rejected_before_desired_changes(self):
        request = SimpleNamespace(state=SimpleNamespace(node_control_body=json.dumps({
            "capabilities": [True], "mode": "Direct", "resources": {"storage": {"enabled": True}},
        }).encode()))
        relation = {"direction": "upstream", "mode": "Relay", "relationship_id": "a" * 32, "peer_id": "b" * 32}
        with (
            patch.object(internal_nodes, "authenticated", AsyncMock(return_value=relation)),
            patch.object(internal_nodes.state, "accept_mode", AsyncMock()) as mode,
            patch.object(internal_nodes.resource_pool, "accept_follower_configuration", AsyncMock()) as resources,
        ):
            with self.assertRaises(HTTPException) as raised:
                await internal_nodes.heartbeat(request)
            self.assertEqual(raised.exception.status_code, 400)
            mode.assert_not_awaited()
            resources.assert_not_awaited()

    async def test_legacy_v2_request_preserves_capability_baseline(self):
        request = SimpleNamespace(state=SimpleNamespace(node_control_body=b"{}"))
        with patch.object(internal_nodes, "authenticated", AsyncMock(return_value={"direction": "downstream"})):
            response = await internal_nodes.heartbeat(request)
        self.assertEqual(response["protocol"], 2)
        self.assertEqual(response["capabilities"], list(protocol.BASELINE_CAPABILITIES))


if __name__ == "__main__":
    unittest.main()
