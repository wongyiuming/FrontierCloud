import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import site_control


class SiteControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.manual = root / '.frontiercloud-maintenance'
        self.hard = root / '.frontiercloud-native-maintenance'
        self.patchers = [patch.object(site_control, 'DATA_ROOT', root),
                         patch.object(site_control, 'MANUAL_MAINTENANCE', self.manual),
                         patch.object(site_control, 'NATIVE_MAINTENANCE', self.hard)]
        for item in self.patchers:
            item.start()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    async def test_manual_maintenance_can_be_entered_and_ended(self):
        self.assertTrue((await site_control.set_maintenance(True))['maintenance'])
        self.assertTrue(self.manual.exists())
        self.assertFalse((await site_control.set_maintenance(False))['maintenance'])
        self.assertFalse(self.manual.exists())

    async def test_operator_fence_cannot_be_explicitly_reopened(self):
        self.hard.write_text('backup in progress')
        self.assertEqual((await site_control.status())['source'], 'operator')
        with self.assertRaises(RuntimeError):
            await site_control.set_maintenance(False)
        self.assertTrue(self.hard.exists())

    async def test_removed_force_open_marker_has_no_authority(self):
        (self.hard.parent / '.frontiercloud-force-open').write_text('obsolete')
        self.hard.write_text('operator')
        self.assertTrue((await site_control.status())['operator_fence'])

    async def test_ending_manual_maintenance_preserves_unrelated_data(self):
        marker = self.hard.parent / 'recovery-receipt.json'
        marker.write_text('{}')
        await site_control.set_maintenance(True)
        await site_control.set_maintenance(False)
        self.assertEqual(marker.read_text(), '{}')
