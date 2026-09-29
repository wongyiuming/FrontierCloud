from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AudioContinuousStreamContractTests(unittest.TestCase):
    def setUp(self):
        self.core = (ROOT / "static/js/audio-continuous-stream.js").read_text(encoding="utf-8")
        self.runtime = (ROOT / "static/js/player-directory-label.js").read_text(encoding="utf-8")
        self.audio_page = (ROOT / "static/media/audio-player.html").read_text(encoding="utf-8")
        self.video_page = (ROOT / "static/media/video-player.html").read_text(encoding="utf-8")
        self.network = (ROOT / "static/js/network-observation.js").read_text(encoding="utf-8")
        self.player_integrity = (ROOT / "app/services/player_directory_label_integrity.py").read_text(encoding="utf-8")
        self.routing = (ROOT / "app/services/upload_site_routing.py").read_text(encoding="utf-8")
        self.upload = (ROOT / "app/api/v1/admin_master_mutation_integrity.py").read_text(encoding="utf-8")

    def test_old_pre_end_handoff_is_not_loaded_or_reintroduced(self):
        forbidden = (
            "HANDOFF_SECONDS",
            "pre-end-200ms",
            "playback-continuity-handoff.js",
            "frontierCloudPlaybackContinuityHandoff",
            "standby.play()",
            "bridge_active",
        )
        for token in forbidden:
            self.assertNotIn(token, self.core)
            self.assertNotIn(token, self.network)
            self.assertNotIn(token, self.audio_page)

    def test_continuous_stream_is_audio_only(self):
        self.assertIn('/static/js/audio-continuous-stream.js', self.audio_page)
        self.assertNotIn('/static/js/audio-continuous-stream.js', self.video_page)
        self.assertIn("PLAYER_KIND !== 'audio'", self.core)

    def test_continuous_stream_asset_is_content_versioned_at_render_time(self):
        self.assertIn('LEGACY_CONTINUOUS_STREAM_SCRIPT', self.player_integrity)
        self.assertIn('filename == "audio-player.html"', self.player_integrity)
        self.assertIn('static_asset_url("js/audio-continuous-stream.js")', self.player_integrity)
        self.assertIn("continuous_stream_url", self.player_integrity)

    def test_mse_contract_is_one_mpeg_sequence_without_end_of_stream(self):
        self.assertIn("const MIME = 'audio/mpeg';", self.core)
        self.assertIn("sourceBuffer.mode = 'sequence';", self.core)
        self.assertIn("const LOOKAHEAD_TRACKS = 2;", self.core)
        self.assertIn("new MediaSource()", self.core)
        self.assertIn("addSourceBuffer(MIME)", self.core)
        self.assertNotIn("endOfStream(", self.core)
        self.assertNotIn("document.createElement('audio')", self.core)
        self.assertNotIn('document.createElement("audio")', self.core)

    def test_track_duration_is_latched_and_never_reads_mse_timeline_for_ui(self):
        self.assertIn("presentationDuration: 0", self.core)
        self.assertIn("function latchPresentationDuration(segment, value)", self.core)
        self.assertIn("if (current > 0) return current;", self.core)
        self.assertIn("return presentationDuration(this.activeSegment);", self.core)
        self.assertIn("latchPresentationDuration(segment, estimate)", self.core)
        self.assertIn("latchPresentationDuration(segment, segment.duration)", self.core)
        self.assertIn("FrontierAudioPlayer.prototype._syncTime = function continuousSyncTime()", self.core)
        self.assertIn("session.syncTimeUi(this)", self.core)
        self.assertIn("presentation_duration: session?.localDuration() || 0", self.core)
        self.assertNotIn("Math.max(this.activeSegment.start, this.bufferedEnd())", self.core)

    def test_mse_append_is_batched_instead_of_one_update_per_network_chunk(self):
        self.assertIn("const FIRST_APPEND_BYTES = 64 * 1024;", self.core)
        self.assertIn("const APPEND_BATCH_BYTES = 512 * 1024;", self.core)
        self.assertIn("const MAX_BUFFER_AHEAD_SECONDS = 30;", self.core)
        self.assertIn("pending.push(value)", self.core)
        self.assertIn("if (pendingBytes >= threshold) await flush();", self.core)
        self.assertNotIn("await this.appendBytes(value);", self.core)
        self.assertNotIn("cache: 'no-store'", self.core)

    def test_source_buffer_quota_is_backpressured_and_retried_silently(self):
        self.assertIn("this.bufferedAhead() < MAX_BUFFER_AHEAD_SECONDS", self.core)
        self.assertIn("error?.name !== 'QuotaExceededError'", self.core)
        self.assertIn("await this.waitForAppendCapacity(true)", self.core)
        self.assertIn("await this.appendBufferOnce(chunk)", self.core)
        self.assertIn("quota_wait_count", self.core)
        self.assertNotIn("QuotaExceededError') this.markRuntimeSkip", self.core)

    def test_transient_media_reads_retry_and_resume_without_changing_playlist_semantics(self):
        self.assertIn("installContinuousAudioFetchRetry", self.runtime)
        self.assertIn("url.pathname === '/api/v1/media/stream'", self.runtime)
        self.assertIn("headers.set('Range', `bytes=${offset}-`)", self.runtime)
        self.assertIn("response.status === 206 && range?.start === offset", self.runtime)
        self.assertIn("while (generationIsCurrent(generation)", self.runtime)
        self.assertIn("RETRY_MAX_MS = 3000", self.runtime)
        self.assertIn("resume_count", self.runtime)
        self.assertNotIn("连续流读取失败", self.runtime)
        self.assertNotIn("自动续播跳过", self.runtime)

    def test_all_mp3_catalog_avoids_redundant_playlist_dom_scan(self):
        self.assertIn("const hasWarning = entries.some", self.core)
        self.assertIn("if (!hasWarning && !existing) return;", self.core)
        self.assertNotIn("renderPlaylist = function renderContinuousAudioPlaylist", self.core)
        self.assertEqual(self.core.count("decoratePlaylist();"), 2)

    def test_only_mp3_is_auto_continuous_and_other_audio_is_visible_as_skipped(self):
        self.assertIn("mediaPath(media).endsWith('.mp3')", self.core)
        self.assertIn("连续流不兼容 · 自动续播跳过", self.core)
        self.assertIn("该曲目仅支持单曲播放，自动续播将跳过", self.core)
        self.assertIn("continuous-stream-warning", self.audio_page)

    def test_continuous_stream_has_bounded_track_window_and_no_server_cache_contract(self):
        self.assertIn("const LOOKAHEAD_TRACKS = 2;", self.core)
        self.assertIn("MAX_TRACK_BYTES", self.core)
        self.assertIn("sourceBuffer.remove(0, removeEnd)", self.core)
        self.assertIn("fetch(media.url", self.core)
        self.assertNotIn("/api/v1/media/continuous", self.core)
        self.assertNotIn("transcode", self.core.lower())

    def test_upload_affinity_is_immediate_parent_and_cannot_spill(self):
        self.assertIn("media_folder_path", self.routing)
        self.assertIn("media_folder_path(path) != folder", self.routing)
        self.assertIn("folder_affinity_member", self.routing)
        self.assertIn("该媒体文件夹绑定的存储节点当前不可写或容量不足", self.routing)
        self.assertIn("该媒体文件夹的历史资源已分散在多个存储节点", self.routing)
        self.assertIn("folder_path=folder_path", self.upload)
        self.assertIn("storage_write_lock", self.upload)


if __name__ == "__main__":
    unittest.main()
