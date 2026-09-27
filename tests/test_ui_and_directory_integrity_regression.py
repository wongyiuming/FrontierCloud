from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.services import lyrics_directory_counts
from app.services import media_directory_rename_integrity


ROOT = Path(__file__).resolve().parents[1]


class _Mappings:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _Mappings(self._rows)


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.execute = AsyncMock(side_effect=lambda *_args, **_kwargs: _Result(self.rows))


class DirectoryRenameReuseRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_deleted_directory_metadata_does_not_block_name_reuse(self):
        conn = _Connection([
            {"media_id": "directory-id", "object_kind": "directory", "media_path": "music/abc"},
        ])
        self.assertFalse(
            await media_directory_rename_integrity.target_metadata_conflict(conn, "music/abc")
        )

    async def test_real_media_at_target_still_blocks_rename(self):
        conn = _Connection([
            {"media_id": "track-id", "object_kind": "audio", "media_path": "music/abc/song.mp3"},
        ])
        self.assertTrue(
            await media_directory_rename_integrity.target_metadata_conflict(conn, "music/abc")
        )


class LyricDirectoryCountRegressionTests(unittest.TestCase):
    def test_counts_files_recursively_inside_each_folder(self):
        directories = [
            {"name": "A", "path": "music/A"},
            {"name": "B", "path": "music/B"},
        ]
        paths = [
            "music/A/one.mp3",
            "music/A/live/two.flac",
            "music/B/three.wav",
        ]
        self.assertEqual(
            lyrics_directory_counts._count_under(directories, paths),
            {"music/A": 2, "music/B": 1},
        )


class FrontendIntegrityContractTests(unittest.TestCase):
    def test_release_ui_uses_semantic_version_state(self):
        source = (ROOT / "static/js/release-version-ui.js").read_text(encoding="utf-8")
        page = (ROOT / "app/api/v1/admin_page_integrity.py").read_text(encoding="utf-8")
        self.assertIn("已与 main HEAD 一致", source)
        self.assertIn("→ 待发布", source)
        self.assertIn('static_asset_url("js/release-version-ui.js")', page)

    def test_lyric_directory_counts_are_visible_on_both_sides(self):
        source = (ROOT / "static/js/lyrics-directory-counts.js").read_text(encoding="utf-8")
        endpoints = (ROOT / "app/api/v1/endpoints.py").read_text(encoding="utf-8")
        self.assertIn("首曲目", source)
        self.assertIn("份歌词", source)
        self.assertIn("install_lyrics_directory_counts()", endpoints)

    def test_player_sidebar_uses_relative_path_not_retired_fixed_label(self):
        source = (ROOT / "static/js/player-directory-label.js").read_text(encoding="utf-8")
        integrity = (ROOT / "app/services/player_directory_label_integrity.py").read_text(encoding="utf-8")
        endpoints = (ROOT / "app/api/v1/endpoints.py").read_text(encoding="utf-8")
        self.assertIn("parts.slice(1)", source)
        self.assertIn("relativeParts.join('/')", source)
        self.assertIn("LEGACY_LABEL", integrity)
        self.assertIn("playerDirectoryLabel", integrity)
        self.assertIn("install_player_directory_label_integrity()", endpoints)


if __name__ == "__main__":
    unittest.main()
