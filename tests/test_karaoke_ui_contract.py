import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class KaraokeUiContractTests(unittest.TestCase):
    def test_karaoke_defaults_and_fullscreen_contract(self):
        template = (ROOT / "static/media/karaoke.html").read_text(encoding="utf-8")
        ui = (ROOT / "static/js/karaoke-ui.js").read_text(encoding="utf-8")

        self.assertIn('id="voiceValue">30%</output>', template)
        self.assertIn('id="monitorValue">150%</output>', template)
        self.assertIn("voiceGain.value = '30'", ui)
        self.assertIn("monitorGain.value = '150'", ui)
        self.assertIn("voiceValue.textContent = '30%'", ui)
        self.assertIn("monitorValue.textContent = '150%'", ui)
        self.assertIn('/static/js/karaoke-ui.js', template)

        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", ui)
        self.assertIn("state.lyrics.forEach", ui)
        self.assertIn("fullscreen-lyrics-column", ui)
        self.assertIn("fullscreen-lyric-line", ui)
        self.assertIn("container === overlayLines", ui)

    def test_local_recording_duration_is_stabilized(self):
        ui = (ROOT / "static/js/karaoke-ui.js").read_text(encoding="utf-8")

        self.assertIn("preview.currentSrc.startsWith('blob:')", ui)
        self.assertIn("Number.isFinite(preview.duration)", ui)
        self.assertIn("preview.currentTime = 1e101", ui)
        self.assertIn("loadedmetadata", ui)


if __name__ == "__main__":
    unittest.main()
