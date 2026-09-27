from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services import media_directories, media_directory_catalog


class _Context:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return False


class _Database:
    def connect(self):
        return _Context()

    def begin(self):
        return _Context()


class DirectoryPathContractTests(unittest.TestCase):
    def test_only_real_media_directories_are_mutable(self):
        self.assertEqual(media_directories.normalize_directory_path("music/artist"), "music/artist")
        self.assertEqual(media_directories.normalize_directory_path("vido/category/sub"), "vido/category/sub")
        for invalid in ("music", "vido", "lyrics/a", "music/a/b/c", "../music/a", "music/.hidden"):
            with self.assertRaises(ValueError, msg=invalid):
                media_directories.normalize_directory_path(invalid)

    def test_rename_keeps_parent_and_validates_name(self):
        self.assertEqual(
            media_directories.renamed_path("music/artist/live", "即兴版"),
            "music/artist/即兴版",
        )
        self.assertEqual(
            media_directories.renamed_path("vido/category", "新分类"),
            "vido/新分类",
        )
        for invalid in ("", ".", "..", ".hidden", "a/b", "a\\b"):
            with self.assertRaises(ValueError, msg=invalid):
                media_directories.validate_directory_name(invalid)

    def test_physical_directory_rename_is_atomic_and_rejects_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "music" / "old"
            source.mkdir(parents=True)
            (source / "track.mp3").write_bytes(b"ID3")
            with patch.object(media_directories, "MEDIA_ROOT", root):
                media_directories._rename_physical_directory("music/old", "music/new")
                self.assertFalse(source.exists())
                self.assertTrue((root / "music" / "new" / "track.mp3").is_file())
                (root / "music" / "collision").mkdir()
                with self.assertRaises(FileExistsError):
                    media_directories._rename_physical_directory("music/new", "music/collision")


class DirectoryRenameRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_rename_moves_bytes_then_commits_metadata(self):
        database = _Database()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "music" / "old"
            source.mkdir(parents=True)
            (source / "track.mp3").write_bytes(b"ID3")
            rewrite = AsyncMock()
            with (
                patch.object(media_directories, "MEDIA_ROOT", root),
                patch.object(media_directories, "_target_metadata_conflict", new=AsyncMock(return_value=False)),
                patch.object(media_directories, "_rewrite_local_metadata", new=rewrite),
                patch.object(media_directories, "invalidate_media_catalog", new=AsyncMock()),
            ):
                result = await media_directories.rename_follower_directory(
                    "music/old", "music/new", database,
                )
            self.assertEqual(result["new_path"], "music/new")
            self.assertTrue((root / "music" / "new" / "track.mp3").is_file())
            rewrite.assert_awaited_once()

    async def test_local_rename_rolls_bytes_back_when_metadata_commit_fails(self):
        database = _Database()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "music" / "old"
            source.mkdir(parents=True)
            (source / "track.mp3").write_bytes(b"ID3")
            with (
                patch.object(media_directories, "MEDIA_ROOT", root),
                patch.object(media_directories, "_target_metadata_conflict", new=AsyncMock(return_value=False)),
                patch.object(
                    media_directories,
                    "_rewrite_local_metadata",
                    new=AsyncMock(side_effect=RuntimeError("db failed")),
                ),
            ):
                with self.assertRaises(RuntimeError):
                    await media_directories.rename_follower_directory(
                        "music/old", "music/new", database,
                    )
            self.assertTrue((root / "music" / "old" / "track.mp3").is_file())
            self.assertFalse((root / "music" / "new").exists())


class DirectoryPriorityContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_directory_sort_is_priority_then_name(self):
        entries = [{"name": "B"}, {"name": "A"}, {"name": "C"}]
        with patch.object(
            media_directories,
            "preferences_for_paths",
            new=AsyncMock(return_value={"music/B": 10, "music/C": -1}),
        ):
            result = await media_directories.sort_directory_entries(
                entries, lambda entry: f"music/{entry['name']}"
            )
        self.assertEqual([item["name"] for item in result], ["B", "A", "C"])

    async def test_priority_catalog_cache_hit_skips_mysql_sort(self):
        from app.api.v1 import media

        original_categories = media.get_media_categories
        original_subcategories = media.get_media_subcategories
        original_installed = getattr(media, "_directory_priority_installed", False)
        try:
            media._directory_priority_installed = False
            media_directory_catalog.install_public_priority()
            cached = [{"name": "cached", "url": "/cached"}]
            with (
                patch.object(media, "load_media_catalog", new=AsyncMock(return_value=(7, cached))),
                patch.object(media_directories, "sort_directory_entries", new=AsyncMock()) as sorter,
            ):
                result = await media.get_media_categories("music", media.AUDIO_EXTS)
            self.assertEqual(result, cached)
            sorter.assert_not_awaited()
        finally:
            media.get_media_categories = original_categories
            media.get_media_subcategories = original_subcategories
            media._directory_priority_installed = original_installed

    def test_priority_and_rename_are_wired_into_admin_and_public_catalog(self):
        root = Path(__file__).resolve().parents[1]
        api = (root / "app/api/v1/admin_directories.py").read_text(encoding="utf-8")
        service = (root / "app/services/media_directories.py").read_text(encoding="utf-8")
        catalog = (root / "app/services/media_directory_catalog.py").read_text(encoding="utf-8")
        client = (root / "static/js/directory-admin.js").read_text(encoding="utf-8")
        page = (root / "app/api/v1/admin_page_integrity.py").read_text(encoding="utf-8")
        internal = (root / "app/api/internal_media_control.py").read_text(encoding="utf-8")

        self.assertIn('@router.post("/directory-priority")', api)
        self.assertIn('@router.post("/directory/rename")', api)
        self.assertIn("object_kind='directory'", service)
        self.assertIn("media_playback_stats", service)
        self.assertIn("media.get_media_categories = prioritized_categories", catalog)
        self.assertIn("media.get_media_subcategories = prioritized_subcategories", catalog)
        self.assertIn("cached is not None", catalog)
        self.assertIn("store_media_catalog", catalog)
        self.assertIn("文件夹优先级", client)
        self.assertIn("renameDirectory", client)
        self.assertIn("/api/v1/media/admin/directory/rename", client)
        self.assertIn('static_asset_url("js/directory-admin.js")', page)
        self.assertIn('@router.post("/directory-rename")', internal)
        self.assertIn("Only the paired Master", internal)

    def test_master_rename_has_rollback_and_catalog_rewrite_contract(self):
        source = Path(__file__).resolve().parents[1].joinpath(
            "app/services/media_directories.py"
        ).read_text(encoding="utf-8")
        self.assertIn("_rewrite_master_catalog", source)
        self.assertIn("for row in reversed(moved)", source)
        self.assertIn("_remote_rename(row, new_path, old_path)", source)
        self.assertIn("目录存在进行中的上传", source)
        self.assertIn("目录仍有待删除媒体", source)


if __name__ == "__main__":
    unittest.main()
