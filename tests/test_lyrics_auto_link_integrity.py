from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import lyrics
from app.services import lyrics_auto_link_integrity as auto_link


class _Begin:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, _kind, _value, _traceback):
        return False


class _Connection:
    def __init__(self, existing: dict[str, str | None]):
        self.existing = existing

    async def scalar(self, _statement, params=None):
        return self.existing.get(str((params or {}).get("media_id")))


class _Engine:
    def __init__(self, conn):
        self.conn = conn

    def begin(self):
        return _Begin(self.conn)


class LyricAutoLinkBoundaryTests(unittest.TestCase):
    def test_disk_scan_only_accepts_supported_managed_hierarchy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            music = root / "music"
            lyric_root = root / "lyrics"
            (music / "artist" / "album").mkdir(parents=True)
            (music / "artist" / "album" / "too-deep").mkdir(parents=True)
            (lyric_root / "artist" / "album").mkdir(parents=True)
            (lyric_root / "artist" / "album" / "too-deep").mkdir(parents=True)
            (music / "artist" / "song.mp3").write_bytes(b"ID3")
            (music / "artist" / "album" / "song.flac").write_bytes(b"fLaC")
            (music / "artist" / "album" / "too-deep" / "song.wav").write_bytes(b"RIFF")
            (lyric_root / "song.lrc").write_text("[00:01]root", encoding="utf-8")
            (lyric_root / "artist" / "song.lrc").write_text("[00:01]artist", encoding="utf-8")
            (lyric_root / "artist" / "album" / "song.lrc").write_text("[00:01]album", encoding="utf-8")
            (lyric_root / "artist" / "album" / "too-deep" / "song.lrc").write_text("[00:01]deep", encoding="utf-8")
            (lyric_root / "default.lrc").write_text("[00:00]fallback", encoding="utf-8")

            with (
                patch.object(lyrics, "MEDIA_ROOT", root),
                patch.object(lyrics, "MUSIC_ROOT", music),
                patch.object(lyrics, "LYRICS_ROOT", lyric_root),
            ):
                tracks = auto_link._supported_local_track_paths()
                lyric_paths = auto_link._supported_lyric_paths()

        self.assertEqual(tracks, [
            "music/artist/album/song.flac",
            "music/artist/song.mp3",
        ])
        self.assertEqual(lyric_paths, [
            "lyrics/artist/album/song.lrc",
            "lyrics/artist/song.lrc",
            "lyrics/song.lrc",
        ])
        self.assertNotIn(lyrics.DEFAULT_LYRIC_PATH, lyric_paths)


class LyricAutoLinkRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_link_preserves_manual_relation_and_only_replaces_fallback(self):
        conn = _Connection({
            "manual-id": "lyrics/manual-choice.lrc",
            "fallback-id": lyrics.DEFAULT_LYRIC_PATH,
        })
        audit = AsyncMock()
        ensure_object = AsyncMock(return_value="new-lyric-id")
        upsert = AsyncMock()

        async def identity(_conn, track_path):
            if track_path.endswith("manual.mp3"):
                return track_path, "manual-id"
            return track_path, "fallback-id"

        with (
            patch.object(auto_link, "node_state", SimpleNamespace(node={"role": "Standalone"})),
            patch.object(auto_link, "_supported_local_track_paths", return_value=[
                "music/a/manual.mp3", "music/b/fallback.mp3",
            ]),
            patch.object(auto_link, "_supported_lyric_paths", return_value=[
                "lyrics/a/manual.lrc", "lyrics/b/fallback.lrc",
            ]),
            patch.object(lyrics, "engine", _Engine(conn)),
            patch.object(lyrics, "ensure_default_lyric_file", return_value=(lyrics.DEFAULT_LYRIC_PATH, Path("default.lrc"))),
            patch.object(lyrics, "_track_identity", new=AsyncMock(side_effect=identity)),
            patch.object(lyrics.media_objects, "ensure_object", new=ensure_object),
            patch.object(lyrics, "_upsert_relation", new=upsert),
            patch("app.services.media_manager.ensure_media_mutations_ready", return_value=None),
        ):
            result = await auto_link.auto_relate_matching_names(audit=audit)

        self.assertEqual(result, {
            "linked": 1,
            "preserved": 1,
            "ambiguous": 0,
            "unmatched": 0,
        })
        ensure_object.assert_awaited_once_with(conn, "lyrics/b/fallback.lrc", "lyric")
        self.assertEqual(upsert.await_count, 1)
        self.assertEqual(upsert.await_args.args[1:5], (
            "fallback-id",
            "music/b/fallback.mp3",
            "new-lyric-id",
            "lyrics/b/fallback.lrc",
        ))
        self.assertEqual(audit.await_args.args[1], "success")
        self.assertEqual(audit.await_args.args[2], 1)
        self.assertEqual(audit.await_args.args[3]["preserved"], 1)

    async def test_missing_manual_lyric_file_is_still_not_overwritten(self):
        conn = _Connection({"manual-id": "lyrics/missing-manual-choice.lrc"})

        async def identity(_conn, track_path):
            return track_path, "manual-id"

        ensure_object = AsyncMock()
        upsert = AsyncMock()
        with (
            patch.object(auto_link, "node_state", SimpleNamespace(node={"role": "Standalone"})),
            patch.object(auto_link, "_supported_local_track_paths", return_value=["music/a/manual.mp3"]),
            patch.object(auto_link, "_supported_lyric_paths", return_value=["lyrics/a/manual.lrc"]),
            patch.object(lyrics, "engine", _Engine(conn)),
            patch.object(lyrics, "ensure_default_lyric_file", return_value=(lyrics.DEFAULT_LYRIC_PATH, Path("default.lrc"))),
            patch.object(lyrics, "_track_identity", new=AsyncMock(side_effect=identity)),
            patch.object(lyrics.media_objects, "ensure_object", new=ensure_object),
            patch.object(lyrics, "_upsert_relation", new=upsert),
            patch("app.services.media_manager.ensure_media_mutations_ready", return_value=None),
        ):
            result = await auto_link.auto_relate_matching_names()

        self.assertEqual(result["preserved"], 1)
        self.assertEqual(result["linked"], 0)
        ensure_object.assert_not_awaited()
        upsert.assert_not_awaited()

    def test_install_replaces_bulk_auto_link_at_module_boundary(self):
        root = Path(__file__).resolve().parents[1]
        endpoints = (root / "app/api/v1/endpoints.py").read_text(encoding="utf-8")
        module = (root / "app/services/lyrics_auto_link_integrity.py").read_text(encoding="utf-8")
        self.assertIn("install_lyrics_auto_link_integrity()", endpoints)
        self.assertIn("lyrics.auto_relate_matching_names = auto_relate_matching_names", module)
        self.assertIn("preserved", module)
        self.assertIn("LYRIC_FILE_DEPTHS", module)
        self.assertIn("str(current) != lyrics.DEFAULT_LYRIC_PATH", module)


if __name__ == "__main__":
    unittest.main()
