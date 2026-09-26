import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class KaraokeUiContractTests(unittest.TestCase):
    def test_karaoke_defaults_and_fullscreen_contract(self):
        template = (ROOT / "static/media/karaoke.html").read_text(encoding="utf-8")
        ui = (ROOT / "static/js/karaoke-ui.js").read_text(encoding="utf-8")
        shared = (ROOT / "static/js/lyrics-window.js").read_text(encoding="utf-8")

        self.assertIn('id="voiceValue">30%</output>', template)
        self.assertIn('id="monitorValue">150%</output>', template)
        self.assertIn("voiceGain.value = '30'", ui)
        self.assertIn("monitorGain.value = '150'", ui)
        self.assertIn("voiceValue.textContent = '30%'", ui)
        self.assertIn("monitorValue.textContent = '150%'", ui)
        self.assertIn('/static/js/karaoke-ui.js', template)

        self.assertIn("const OFFSETS = Object.freeze([-3, -2, -1, 0, 1, 2, 3])", shared)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", shared)
        self.assertIn("renderFullscreenColumns", shared)
        self.assertIn("syncFullscreenLyrics", shared)
        self.assertIn("window.renderFullscreenLyrics = function renderSharedPlayerFullscreenLyrics", shared)
        self.assertIn("const lyricUi = window.FrontierLyricsUI", ui)
        self.assertIn("lyricUi.renderWindow", ui)
        self.assertIn("lyricUi.renderFullscreenColumns", ui)
        self.assertIn("lyricUi.syncFullscreenLyrics", ui)
        self.assertIn("container === overlayLines", ui)
        self.assertIn('class="fullscreen-lyrics hidden"', template)
        self.assertEqual(template.count('class="fullscreen-lyrics-column"'), 3)
        self.assertNotIn('class="overlay"', template)
        self.assertNotIn("grid-template-columns: repeat(3, minmax(0, 1fr))", ui)
        self.assertNotIn("fullscreen-lyric-line:nth-child", ui)

    def test_local_recording_duration_is_stabilized(self):
        ui = (ROOT / "static/js/karaoke-ui.js").read_text(encoding="utf-8")

        self.assertIn("preview.currentSrc.startsWith('blob:')", ui)
        self.assertIn("Number.isFinite(preview.duration)", ui)
        self.assertIn("preview.currentTime = 1e101", ui)
        self.assertIn("loadedmetadata", ui)


if __name__ == "__main__":
    unittest.main()
