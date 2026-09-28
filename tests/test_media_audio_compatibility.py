import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.services import media_audio_compatibility as compatibility


class MediaAudioCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        compatibility._decision_cache.clear()

    @staticmethod
    def _make_video(path: Path, audio_codec: str) -> None:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=32x32:r=1",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", "0.5", "-c:v", "mpeg4", "-c:a", audio_codec, str(path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    async def test_mp4_ac3_audio_is_transcoded_to_browser_safe_aac(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "movie.mp4"
            self._make_video(source, "ac3")
            self.assertEqual(await compatibility.probe_audio_codecs(source), ("ac3",))

            playback = await compatibility.browser_compatible_video(source)

            self.assertNotEqual(playback, source)
            self.assertTrue(playback.is_file())
            self.assertEqual(await compatibility.probe_audio_codecs(playback), ("aac",))
            mtime_ns = playback.stat().st_mtime_ns
            self.assertEqual(await compatibility.browser_compatible_video(source), playback)
            self.assertEqual(playback.stat().st_mtime_ns, mtime_ns)

    async def test_native_aac_audio_keeps_original_object(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "movie.mp4"
            self._make_video(source, "aac")

            playback = await compatibility.browser_compatible_video(source)

            self.assertEqual(playback, source.resolve())
            cache_root = Path(directory) / compatibility.CACHE_DIRECTORY_NAME
            self.assertEqual(list(cache_root.glob("*.mp4")), [])

    def test_transcode_only_reencodes_audio_and_preserves_other_streams(self):
        command = compatibility._transcode_command(Path("movie.mp4"), Path("playback.mp4"))
        map_index = command.index("-map")
        codec_index = command.index("-c")
        self.assertEqual(command[map_index + 1], "0")
        self.assertEqual(command[codec_index + 1], "copy")
        self.assertNotIn("-sn", command)
        self.assertNotIn("-dn", command)
        self.assertIn("-c:a", command)
        self.assertIn("aac", command)


class MediaAudioCompatibilityContractTests(unittest.TestCase):
    def test_runtime_installs_ffmpeg_and_playback_integrity_layer(self):
        root = Path(__file__).resolve().parents[1]
        endpoints = (root / "app" / "api" / "v1" / "endpoints.py").read_text(encoding="utf-8")
        self.assertIsNotNone(shutil.which("ffmpeg"))
        self.assertIsNotNone(shutil.which("ffprobe"))
        self.assertIn("install_media_audio_compatibility()", endpoints)


if __name__ == "__main__":
    unittest.main()
