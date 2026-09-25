import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services import site_control


class SiteControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.manual = root / ".frontiercloud-maintenance"
        self.force_open = root / ".frontiercloud-force-open"
        self.patchers = [
            patch.object(site_control, "DATA_ROOT", root),
            patch.object(site_control, "MANUAL_MAINTENANCE", self.manual),
            patch.object(site_control, "FORCE_OPEN", self.force_open),
        ]
        for item in self.patchers:
            item.start()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    async def test_manual_maintenance_can_be_entered_and_ended(self):
        with patch.object(site_control.release_control, "agent_status", AsyncMock(return_value={
            "state": "success", "phase": "complete",
        })):
            entered = await site_control.set_maintenance(True)
            self.assertTrue(entered["maintenance"])
            self.assertTrue(self.manual.exists())
            opened = await site_control.set_maintenance(False)
            self.assertFalse(opened["maintenance"])
            self.assertFalse(self.manual.exists())
            self.assertFalse(self.force_open.exists())

    async def test_failed_release_can_be_explicitly_reopened(self):
        with patch.object(site_control.release_control, "agent_status", AsyncMock(return_value={
            "state": "failed", "phase": "failed", "target_sha": "a" * 40,
        })):
            before = await site_control.status()
            self.assertTrue(before["maintenance"])
            opened = await site_control.set_maintenance(False)
            self.assertFalse(opened["maintenance"])
            self.assertTrue(self.force_open.exists())
            self.assertEqual(opened["source"], "manual-open-override")

    async def test_busy_release_cannot_be_forced_open(self):
        with patch.object(site_control.release_control, "agent_status", AsyncMock(return_value={
            "state": "running", "phase": "replacing",
        })):
            with self.assertRaisesRegex(RuntimeError, "版本发布正在执行"):
                await site_control.set_maintenance(False)

    def test_new_release_clears_previous_open_override(self):
        self.force_open.write_text("test\n", encoding="utf-8")
        site_control.prepare_release()
        self.assertFalse(self.force_open.exists())


if __name__ == "__main__":
    unittest.main()
