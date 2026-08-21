import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PlayerUIContractTests(unittest.TestCase):
    def test_player_reports_wall_clock_play_time_and_exposes_preference_controls(self):
        script = (ROOT / "static" / "js" / "player.js").read_text(encoding="utf-8")
        template = (ROOT / "static" / "media" / "player.html").read_text(encoding="utf-8")

        self.assertIn("performance.now()", script)
        self.assertIn("elapsed <= 2.5", script)
        self.assertIn("/api/v1/media/playback", script)
        self.assertIn("/api/v1/media/preference", script)
        self.assertIn("event.stopPropagation()", script)
        self.assertIn("const playbackSessionId = {{PLAYBACK_SESSION_ID}}", template)

    def test_progress_hit_area_is_large_and_excluded_from_page_gestures(self):
        script = (ROOT / "static" / "js" / "player.js").read_text(encoding="utf-8")
        style = (ROOT / "static" / "css" / "player.css").read_text(encoding="utf-8")

        self.assertIn("--art-progress-height: 26px", style)
        self.assertIn(".art-control-progress-inner { height: 8px", style)
        self.assertIn("function isGestureControl(target)", script)
        self.assertIn(".art-control-progress", script)
        self.assertIn("touchStartedOnControl", script)
        self.assertGreaterEqual(script.count("isGestureControl(e.target)"), 3)


if __name__ == "__main__":
    unittest.main()
