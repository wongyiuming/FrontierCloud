import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.api.v1 import media


class PublicMediaHierarchyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.media_root = Path(self.temp_dir.name) / "media"
        self.music_root = self.media_root / "music"
        self.video_root = self.media_root / "vido"
        self.music_root.mkdir(parents=True)
        self.video_root.mkdir(parents=True)
        self.patchers = [
            patch.object(media, "MEDIA_ROOT", self.media_root),
            patch.object(media, "MUSIC_ROOT", self.music_root),
            patch.object(media, "VIDEO_ROOT", self.video_root),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def _build_nested_music(self):
        artist = self.music_root / "黄耀明"
        album = artist / "舞台上的明哥"
        deeper = album / "不支持的第三层"
        other_album = artist / "另一张专辑"
        deeper.mkdir(parents=True)
        other_album.mkdir(parents=True)
        (album / "边走边唱.mp3").write_bytes(b"audio")
        (deeper / "不应展示.mp3").write_bytes(b"audio")
        (other_album / "另一首歌.mp3").write_bytes(b"audio")
        return artist, album

    def test_filesystem_hierarchy_is_preserved_in_public_catalog(self):
        artist, album = self._build_nested_music()

        categories = media._get_media_categories_sync("music", media.AUDIO_EXTS, set())
        subcategories = media._get_media_subcategories_sync(
            "music",
            artist.relative_to(self.media_root).as_posix(),
            media.AUDIO_EXTS,
            set(),
        )
        artist_tracks = media._scan_media_files_by_category_sync(
            artist.relative_to(self.media_root).as_posix(),
            media.AUDIO_EXTS,
            "audio",
            set(),
        )
        album_tracks = media._scan_media_files_by_category_sync(
            album.relative_to(self.media_root).as_posix(),
            media.AUDIO_EXTS,
            "audio",
            set(),
        )

        self.assertEqual([item["name"] for item in categories], ["黄耀明"])
        self.assertEqual(
            [item["name"] for item in subcategories],
            ["另一张专辑", "舞台上的明哥"],
        )
        self.assertEqual(artist_tracks, [])
        self.assertEqual([item["title"] for item in album_tracks], ["边走边唱"])

    async def test_first_level_route_renders_subfolders_before_player(self):
        artist, _ = self._build_nested_music()
        artist_path = artist.relative_to(self.media_root).as_posix()
        subcategories = media._get_media_subcategories_sync(
            "music",
            artist_path,
            media.AUDIO_EXTS,
            set(),
        )

        with patch.object(
            media,
            "get_media_subcategories",
            new=AsyncMock(return_value=subcategories),
        ):
            response = await media.get_music_player_page(artist_path)

        body = response.body.decode("utf-8")
        self.assertIn("舞台上的明哥", body)
        self.assertIn("另一张专辑", body)
        self.assertNotIn("边走边唱", body)

    async def test_second_level_route_plays_only_that_directory(self):
        artist, album = self._build_nested_music()
        album_path = album.relative_to(self.media_root).as_posix()
        media_list = media._scan_media_files_by_category_sync(
            album_path,
            media.AUDIO_EXTS,
            "audio",
            set(),
        )

        with (
            patch.object(
                media,
                "scan_media_files_by_category",
                new=AsyncMock(return_value=media_list),
            ),
            patch.object(
                media.playback,
                "attach_stats_and_sort",
                new=AsyncMock(return_value=media_list),
            ),
        ):
            response = await media.get_music_player_page(album_path)

        body = response.body.decode("utf-8")
        self.assertIn("边走边唱", body)
        self.assertNotIn("另一首歌", body)
        self.assertNotIn("不应展示", body)
        self.assertIn("path=music%2F%E9%BB%84%E8%80%80%E6%98%8E", body)


if __name__ == "__main__":
    unittest.main()
