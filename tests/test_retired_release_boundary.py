"""Removed software-update controls stay unreachable; operator fencing wins."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from app.api.v1.admin_nodes import router, require_session
from app.api.internal_control import router as internal_control_router
from app.services import site_control


class RemovedReleaseRoutes(unittest.IsolatedAsyncioTestCase):
    async def test_software_replacement_routes_are_not_registered(self):
        app = FastAPI()
        app.include_router(router, prefix='/api/v1/media/admin')
        app.include_router(internal_control_router)
        app.dependency_overrides[require_session] = lambda: 'authorized-admin'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://4399.com') as client:
            for method, path in [('GET', '/api/v1/media/admin/nodes/release'),
                                 ('POST', '/api/v1/media/admin/nodes/release/upgrade'),
                                 ('POST', '/api/v1/media/admin/nodes/release/rollback'),
                                 ('POST', '/internal/v1/cluster-update/start')]:
                response = await client.request(method, path, json={})
                self.assertEqual(response.status_code, 404, path)

    async def test_old_force_open_marker_cannot_disable_operator_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(site_control, 'DATA_ROOT', root), \
                 patch.object(site_control, 'MANUAL_MAINTENANCE', root / 'manual'), \
                 patch.object(site_control, 'NATIVE_MAINTENANCE', root / 'hard'):
                (root / 'hard').write_text('operator')
                (root / '.frontiercloud-force-open').write_text('obsolete')
                state = await site_control.status()
                self.assertTrue(state['maintenance'])
                self.assertTrue(state['operator_fence'])
                with self.assertRaises(RuntimeError):
                    await site_control.set_maintenance(False)
                self.assertTrue((root / 'hard').exists())

    async def test_manual_maintenance_remains_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(site_control, 'DATA_ROOT', root), \
                 patch.object(site_control, 'MANUAL_MAINTENANCE', root / 'manual'), \
                 patch.object(site_control, 'NATIVE_MAINTENANCE', root / 'hard'):
                self.assertTrue((await site_control.set_maintenance(True))['maintenance'])
                self.assertFalse((await site_control.set_maintenance(False))['maintenance'])
