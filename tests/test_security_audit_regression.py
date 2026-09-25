import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from app.api.v1 import admin, admin_nodes, admin_site, admin_transport, endpoints
from app.services import admin_service


def _request(*, scheme="http", client="203.0.113.10", headers=None, path="/api/v1/media/admin"):
    encoded_headers = [
        (str(name).lower().encode("ascii"), str(value).encode("ascii"))
        for name, value in (headers or {}).items()
    ]
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": scheme,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": encoded_headers,
        "client": (client, 43210),
        "server": ("frontiercloud.test", 443 if scheme == "https" else 80),
    })


def _admin_application() -> FastAPI:
    application = FastAPI()
    application.include_router(endpoints.router, prefix="/api/v1")
    return application


class AdminTransportBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_untrusted_peer_cannot_spoof_https_with_forwarded_header(self):
        request = _request(headers={"X-Forwarded-Proto": "https"})
        self.assertFalse(admin_transport.secure_admin_transport(request))

    def test_trusted_reverse_proxy_can_assert_https_for_verified_client(self):
        request = _request(
            client="172.20.0.5",
            headers={
                "X-Real-IP": "203.0.113.77",
                "X-Forwarded-Proto": "https",
            },
        )
        self.assertTrue(admin_transport.secure_admin_transport(request))

    def test_direct_https_and_documented_loopback_http_are_secure_admin_transports(self):
        self.assertTrue(admin_transport.secure_admin_transport(_request(scheme="https")))
        self.assertTrue(admin_transport.secure_admin_transport(_request(client="127.0.0.1")))
        self.assertTrue(admin_transport.secure_admin_transport(_request(client="::1")))

    def test_every_admin_route_has_the_secure_transport_dependency(self):
        admin_routes = [
            route for route in endpoints.router.routes
            if getattr(route, "path", "").startswith("/media/admin")
        ]
        self.assertTrue(admin_routes)
        for route in admin_routes:
            with self.subTest(path=route.path):
                calls = {dependency.call for dependency in route.dependant.dependencies}
                self.assertIn(admin_transport.require_secure_admin_transport, calls)

    async def test_tls_admin_key_login_rejects_insecure_transport_before_secret_verification(self):
        redeem = AsyncMock()
        create_session = AsyncMock()
        application = _admin_application()
        transport = httpx.ASGITransport(
            app=application,
            client=("203.0.113.10", 43210),
        )
        with (
            patch.object(admin_transport.settings, "TLS_ENABLED", True),
            patch.object(admin.admin_service, "redeem_admin_credential", new=redeem),
            patch.object(admin.admin_service, "create_session", new=create_session),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://frontiercloud.test",
            ) as client:
                response = await client.post(
                    "/api/v1/media/admin/elevate",
                    data={"token": "top-secret-admin-key"},
                )

        self.assertEqual(response.status_code, 426)
        redeem.assert_not_awaited()
        create_session.assert_not_awaited()

    async def test_tls_admin_session_rejects_insecure_transport_before_session_lookup(self):
        require_admin = AsyncMock(return_value="session-hash")
        application = _admin_application()
        transport = httpx.ASGITransport(
            app=application,
            client=("203.0.113.10", 43210),
        )
        with (
            patch.object(admin_transport.settings, "TLS_ENABLED", True),
            patch.object(admin.admin_service, "require_admin", new=require_admin),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://frontiercloud.test",
            ) as client:
                response = await client.get("/api/v1/media/admin/status")

        self.assertEqual(response.status_code, 426)
        require_admin.assert_not_awaited()


