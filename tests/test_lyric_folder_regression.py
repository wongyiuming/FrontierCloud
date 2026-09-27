from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile
from starlette.requests import Request

from app.api.v1 import admin_upload_guard
from app.services import lyrics
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


class LyricUploadRegistrationRegressionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def request() -> Request:
        return Request({
            "type": "http",
            "method": "POST",
            "path": "/api/v1/media/admin/upload/lyric",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "scheme": "https",
            "server": ("test", 443),
        })

    async def test_success_is_returned_only_after_managed_object_registration(self):
        upload = UploadFile(filename="song.lrc", file=io.BytesIO(b"[00:01]line"))
        audit = AsyncMock()
        with (
            patch.object(
                admin_upload_guard.MediaManager,
                "upload_lyric",
                new=AsyncMock(return_value="lyrics/artist/album/song.lrc"),
            ),
            patch.object(
                admin_upload_guard.media_objects,
                "ensure_objects",
                new=AsyncMock(return_value={"lyrics/artist/album/song.lrc": "lyric-id"}),
            ) as register,
            patch.object(admin_upload_guard, "invalidate_media_catalog", new=AsyncMock()),
            patch.object(admin_upload_guard.legacy_admin, "_mutation_audit", return_value=audit),
        ):
            result = await admin_upload_guard.upload_lyric(
                self.request(), upload, "artist/album/song.lrc", "session",
            )

        self.assertEqual(result, {"path": "lyrics/artist/album/song.lrc"})
        register.assert_awaited_once_with([("lyrics/artist/album/song.lrc", "lyric")])
        self.assertEqual(audit.await_args_list[-1].args[1], "success")

    async def test_registration_failure_rolls_back_the_published_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            lyric_root = root / "lyrics"
            target = lyric_root / "artist" / "album" / "song.lrc"
            target.parent.mkdir(parents=True)
            target.write_text("[00:01]line", encoding="utf-8")
            upload = UploadFile(filename="song.lrc", file=io.BytesIO(b"[00:01]line"))
            audit = AsyncMock()
            with (
                patch.object(admin_upload_guard, "MEDIA_ROOT", root),
                patch.object(admin_upload_guard, "LYRICS_ROOT", lyric_root),
                patch.object(
                    admin_upload_guard.MediaManager,
                    "upload_lyric",
                    new=AsyncMock(return_value="lyrics/artist/album/song.lrc"),
                ),
                patch.object(
                    admin_upload_guard.media_objects,
                    "ensure_objects",
                    new=AsyncMock(side_effect=RuntimeError("db failed")),
                ),
                patch.object(admin_upload_guard, "invalidate_media_catalog", new=AsyncMock()),
                patch.object(admin_upload_guard.legacy_admin, "_mutation_audit", return_value=audit),
            ):
                with self.assertRaises(HTTPException) as raised:
                    await admin_upload_guard.upload_lyric(
                        self.request(), upload, "artist/album/song.lrc", "session",
                    )

            self.assertEqual(raised.exception.status_code, 500)
            self.assertFalse(target.exists())
            self.assertFalse((lyric_root / "artist").exists())
            self.assertEqual(audit.await_args_list[-1].args[1], "failed")

    def test_endpoint_override_and_integrity_install_are_mandatory(self):
        root = Path(__file__).resolve().parents[1]
        endpoints = (root / "app/api/v1/endpoints.py").read_text(encoding="utf-8")
        self.assertIn('"/upload/lyric"', endpoints)
        self.assertIn("install_lyrics_hierarchy_integrity()", endpoints)


if __name__ == "__main__":
    unittest.main()
