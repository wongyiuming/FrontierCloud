import unittest

from auto_download.nama_clean import (
    allocate_unique_stem,
    sanitize_component,
    sanitize_filename,
)


class FilenameCleaningTests(unittest.TestCase):
    def test_sanitize_component_preserves_unicode_and_collapses_punctuation(self):
        self.assertEqual(sanitize_component("  羽江：歌 / 01？ "), "羽江_歌_01")

    def test_sanitize_component_avoids_windows_reserved_names(self):
        self.assertEqual(sanitize_component("CON"), "_CON")

    def test_sanitize_filename_removes_repeated_parent_and_keeps_extension(self):
        self.assertEqual(
            sanitize_filename("黄耀明 - 黄耀明：春光乍泄.MP3", parent_name="黄耀明"),
            "春光乍泄.mp3",
        )

    def test_allocate_unique_stem_uses_media_id_only_for_collisions(self):
        occupied: set[str] = set()
        self.assertEqual(allocate_unique_stem("同名歌曲", "first", occupied), "同名歌曲")
        self.assertEqual(
            allocate_unique_stem("同名歌曲", "second-id", occupied),
            "同名歌曲_second_id",
        )


if __name__ == "__main__":
    unittest.main()
