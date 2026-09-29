from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AudioPlaybackRollbackContractTests(unittest.TestCase):
    def setUp(self):
        self.audio_page = (ROOT / "static/media/audio-player.html").read_text(encoding="utf-8")
        self.video_page = (ROOT / "static/media/video-player.html").read_text(encoding="utf-8")
        self.network = (ROOT / "static/js/network-observation.js").read_text(encoding="utf-8")
        self.player = (ROOT / "static/js/player.js").read_text(encoding="utf-8")
        self.routing = (ROOT / "app/services/upload_site_routing.py").read_text(encoding="utf-8")
        self.upload = (ROOT / "app/api/v1/admin_master_mutation_integrity.py").read_text(encoding="utf-8")

    def test_mse_experiment_is_not_loaded_by_production_players(self):
        self.assertNotIn('/static/js/audio-continuous-stream.js', self.audio_page)
        self.assertNotIn('/static/js/audio-continuous-stream.js', self.video_page)
        self.assertNotIn('audio-continuous-stream.js', self.network)
        self.assertNotIn('continuous-stream-warning', self.audio_page)

    def test_audio_returns_to_native_duration_and_existing_preload_path(self):
        self.assertIn('get duration() { return Number.isFinite(this.video.duration)', self.player)
        self.assertIn('const PRELOAD_START_SECONDS = 5;', self.player)
        self.assertIn('checkAndPreloadNext(art.currentTime)', self.player)
        self.assertIn('void preloadMedia(nextPreload)', self.player)

    def test_upload_affinity_remains_enabled(self):
        self.assertIn('media_folder_path', self.routing)
        self.assertIn('media_folder_path(path) != folder', self.routing)
        self.assertIn('folder_affinity_member', self.routing)
        self.assertIn('该媒体文件夹绑定的存储节点当前不可写或容量不足', self.routing)
        self.assertIn('该媒体文件夹的历史资源已分散在多个存储节点', self.routing)
        self.assertIn('folder_path=folder_path', self.upload)
        self.assertIn('storage_write_lock', self.upload)


if __name__ == "__main__":
    unittest.main()
