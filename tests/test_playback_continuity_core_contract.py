from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PlaybackContinuityCoreContractTests(unittest.TestCase):
    def setUp(self):
        self.core = (ROOT / "static/js/playback-continuity-handoff.js").read_text(encoding="utf-8")
        self.loader = (ROOT / "static/js/network-observation.js").read_text(encoding="utf-8")
        self.frontend_smoke = (ROOT / "tests/playback_handoff_smoke.mjs").read_text(encoding="utf-8")
        self.wiki = (ROOT / "docs/wiki/Playback-Continuity.md").read_text(encoding="utf-8")
        self.design = (ROOT / "docs/playback-continuity.md").read_text(encoding="utf-8")

    def test_audio_continuity_core_is_permanent_and_loaded(self):
        self.assertIn("const HANDOFF_SECONDS = 0.2;", self.core)
        self.assertIn("/static/js/playback-continuity-handoff.js", self.loader)
        core_loader = self.loader.split("function loadPlaybackContinuityCore", 1)[1].split(
            "function loadTemporaryPlaybackContinuityDiagnostics", 1
        )[0]
        self.assertNotIn("retireAt", core_loader)
        self.assertNotIn("RETIRE", core_loader)

    def test_standby_must_start_before_old_deck_is_released(self):
        start = self.core.index("await candidate.element.play()")
        pause = self.core.index("oldVideo.pause()", start)
        switch = self.core.index("originalSelectMedia(targetIndex)", pause)
        self.assertLess(start, pause)
        self.assertLess(pause, switch)
        self.assertIn("MIN_READY_STATE = 3", self.core)
        self.assertIn("preend_standby_ready", self.core)
        self.assertIn("preend_bridge_active", self.core)
        self.assertIn("preend_main_deck_resumed", self.core)

    def test_failed_early_handoff_preserves_ended_fallback(self):
        self.assertIn("handoffInFlight = false;", self.core)
        self.assertIn("preend_handoff_failed", self.core)
        self.assertIn("playNext('legacy-next')", self.frontend_smoke)
        self.assertIn("failed standby play must never cut the old track early", self.frontend_smoke)
        self.assertIn("normal ended-style fallback remains available", self.frontend_smoke)

    def test_system_media_controls_are_deduplicated_after_handoff(self):
        self.assertIn("const SYSTEM_DEDUP_MS = 900;", self.core)
        for source in (
            "media-session-next",
            "media-session-prev",
            "media-key-next",
            "media-key-prev",
        ):
            self.assertIn(source, self.core)
        self.assertIn("duplicate system media control must be suppressed after handoff", self.frontend_smoke)

    def test_core_contract_is_documented(self):
        for document in (self.wiki, self.design):
            self.assertIn("200 ms", document)
            self.assertIn("standby", document.lower())
            self.assertIn("ended fallback", document.lower())
            self.assertIn("core", document.lower())


if __name__ == "__main__":
    unittest.main()
