from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api.v1 import admin_upload_guard
from app.services import lyrics, media_manager
from app.services import lyrics_hierarchy_integrity as integrity


class NestedLyricCatalogRegressionTests(unittest.TestCase):
    def test_nested_folder_lyrics_are_browsable_and_default_is_internal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            lyric_root = root / "lyrics"
            album = lyric_root / "歌手" / "专辑"
            album.mkdir(parents=True)
            (album / "歌曲.lrc").write_text("[00:01]歌词", encoding="utf-8")
            (lyric_root / "default.lrc").write_text("[00:00]fallback", encoding="utf-8")

            with patch.object(lyrics, "MEDIA_ROOT", root):
                root_items, root_dirs, root_total, _ = integrity._scan_catalog_scope_sync(
                    "lyrics", lyric_root, "lyric", "",
                )
                artist_items, artist_dirs, artist_total, _ = integrity._scan_catalog_scope_sync(
                    "lyrics/歌手", lyric_root / "歌手", "lyric", "",
                )
                album_items, album_dirs, album_total, _ = integrity._scan_catalog_scope_sync(
                    "lyrics/歌手/专辑", album, "lyric", "",
                )
                normalized, resolved = integrity._safe_file(
                    "lyrics/歌手/专辑/歌曲.lrc", "lyrics", lyrics.LYRIC_EXTS,
                )

            self.assertEqual(root_items, [])
            self.assertEqual(root_dirs, [{"name": "歌手", "path": "lyrics/歌手"}])
            self.assertEqual(root_total, 1)
            self.assertEqual(artist_items, [])
            self.assertEqual(artist_dirs, [{"name": "专辑", "path": "lyrics/歌手/专辑"}])
            self.assertEqual(artist_total, 1)
            self.assertEqual(album_dirs, [])
            self.assertEqual([item["path"] for item in album_items], ["lyrics/歌手/专辑/歌曲.lrc"])
            self.assertEqual(album_total, 1)
            self.assertEqual(normalized, "lyrics/歌手/专辑/歌曲.lrc")
            self.assertEqual(resolved, album / "歌曲.lrc")

    def test_admin_catalog_removes_system_fallback_from_counts_and_links(self):
        payload = {
            "counts": {"tracks": 2, "lyrics": 1, "relations": 2},
            "lyrics": [
                {"path": lyrics.DEFAULT_LYRIC_PATH, "name": "default"},
                {"path": "lyrics/album/song.lrc", "name": "song"},
            ],
            "relations": [
                {"track": "music/a/one.mp3", "lyric": lyrics.DEFAULT_LYRIC_PATH},
                {"track": "music/a/two.mp3", "lyric": "lyrics/album/song.lrc"},
            ],
            "tracks": [
                {"path": "music/a/one.mp3", "lyric_path": lyrics.DEFAULT_LYRIC_PATH},
                {"path": "music/a/two.mp3", "lyric_path": "lyrics/album/song.lrc"},
            ],
        }
        result = integrity._strip_system_fallback(payload, 1)
        self.assertEqual([item["path"] for item in result["lyrics"]], ["lyrics/album/song.lrc"])
        self.assertEqual(result["relations"], [
            {"track": "music/a/two.mp3", "lyric": "lyrics/album/song.lrc"},
        ])
        self.assertIsNone(result["tracks"][0]["lyric_path"])
        self.assertEqual(result["tracks"][1]["lyric_path"], "lyrics/album/song.lrc")
        self.assertEqual(result["counts"]["lyrics"], 1)
        self.assertEqual(result["counts"]["relations"], 1)

    def test_upload_depth_is_the_same_as_catalog_depth(self):
        self.assertEqual(
            integrity.validate_upload_relative_path("歌手/专辑/歌曲.lrc", "歌曲.lrc"),
            "歌手/专辑/歌曲.lrc",
        )
        self.assertEqual(
            integrity.validate_upload_relative_path("歌手/歌曲.lrc", "歌曲.lrc"),
            "歌手/歌曲.lrc",
        )
        self.assertEqual(
            integrity.validate_upload_relative_path(None, "歌曲.lrc"),
            "歌曲.lrc",
        )
        for invalid in (
            "歌手/专辑/碟1/歌曲.lrc",
            ".隐藏/歌曲.lrc",
            "歌手/.隐藏/歌曲.lrc",
        ):
            with self.assertRaises(HTTPException, msg=invalid) as raised:
                integrity.validate_upload_relative_path(invalid, "歌曲.lrc")
            self.assertEqual(raised.exception.status_code, 400)


class NestedLyricAdminSurfaceRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_tree_browses_nested_lyrics_and_never_lists_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            lyric_root = root / "lyrics"
            album = lyric_root / "artist" / "album"
            album.mkdir(parents=True)
            (album / "song.lrc").write_text("[00:01]line", encoding="utf-8")
            (lyric_root / "default.lrc").write_text("[00:00]fallback", encoding="utf-8")
            fallback = AsyncMock(side_effect=AssertionError("lyrics tree must not use the legacy flat scanner"))

            with (
                patch.object(lyrics, "MEDIA_ROOT", root),
                patch.object(media_manager, "MEDIA_ROOT", root),
            ):
                at_root = await integrity._list_lyric_tree("lyrics", fallback)
                at_artist = await integrity._list_lyric_tree("lyrics/artist", fallback)
                at_album = await integrity._list_lyric_tree("lyrics/artist/album", fallback)

            self.assertEqual([item["path"] for item in at_root["items"]], ["lyrics/artist"])
            self.assertEqual([item["path"] for item in at_artist["items"]], ["lyrics/artist/album"])
            self.assertEqual([item["path"] for item in at_album["items"]], ["lyrics/artist/album/song.lrc"])
            self.assertNotIn(lyrics.DEFAULT_LYRIC_PATH, {
                item["path"] for result in (at_root, at_artist, at_album) for item in result["items"]
            })

    async def test_search_and_collect_support_nested_lyrics_but_protect_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            lyric_root = root / "lyrics"
            album = lyric_root / "artist" / "album"
            album.mkdir(parents=True)
            song = album / "song.lrc"
            song.write_text("[00:01]line", encoding="utf-8")
            default = lyric_root / "default.lrc"
            default.write_text("[00:00]fallback", encoding="utf-8")

            original_scan = lambda *_args: (_ for _ in ()).throw(AssertionError("legacy lyric scan used"))
            original_collect = AsyncMock(side_effect=AssertionError("legacy lyric collect used"))
            with (
                patch.object(lyrics, "MEDIA_ROOT", root),
                patch.object(media_manager, "MEDIA_ROOT", root),
            ):
                catalog = integrity._lyric_search_catalog_sync(
                    "lyrics", lyric_root, {".lrc"}, set(), original_scan,
                )
                selected = await integrity._collect_with_nested_lyrics(
                    ["lyrics/artist", "lyrics/artist/album/song.lrc"], original_collect,
                )
                for protected in ("lyrics", lyrics.DEFAULT_LYRIC_PATH):
                    with self.assertRaises(HTTPException, msg=protected):
                        await integrity._collect_with_nested_lyrics([protected], original_collect)

            self.assertEqual([item["path"] for item in catalog], ["lyrics/artist/album/song.lrc"])
            self.assertEqual([path for path, _target in selected], [
                "lyrics/artist", "lyrics/artist/album/song.lrc",
            ])


class LyricUploadRegistrationRegressionTests(unittest.TestCase):
    # Publication/audit atomicity, rollback, cancellation and cache failure now
    # use real SQLite and files in test_lyric_publication, replacing old mocks.
    def test_endpoint_override_and_integrity_install_are_mandatory(self):
        root = Path(__file__).resolve().parents[1]
        endpoints = (root / "app/api/v1/endpoints.py").read_text(encoding="utf-8")
        integrity_source = (root / "app/services/lyrics_hierarchy_integrity.py").read_text(encoding="utf-8")
        self.assertIn('"/upload/lyric"', endpoints)
        self.assertIn("install_lyrics_hierarchy_integrity()", endpoints)
        self.assertIn("MediaManager.list_tree = staticmethod(list_tree)", integrity_source)
        self.assertIn("MediaManager._collect = staticmethod(collect)", integrity_source)
        self.assertIn("MediaManager.upload_lyric = staticmethod(upload_lyric)", integrity_source)


if __name__ == "__main__":
    unittest.main()
