import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LyricsUiContractTests(unittest.TestCase):
    def test_audio_and_karaoke_share_seven_line_renderer(self):
        shared = (ROOT / "static/js/lyrics-window.js").read_text(encoding="utf-8")
        audio = (ROOT / "static/media/audio-player.html").read_text(encoding="utf-8")
        karaoke = (ROOT / "static/media/karaoke.html").read_text(encoding="utf-8")
        karaoke_ui = (ROOT / "static/js/karaoke-ui.js").read_text(encoding="utf-8")

        self.assertIn("[-3, -2, -1, 0, 1, 2, 3]", shared)
        self.assertIn("renderWindow", shared)
        self.assertIn("renderFullscreenColumns", shared)
        self.assertIn("window.renderFullscreenLyrics = function renderSharedPlayerFullscreenLyrics", shared)
        self.assertLess(audio.index("{{PLAYER_JS_URL}}"), audio.index("/static/js/lyrics-window.js"))
        self.assertLess(karaoke.index("{{KARAOKE_JS_URL}}"), karaoke.index("/static/js/lyrics-window.js"))
        self.assertLess(karaoke.index("/static/js/lyrics-window.js"), karaoke.index("/static/js/karaoke-ui.js"))
        self.assertIn("FrontierLyricsUI", karaoke_ui)
        self.assertIn("frontier-karaoke-lyrics", karaoke_ui)
        self.assertIn("lyricUi.renderFullscreenColumns", karaoke_ui)
        self.assertIn('class="fullscreen-lyrics hidden"', audio)
        self.assertIn('class="fullscreen-lyrics hidden"', karaoke)
        self.assertEqual(audio.count('class="fullscreen-lyrics-column"'), 3)
        self.assertEqual(karaoke.count('class="fullscreen-lyrics-column"'), 3)
        self.assertNotIn("#overlayLines .fullscreen-lyric-line", karaoke_ui)

    def test_default_lyric_is_runtime_generated_and_startup_backfilled(self):
        lyrics = (ROOT / "app/services/lyrics.py").read_text(encoding="utf-8")
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        deletion = (ROOT / "app/api/v1/admin_delete_integrity.py").read_text(encoding="utf-8")

        self.assertIn("DEFAULT_LYRIC_PATH = \"lyrics/default.lrc\"", lyrics)
        self.assertIn("建设中，暂无歌词", lyrics)
        self.assertIn("ensure_default_lyric_file", lyrics)
        self.assertIn("initialize_default_lyrics", main)
        self.assertIn("系统默认歌词为保留对象，不能删除", deletion)


if __name__ == "__main__":
    unittest.main()
