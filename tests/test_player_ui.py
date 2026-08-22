import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PlayerUIContractTests(unittest.TestCase):
    def test_player_reports_wall_clock_play_time_and_exposes_preference_controls(self):
        script = (ROOT / "static" / "js" / "player.js").read_text(encoding="utf-8")
        audio_template = (ROOT / "static" / "media" / "audio-player.html").read_text(encoding="utf-8")
        video_template = (ROOT / "static" / "media" / "video-player.html").read_text(encoding="utf-8")

        self.assertIn("performance.now()", script)
        self.assertIn("elapsed <= 2.5", script)
        self.assertIn("/api/v1/media/playback", script)
        self.assertIn("/api/v1/media/preference", script)
        self.assertIn("event.stopPropagation()", script)
        self.assertIn("const playbackSessionId = {{PLAYBACK_SESSION_ID}}", audio_template)
        self.assertIn("const PLAYER_KIND = 'audio'", audio_template)
        self.assertIn("const PLAYER_KIND = 'video'", video_template)

    def test_audio_and_video_players_use_independent_templates(self):
        api = (ROOT / "app" / "api" / "v1" / "media.py").read_text(encoding="utf-8")

        self.assertIn('load_html_template("audio-player.html")', api)
        self.assertIn('load_html_template("video-player.html")', api)
        self.assertFalse((ROOT / "static" / "media" / "player.html").exists())

    def test_progress_hit_area_is_large_and_excluded_from_page_gestures(self):
        script = (ROOT / "static" / "js" / "player.js").read_text(encoding="utf-8")
        style = (ROOT / "static" / "css" / "player.css").read_text(encoding="utf-8")

        self.assertIn("--art-progress-height: 26px", style)
        self.assertIn(".art-control-progress-inner { height: 8px", style)
        self.assertIn("function isGestureControl(target)", script)
        self.assertIn(".art-control-progress", script)
        self.assertIn("let pointerId = null", script)
        self.assertGreaterEqual(script.count("isGestureControl(event.target)"), 4)

    def test_audio_player_has_visible_split_zone_and_persistent_progress(self):
        script = (ROOT / "static" / "js" / "player.js").read_text(encoding="utf-8")
        style = (ROOT / "static" / "css" / "player.css").read_text(encoding="utf-8")
        template = (ROOT / "static" / "media" / "audio-player.html").read_text(encoding="utf-8")

        self.assertIn("const DIRECT_SEEK_ZONE_START = 0.75", script)
        self.assertIn("seekToHorizontalPosition(event.clientX, playerSection)", script)
        self.assertGreaterEqual(script.count("const isDirectSeekZone = verticalPlayerRatio"), 2)
        self.assertIn("initAudioGestureControl", script)
        self.assertIn("单击下一首", template)
        self.assertIn("双击上一首", template)
        self.assertIn("拖动微调进度条", template)
        self.assertIn("点击跳转", template)
        self.assertIn('class="interaction-boundary"', template)
        self.assertIn("top: 75%", style)
        self.assertIn(".audio-player-page .artplayer-app .art-bottom { opacity: 1", style)

    def test_video_uses_split_seek_without_overlay_or_track_switch_gestures(self):
        script = (ROOT / "static" / "js" / "player.js").read_text(encoding="utf-8")
        template = (ROOT / "static" / "media" / "video-player.html").read_text(encoding="utf-8")
        video_gesture = script.split("function initVideoGestureControl()", 1)[1].split("function initGestureControl()", 1)[0]

        self.assertIn("verticalPlayerRatio(event, playerSection)", video_gesture)
        self.assertIn("seekToHorizontalPosition(event.clientX, playerSection)", video_gesture)
        self.assertNotIn("playNext()", video_gesture)
        self.assertNotIn("playPrev()", video_gesture)
        self.assertNotIn("showGestureHud", video_gesture)
        self.assertNotIn("audio-interaction-guide", template)
        self.assertNotIn("corner-text", template)
        self.assertNotIn("audio-cover-container", template)
        self.assertNotIn("gesture-hud", template)

    def test_media_ui_assets_are_versioned_and_offer_cache_reset(self):
        api = (ROOT / "app" / "api" / "v1" / "media.py").read_text(encoding="utf-8")
        nginx = (ROOT / "nginx" / "nginx.conf").read_text(encoding="utf-8")
        templates = [
            (ROOT / "static" / "media" / name).read_text(encoding="utf-8")
            for name in ("index.html", "category.html", "audio-player.html", "video-player.html")
        ]

        self.assertIn("hashlib.sha256", api)
        self.assertIn('"Clear-Site-Data": \'"cache"\'', api)
        self.assertIn('"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"', api)
        self.assertIn("location = /static/js/player.js", nginx)
        self.assertIn("location = /static/css/player.css", nginx)
        for template in templates:
            self.assertIn("/api/v1/media/refresh", template)
            self.assertIn("{{NETWORK_OBSERVATION_JS_URL}}", template)
        for template in templates[-2:]:
            self.assertIn("{{PLAYER_CSS_URL}}", template)
            self.assertIn("{{PLAYER_JS_URL}}", template)


if __name__ == "__main__":
    unittest.main()
