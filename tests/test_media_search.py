import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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

    def test_admin_search_catalog_is_limited_to_the_selected_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            track = root / "music" / "黃耀明" / "暗湧.mp3"
            other_track = root / "music" / "其他" / "暗湧.mp3"
            lyric = root / "lyrics" / "暗湧.lrc"
            track.parent.mkdir(parents=True)
            other_track.parent.mkdir(parents=True)
            lyric.parent.mkdir()
            track.write_bytes(b"ID3")
            other_track.write_bytes(b"ID3")
            lyric.write_text("[00:01]暗湧", encoding="utf-8")
            with patch.object(media_manager, "MEDIA_ROOT", root):
                catalog = media_manager.MediaManager._search_catalog_sync(
                    "music/黃耀明",
                    track.parent,
                    {".mp3"},
                    {"music/黃耀明"},
                )

        self.assertEqual([item["path"] for item in catalog], ["music/黃耀明/暗湧.mp3"])
        self.assertTrue(catalog[0]["hidden"])

    def test_global_admin_search_scope_is_rejected(self):
        for scope in ("", "data/media", "unknown"):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                media_manager.MediaManager._search_scope(scope)


class MediaScopedSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_identity_contains_the_validated_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            scope = root / "music" / "黃耀明"
            scope.mkdir(parents=True)
            (scope / "暗湧.mp3").write_bytes(b"ID3")
            with (
                patch.object(media_manager, "MEDIA_ROOT", root),
                patch.object(media_manager.MediaManager, "hidden_paths", new=AsyncMock(return_value=set())),
                patch.object(media_manager, "load_media_catalog", new=AsyncMock(return_value=(7, None))) as load,
                patch.object(media_manager, "store_media_catalog", new=AsyncMock()) as store,
            ):
                result = await media_manager.MediaManager.search_tree("暗涌", "music/黃耀明")

        load.assert_awaited_once_with("search", "admin:music/黃耀明")
        self.assertEqual(store.await_args.args[:3], (7, "search", "admin:music/黃耀明"))
        self.assertEqual(result["path"], "music/黃耀明")
        self.assertEqual([item["path"] for item in result["items"]], ["music/黃耀明/暗湧.mp3"])


if __name__ == "__main__":
    unittest.main()
