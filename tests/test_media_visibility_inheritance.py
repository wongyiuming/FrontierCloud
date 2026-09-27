from __future__ import annotations

import unittest
from pathlib import Path

from app.services.media_visibility_integrity import _decorate, effective_hidden


ROOT = Path(__file__).resolve().parents[1]


class MediaVisibilityInheritanceTests(unittest.TestCase):
    def test_effective_hidden_inherits_from_parent(self):
        hidden = {"music/artist"}
        self.assertTrue(effective_hidden("music/artist", hidden))
        self.assertTrue(effective_hidden("music/artist/album", hidden))
        self.assertTrue(effective_hidden("music/artist/album/song.mp3", hidden))
        self.assertFalse(effective_hidden("music/other/album", hidden))

    def test_tree_items_separate_effective_and_direct_hidden(self):
        items = [
            {"path": "music/artist", "kind": "directory"},
            {"path": "music/artist/album", "kind": "directory"},
            {"path": "music/other", "kind": "directory"},
        ]
        _decorate(items, {"music/artist"})
        self.assertEqual(
            [(item["hidden"], item["hidden_direct"]) for item in items],
            [(True, True), (True, False), (False, False)],
        )

    def test_admin_disables_noop_restore_for_inherited_hiding(self):
        client = (ROOT / "static/js/admin-visibility-integrity.js").read_text(encoding="utf-8")
        page = (ROOT / "app/api/v1/admin_page_integrity.py").read_text(encoding="utf-8")
        endpoints = (ROOT / "app/api/v1/endpoints.py").read_text(encoding="utf-8")

        self.assertIn("hidden_direct", client)
        self.assertIn("由上级隐藏", client)
        self.assertIn("row.dataset.hiddenInherited === 'true'", client)
        self.assertIn("$('hide').disabled = true", client)
        self.assertIn('static_asset_url("js/admin-visibility-integrity.js")', page)
        self.assertIn("install_media_visibility_integrity()", endpoints)


if __name__ == "__main__":
    unittest.main()
