from __future__ import annotations

import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.services import playback_continuity_diagnostics as diagnostics

ROOT = Path(__file__).resolve().parents[1]


class PlaybackContinuityDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def test_temporary_interface_has_mandatory_retirement(self):
        retire = datetime.fromisoformat(diagnostics.RETIRE_AT_ISO)
        self.assertIsNotNone(retire.tzinfo)
        now = datetime.now(timezone.utc)
        self.assertLess(
            now,
            retire,
            "Temporary Tesla playback diagnostics reached their retirement date; remove the API, client probe, store, and this test before releasing.",
        )

    def test_retirement_and_non_persistence_are_source_contracts(self):
        api_source = (ROOT / "app/api/v1/playback_continuity_diagnostics.py").read_text(encoding="utf-8")
        store_source = (ROOT / "app/services/playback_continuity_diagnostics.py").read_text(encoding="utf-8")
        client_source = (ROOT / "static/js/playback-continuity-diagnostics.js").read_text(encoding="utf-8")
        loader_source = (ROOT / "static/js/network-observation.js").read_text(encoding="utf-8")
        retirement = "2026-10-15T00:00:00Z"

        self.assertIn("runtime.call(upstream, RELAY_PATH, payload)", api_source)
        self.assertNotIn("state.database", api_source)
        self.assertNotIn("sqlalchemy", store_source.lower())
        self.assertNotIn("redis", store_source.lower())
        self.assertNotIn("pathlib", store_source.lower())
        self.assertNotIn("open(", store_source)
        self.assertIn(retirement, client_source)
        self.assertIn(retirement, loader_source)
        self.assertEqual(diagnostics.RETIRE_AT_ISO, "2026-10-15T00:00:00+00:00")

    async def test_store_is_bounded_and_expires_in_memory(self):
        store = diagnostics.EphemeralDiagnosticStore()
        base = int(time.time())
        for index in range(diagnostics.MAX_REPORTS + 5):
            await store.add(
                {"diagnostic_id": str(index), "stage": "sample", "timeline": []},
                source_node="node",
                delivery="test",
                now=base,
            )
        snapshot = await store.snapshot(now=base)
        self.assertEqual(len(snapshot["reports"]), diagnostics.MAX_REPORTS)
        expired = await store.snapshot(now=base + diagnostics.TTL_SECONDS + 1)
        self.assertEqual(expired["reports"], [])

    def test_sensitive_fields_are_discarded(self):
        report = diagnostics.normalize_report({
            "diagnostic_id": "abc",
            "stage": "failure",
            "sample": {
                "currentSrc": "https://example.test/media?token=secret",
                "authorization": "secret",
                "paused": True,
            },
            "timeline": [{"event": "pause", "href": "https://secret.test/"}],
        })
        self.assertNotIn("currentSrc", report["sample"])
        self.assertNotIn("authorization", report["sample"])
        self.assertNotIn("href", report["timeline"][0])
        self.assertTrue(report["sample"]["paused"])


if __name__ == "__main__":
    unittest.main()
