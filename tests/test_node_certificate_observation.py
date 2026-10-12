"""Known offline peers must not cause serial network timeouts in AdminGUI."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.api.v1 import admin_nodes


def relationship(identifier, status='online'):
    return {'relationship_id': identifier, 'state': 'active', 'direction': 'downstream',
            'status': status, 'summary': {'certificate': {'state': 'ACTIVE'}},
            'credential': 'private-credential', 'peer_key': 'private-peer-key'}


class CertificateObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_many_offline_peers_skip_rpc_while_online_peers_remain_serial(self):
        relations = [relationship(str(index), 'offline') for index in range(36)]
        relations += [relationship('healthy'), relationship('degraded', 'degraded')]
        active, highest, requested = 0, 0, []
        async def call(row, path, body):
            nonlocal active, highest
            active += 1
            highest = max(highest, active)
            requested.append(row['relationship_id'])
            await asyncio.sleep(0)
            active -= 1
            self.assertEqual(path, '/internal/v1/certificate')
            self.assertEqual(body, {'action': 'status'})
            return {'state': 'ACTIVE', 'https_ready': True, 'controller_running': True}
        with patch.object(admin_nodes.state, 'node', {'node_id': 'master', 'role': 'Master', 'endpoint': 'https://master.4399.com'}), \
                patch.object(admin_nodes.state, 'list_relationships', AsyncMock(return_value=relations)), \
                patch.object(admin_nodes, '_certificate_view', AsyncMock(return_value={'state': 'ACTIVE'})), \
                patch.object(admin_nodes.runtime, 'call', AsyncMock(side_effect=call)), \
                patch('app.services.storage_capacity.observed_pool', AsyncMock(return_value={})):
            result = await admin_nodes.status('test-administrator')
        self.assertEqual(requested, ['healthy', 'degraded'])
        self.assertEqual(highest, 1)
        for row in result['relationships'][:36]:
            certificate = row['summary']['certificate']
            self.assertEqual(certificate['state'], 'UNAVAILABLE')
            self.assertEqual(certificate['last_error'], 'storage_offline')
            for field in ('enabled', 'controller_running', 'https_ready', 'serving_matches'):
                self.assertIsNone(certificate[field])
        self.assertTrue(all('credential' not in row and 'peer_key' not in row for row in result['relationships']))

    async def test_explicit_readonly_refresh_also_skips_known_offline_peer(self):
        with patch.object(admin_nodes.state, 'node', {'node_id': 'master', 'role': 'Master'}), \
                patch.object(admin_nodes.state, 'relationship', AsyncMock(return_value=relationship('offline', 'offline'))), \
                patch.object(admin_nodes.runtime, 'call', AsyncMock()) as call:
            result = await admin_nodes.certificate_status('offline', 'test-administrator')
        self.assertEqual(result['state'], 'UNAVAILABLE')
        call.assert_not_awaited()

    async def test_online_peer_uses_authenticated_control_and_unreachable_is_unknown(self):
        row = relationship('online')
        with patch.object(admin_nodes.state, 'node', {'node_id': 'master', 'role': 'Master', 'endpoint': 'https://master.4399.com'}), \
                patch.object(admin_nodes.state, 'list_relationships', AsyncMock(return_value=[row])), \
                patch.object(admin_nodes, '_certificate_view', AsyncMock(return_value={'state': 'ACTIVE'})), \
                patch.object(admin_nodes.runtime, 'call', AsyncMock(side_effect=OSError('network unavailable'))) as call, \
                patch('app.services.storage_capacity.observed_pool', AsyncMock(return_value={})):
            result = await admin_nodes.status('test-administrator')
        call.assert_awaited_once_with(row, '/internal/v1/certificate', {'action': 'status'})
        self.assertEqual(result['relationships'][0]['summary']['certificate']['state'], 'UNAVAILABLE')
        self.assertIsNone(result['relationships'][0]['summary']['certificate']['https_ready'])
