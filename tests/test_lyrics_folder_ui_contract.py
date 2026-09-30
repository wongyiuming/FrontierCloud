from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LyricsFolderAdminUIContractTests(unittest.TestCase):
    def test_folder_upload_and_auto_link_controls_are_wired(self):
        html = (ROOT / "static/media/admin.html").read_text(encoding="utf-8")
        admin = (ROOT / "static/js/admin.js").read_text(encoding="utf-8")
        integrity = (ROOT / "static/js/admin-upload-integrity.js").read_text(encoding="utf-8")

        self.assertIn('id="uploadLyricsFolder"', html)
        self.assertIn('id="lyricsFolderInput"', html)
        self.assertIn('id="lyricsAutoLink"', html)
        self.assertIn("api('/api/v1/media/admin/lyrics/auto-relate'", admin)
        self.assertEqual(admin.count("/api/v1/media/admin/lyrics/auto-relate"), 1)
        self.assertIn("JSON.stringify({manual: true})", admin)
        self.assertIn("formData.append('relative_path', relativePaths[index])", admin)
        self.assertIn("formData.append('relative_path', relativePaths[index])", integrity)


if __name__ == "__main__":
    unittest.main()
