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
        self.assertIn("const MIN_PREFERENCE = -2", script)
        self.assertIn("const MAX_PREFERENCE = 7", script)
        self.assertIn(">💔</button>", script)
        self.assertIn(">❤️</button>", script)
        self.assertIn("const playbackSessionId = {{PLAYBACK_SESSION_ID}}", audio_template)
        self.assertIn("const PLAYER_KIND = 'audio'", audio_template)
        self.assertIn("const PLAYER_KIND = 'video'", video_template)
        self.assertIn('id="playlistSearch"', audio_template)
        self.assertIn('id="playlistSearch"', video_template)
        self.assertNotIn("up_music", audio_template + video_template)
        self.assertNotIn("next_music", audio_template + video_template)
        self.assertIn("function playNext()", script)
        self.assertIn("function playPrev()", script)

    def test_audio_and_video_players_use_independent_templates(self):
        api = (ROOT / "app" / "api" / "v1" / "media.py").read_text(encoding="utf-8")

        self.assertIn('"audio-player.html"', api)
        self.assertIn('"video-player.html"', api)
        self.assertIn("html = load_html_template(player_template)", api)
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
        self.assertNotIn("单击下一首", template)
        self.assertNotIn("双击上一首", template)
        self.assertNotIn("拖动微调进度条", template)
        self.assertNotIn("点击跳转", template)
        self.assertIn('class="interaction-boundary"', template)
        self.assertIn("top: 75%", style)
        self.assertIn(".audio-player-page .artplayer-app .art-bottom { opacity: 1", style)

    def test_audio_player_offers_four_line_lrc_and_three_column_fullscreen_lyrics(self):
        player_script = (ROOT / "static" / "js" / "player.js").read_text(encoding="utf-8")
        lyric_script = (ROOT / "static" / "js" / "lyrics.js").read_text(encoding="utf-8")
        lyric_style = (ROOT / "static" / "css" / "lyrics.css").read_text(encoding="utf-8")
        player_style = (ROOT / "static" / "css" / "player.css").read_text(encoding="utf-8")
        audio_template = (ROOT / "static" / "media" / "audio-player.html").read_text(encoding="utf-8")
        lyric_template = (ROOT / "static" / "media" / "lyrics.html").read_text(encoding="utf-8")

        self.assertIn('id="lyricsLink"', audio_template)
        self.assertIn('id="inlineLyrics"', audio_template)
        self.assertIn('id="inlineLyricsLines"', audio_template)
        self.assertIn('id="inlineLyricsTrack"', audio_template)
        self.assertNotIn('id="lyricPrevious"', audio_template)
        self.assertNotIn('id="lyricCurrent"', audio_template)
        self.assertNotIn('id="audioDisk"', audio_template)
        self.assertNotIn('id="audioCover"', audio_template)
        self.assertIn("encodeURIComponent(media.media_path)", player_script)
        self.assertIn("/api/v1/media/lyrics/content?track=", player_script)
        self.assertIn("inlineLyricsRequest?.abort()", player_script)
        self.assertNotIn("audioDisk", player_script)
        self.assertIn(".sync-lyrics-track { width: 100%; height: 125%", player_style)
        self.assertIn("translateY(-20%)", player_script)
        self.assertIn("const LYRIC_SLIDE_MS = 480", player_script)
        self.assertIn("bottom: calc(25% + 32px)", player_style)
        self.assertIn("function lyricIndexAt(entries, currentTime)", player_script)
        self.assertIn("updateSynchronizedLyrics(art.currentTime)", player_script)
        self.assertIn("function startLyricClock()", player_script)
        self.assertIn("requestAnimationFrame(tick)", player_script)
        timeupdate_handler = player_script.split("art.on('video:timeupdate'", 1)[1].split("art.on('video:ended'", 1)[0]
        self.assertIn("startLyricClock()", timeupdate_handler)
        self.assertIn(".audio-player-page { flex-direction: row; }", player_style)
        self.assertIn("body {", player_style)
        self.assertIn("flex-direction: row-reverse", player_style)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", lyric_style)
        self.assertEqual(lyric_template.count('class="lyrics-column"'), 3)
        self.assertIn("lyricPalette[index % lyricPalette.length]", lyric_script)
        self.assertIn("--lyric-font-size", lyric_script)
        self.assertIn("{{LYRICS_JSON}}", lyric_template)
        self.assertNotIn("text-shadow", lyric_style)

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
        templates = [
            (ROOT / "static" / "media" / name).read_text(encoding="utf-8")
            for name in ("index.html", "category.html", "audio-player.html", "video-player.html")
        ]

        self.assertIn("hashlib.sha256", api)
        self.assertIn('"Clear-Site-Data": \'"cache"\'', api)
        self.assertIn('"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"', api)
        self.assertIn("/api/v1/media/refresh", templates[0])
        for template in templates[1:]:
            self.assertNotIn("/api/v1/media/refresh", template)
            self.assertNotIn("刷新界面", template)
        for template in templates:
            self.assertIn("{{NETWORK_OBSERVATION_JS_URL}}", template)
        for template in templates[-2:]:
            self.assertIn("{{PLAYER_CSS_URL}}", template)
            self.assertIn("{{PLAYER_JS_URL}}", template)

    def test_public_home_keeps_elevation_and_the_only_refresh_action(self):
        template = (ROOT / "static" / "media" / "index.html").read_text(encoding="utf-8")

        self.assertEqual(template.count('/api/v1/media/refresh'), 1)
        self.assertIn('id="elevate"', template)
        self.assertIn("/api/v1/media/admin/elevate", template)
        self.assertIn("提权", template)

    def test_android_portrait_layout_stacks_player_above_sidebar(self):
        style = (ROOT / "static" / "css" / "player.css").read_text(encoding="utf-8")

        self.assertIn("@media (max-width: 700px) and (orientation: portrait)", style)
        self.assertIn("flex-direction: column", style)
        self.assertIn("flex-basis: 60dvh", style)
        self.assertIn("min-width: 0", style)


if __name__ == "__main__":
    unittest.main()
