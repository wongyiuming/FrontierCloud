import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api.v1 import media
from app.services import media_manager, media_search


class MediaSearchTests(unittest.TestCase):
    def test_simplified_traditional_and_pinyin_share_one_index(self):
        search_text = media_search.build_search_text(
            "暗湧",
            "music/黃耀明/人山人海/暗湧.mp3",
        )
        for query in ("暗涌", "暗湧", "anyong", "huangyaoming", "人山人海"):
            with self.subTest(query=query):
                self.assertTrue(
                    media_search.matches_search(
                        search_text,
                        media_search.normalized_query(query),
                    )
                )

    def test_query_is_bounded_and_must_contain_searchable_text(self):
        with self.assertRaises(ValueError):
            media_search.normalized_query("---")
        with self.assertRaises(ValueError):
            media_search.normalized_query("x" * 101)

    def test_public_search_catalog_hides_hidden_media_and_includes_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            music = root / "music"
            video = root / "vido"
            (music / "黃耀明" / "人山人海").mkdir(parents=True)
            (music / "黃耀明" / "人山人海" / "暗湧.mp3").write_bytes(b"ID3")
            (music / "隐藏").mkdir()
            (music / "隐藏" / "秘密.mp3").write_bytes(b"ID3")
            video.mkdir()
            with (
                patch.object(media, "MEDIA_ROOT", root),
                patch.object(media, "MUSIC_ROOT", music),
                patch.object(media, "VIDEO_ROOT", video),
            ):
                catalog = media._scan_public_search_catalog_sync({"music/隐藏"})

        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]["display_path"], "/黃耀明/人山人海/暗湧.mp3")
        self.assertIn("track=music%2F%E9%BB%83", catalog[0]["open_url"])
        self.assertTrue(media_search.matches_search(
            catalog[0]["search_text"], media_search.normalized_query("anyong")
        ))

    def test_admin_search_catalog_includes_hidden_and_lyric_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            track = root / "music" / "黃耀明" / "暗湧.mp3"
            lyric = root / "lyrics" / "暗湧.lrc"
            track.parent.mkdir(parents=True)
            lyric.parent.mkdir()
            track.write_bytes(b"ID3")
            lyric.write_text("[00:01]暗湧", encoding="utf-8")
            with patch.object(media_manager, "MEDIA_ROOT", root):
                catalog = media_manager.MediaManager._search_catalog_sync({"music/黃耀明"})

        self.assertEqual({item["path"] for item in catalog}, {
            "music/黃耀明/暗湧.mp3",
            "lyrics/暗湧.lrc",
        })
        track_item = next(item for item in catalog if item["path"].startswith("music/"))
        self.assertTrue(track_item["hidden"])


if __name__ == "__main__":
    unittest.main()
