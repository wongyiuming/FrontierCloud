"""P0 regression: remote Admin credentials must never cross plain HTTP."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from app.api.v1 import admin, endpoints


class RemoteAdminTransportP0Tests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_http_rejects_admin_key_before_credential_verification_even_without_tls_mode(self):
        application = FastAPI()
        application.include_router(endpoints.router, prefix="/api/v1")
        redeem = AsyncMock()
        create_session = AsyncMock()
        transport = httpx.ASGITransport(app=application, client=("203.0.113.10", 43210))

        with (
            patch.object(admin.admin_service, "redeem_admin_credential", new=redeem),
            patch.object(admin.admin_service, "create_session", new=create_session),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://frontiercloud.test",
            ) as client:
                response = await client.post(
                    "/api/v1/media/admin/elevate",
                    data={"token": "must-not-cross-plain-http"},
                )

        self.assertEqual(response.status_code, 426)
        redeem.assert_not_awaited()
        create_session.assert_not_awaited()

    async def test_loopback_http_remains_available_for_local_recovery_workflow(self):
        application = FastAPI()
        application.include_router(endpoints.router, prefix="/api/v1")
        redeem = AsyncMock(side_effect=RuntimeError("transport boundary passed"))
        transport = httpx.ASGITransport(app=application, client=("127.0.0.1", 43210))

        with patch.object(admin.admin_service, "redeem_admin_credential", new=redeem):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                with self.assertRaisesRegex(RuntimeError, "transport boundary passed"):
                    await client.post(
                        "/api/v1/media/admin/elevate",
                        data={"token": "local-only"},
                    )

        redeem.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
