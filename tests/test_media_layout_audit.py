import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.services import media_manager


class _CountingScandir:
    def __init__(self, iterator, counter):
        self.iterator = iterator
        self.counter = counter

    def __enter__(self):
        self.iterator.__enter__()
        return self

    def __exit__(self, *args):
        return self.iterator.__exit__(*args)

    def __iter__(self):
        for entry in self.iterator:
            self.counter[0] += 1
            yield entry


class MediaLayoutAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        media_manager._LAYOUT_CACHE.clear()

    def tearDown(self):
        media_manager._LAYOUT_CACHE.clear()

    def test_repeated_flat_upload_checks_do_not_rescan_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            category = root / "music" / "album"
            category.mkdir(parents=True)
            for index in range(1000):
                (category / f"existing-{index}.mp3").write_bytes(b"ID3")
            real_scandir = os.scandir
            visits = [0]

            def counted(path):
                return _CountingScandir(real_scandir(path), visits)

            with patch.object(media_manager, "MEDIA_ROOT", root), patch.object(media_manager.os, "scandir", counted):
                for index in range(50):
                    media_manager.MediaManager._validate_media_destination(category / f"new-{index}.mp3")

            self.assertLessEqual(visits[0], 1000)

    def test_flat_layout_cache_detects_media_added_inside_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            category = root / "music" / "album"
            child = category / "disc"
            child.mkdir(parents=True)
            with patch.object(media_manager, "MEDIA_ROOT", root):
                media_manager.MediaManager._validate_media_destination(category / "flat.mp3")
                (child / "nested.mp3").write_bytes(b"ID3")
                with self.assertRaises(HTTPException) as raised:
                    media_manager.MediaManager._validate_media_destination(category / "flat.mp3")
            self.assertEqual(raised.exception.status_code, 409)

    async def test_delete_rejects_symlink_alias_before_resolving_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            album = root / "music" / "album"
            album.mkdir(parents=True)
            (album / "song.mp3").write_bytes(b"ID3")
            alias = root / "music" / "alias"
            try:
                alias.symlink_to(album, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks require additional privileges")
            with patch.object(media_manager, "MEDIA_ROOT", root):
                with self.assertRaises(HTTPException) as raised:
                    await media_manager.MediaManager._collect(["music/alias"])
            self.assertEqual(raised.exception.status_code, 400)
            self.assertTrue((album / "song.mp3").is_file())


if __name__ == "__main__":
    unittest.main()