class ControlPlaneAuditTests(unittest.IsolatedAsyncioTestCase):
    def _audit(self, events):
        async def record(session_hash, action, target_count, source_summary,
                         result, detail, request, **_kwargs):
            events.append(("audit", action, result, source_summary, detail, session_hash))
        return record

    async def test_maintenance_records_intent_before_side_effect_and_success_after(self):
        events = []

        async def set_maintenance(enabled):
            events.append(("side-effect", "maintenance", enabled))
            return {"maintenance": enabled}

        request = _request(scheme="https", path="/api/v1/media/admin/site/maintenance")
        with (
            patch.object(admin_service, "audit", new=self._audit(events)),
            patch.object(admin_site.site_control, "set_maintenance", new=set_maintenance),
            patch.object(admin_site, "require_https", new=lambda _request: None),
        ):
            result = await admin_site.maintenance_change(
                request, admin_site.MaintenanceChange(enabled=True), "session-hash",
            )

        self.assertEqual(result, {"maintenance": True})
        self.assertEqual(events[0][0:3], ("audit", "maintenance_change", "pending"))
        self.assertEqual(events[1], ("side-effect", "maintenance", True))
        self.assertEqual(events[2][0:3], ("audit", "maintenance_change", "success"))

    async def test_maintenance_failure_keeps_pending_and_failed_audit_evidence(self):
        events = []

        async def set_maintenance(_enabled):
            events.append(("side-effect", "maintenance", "failed"))
            raise RuntimeError("maintenance backend unavailable")

        request = _request(scheme="https", path="/api/v1/media/admin/site/maintenance")
        with (
            patch.object(admin_service, "audit", new=self._audit(events)),
            patch.object(admin_site.site_control, "set_maintenance", new=set_maintenance),
            patch.object(admin_site, "require_https", new=lambda _request: None),
        ):
            with self.assertRaises(HTTPException) as raised:
                await admin_site.maintenance_change(
                    request, admin_site.MaintenanceChange(enabled=False), "session-hash",
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(events[0][0:3], ("audit", "maintenance_change", "pending"))
        self.assertEqual(events[1], ("side-effect", "maintenance", "failed"))
        self.assertEqual(events[2][0:3], ("audit", "maintenance_change", "failed"))

    async def test_release_upgrade_and_rollback_are_audited_around_updater_side_effect(self):
        for mode, action, method_name in (
            ("upgrade", "release_upgrade", "start_upgrade"),
            ("rollback", "release_rollback", "start_rollback"),
        ):
            with self.subTest(mode=mode):
                events = []

                async def start():
                    events.append(("side-effect", mode))
                    return {"ok": True, "mode": mode}

                request = _request(scheme="https", path=f"/api/v1/media/admin/nodes/release/{mode}")
                with (
                    patch.object(admin_service, "audit", new=self._audit(events)),
                    patch.object(admin_nodes.release_control, method_name, new=start),
                    patch.object(admin_nodes.site_control, "prepare_release", new=lambda: None),
                    patch.object(admin_nodes, "require_https", new=lambda _request: None),
                ):
                    endpoint = admin_nodes.release_upgrade if mode == "upgrade" else admin_nodes.release_rollback
                    result = await endpoint(request, "session-hash")

                self.assertEqual(result, {"ok": True, "mode": mode})
                self.assertEqual(events[0][0:3], ("audit", action, "pending"))
                self.assertEqual(events[1], ("side-effect", mode))
                self.assertEqual(events[2][0:3], ("audit", action, "success"))

    async def test_release_failure_is_audited_after_pending_intent(self):
        events = []

        async def fail_upgrade():
            events.append(("side-effect", "upgrade", "failed"))
            raise RuntimeError("updater rejected release")

        request = _request(scheme="https", path="/api/v1/media/admin/nodes/release/upgrade")
        with (
            patch.object(admin_service, "audit", new=self._audit(events)),
            patch.object(admin_nodes.release_control, "start_upgrade", new=fail_upgrade),
            patch.object(admin_nodes.site_control, "prepare_release", new=lambda: None),
            patch.object(admin_nodes, "require_https", new=lambda _request: None),
        ):
            with self.assertRaises(HTTPException) as raised:
                await admin_nodes.release_upgrade(request, "session-hash")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(events[0][0:3], ("audit", "release_upgrade", "pending"))
        self.assertEqual(events[1], ("side-effect", "upgrade", "failed"))
        self.assertEqual(events[2][0:3], ("audit", "release_upgrade", "failed"))


if __name__ == "__main__":
    unittest.main()
