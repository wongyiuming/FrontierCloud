import asyncio
import unittest
from pathlib import Path

from app.api.v1 import media


ROOT = Path(__file__).resolve().parents[1]


class KaraokeUIContractTests(unittest.TestCase):
    def test_standalone_page_is_versioned_private_and_microphone_enabled(self):
        response = asyncio.run(media.get_karaoke_page())
        body = response.body.decode("utf-8")

        self.assertIn("/static/js/karaoke.js?v=", body)
        self.assertIn("/static/css/karaoke.css?v=", body)
        self.assertNotIn("KARAOKE_JS_URL", body)
        self.assertNotIn("KARAOKE_CSS_URL", body)
        self.assertEqual(
            response.headers["cache-control"],
            "no-store, no-cache, must-revalidate, max-age=0",
        )
        self.assertEqual(
            response.headers["permissions-policy"],
            "microphone=(self), camera=()",
        )

    def test_probe_uses_only_browser_local_audio_apis(self):
        script = (ROOT / "static" / "js" / "karaoke.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("navigator.mediaDevices.getUserMedia", script)
        self.assertIn("requestAudioStreamWithTimeout", script)
        self.assertIn("}, 15000)", script)
        self.assertIn("createMediaStreamSource(stream)", script)
        self.assertIn("monitorGainNode.connect(audioContext.destination)", script)
        self.assertIn("new MediaRecorder(", script)
        self.assertIn("const segmentDurationMs = 10000", script)
        self.assertIn("const maxClipCount = 3", script)
        self.assertIn("URL.createObjectURL(blob)", script)
        self.assertIn("stream.getTracks().forEach(track => track.stop())", script)
        self.assertNotIn("fetch(", script)
        self.assertNotIn("WebSocket", script)

    def test_page_exposes_diagnostics_safe_monitoring_and_local_accompaniment(self):
        template = (ROOT / "static" / "media" / "karaoke.html").read_text(
            encoding="utf-8"
        )
        index = (ROOT / "static" / "media" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="capabilities"', template)
        self.assertIn('id="diagnostics"', template)
        self.assertIn('id="toggleMonitor" disabled', template)
        self.assertIn('id="backingFile" type="file" accept="audio/*"', template)
        self.assertIn("所有录音只保存在当前网页内存", template)
        self.assertIn('href="/api/v1/media/karaoke"', index)


if __name__ == "__main__":
    unittest.main()
