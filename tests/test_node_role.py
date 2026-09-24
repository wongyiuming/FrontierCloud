import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from app.middleware.node_role import NodeRoleMiddleware
from app.services.federation.state import state


class NodeRoleBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        inner = FastAPI()

        @inner.get("/{path:path}")
        async def get_path(path: str):
            return {"path": path}

        @inner.post("/{path:path}")
        async def post_path(path: str):
            return {"path": path}

        self.app = NodeRoleMiddleware(inner)
        self.node_patch = patch.object(state, "node", {"role": "Follower"})
        self.relationship_patch = patch.object(state, "list_relationships", new=AsyncMock(return_value=[{
            "direction": "upstream", "state": "active", "peer_endpoint": "https://master.example.com",
        }]))
        self.node_patch.start()
        self.relationship_patch.start()

    async def asyncTearDown(self):
        self.relationship_patch.stop()
        self.node_patch.stop()

    async def test_public_page_redirects_to_master_and_preserves_query(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                     base_url="https://follower.example.com",
                                     follow_redirects=False) as client:
            response = await client.get("/api/v1/media/music?x=1", headers={"Accept": "text/html"})
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "https://master.example.com/api/v1/media/music?x=1")

    async def test_business_api_is_rejected_before_router(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                     base_url="https://follower.example.com") as client:
            response = await client.post("/api/v1/karaoke/account/login", json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {
            "code": "NODE_BUSINESS_DISABLED_ON_FOLLOWER",
            "detail": "业务由 Master 管理",
            "master_url": "https://master.example.com",
        })

    async def test_node_management_and_health_remain_available(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                     base_url="https://follower.example.com") as client:
            node = await client.get("/api/v1/media/admin/nodes")
            health = await client.get("/health")
        self.assertEqual(node.status_code, 200)
        self.assertEqual(health.status_code, 200)

