import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SecurityUIContractTests(unittest.TestCase):
    def test_history_filters_pagination_and_reban_are_exposed(self):
        html = (ROOT / "static" / "media" / "admin.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "js" / "admin.js").read_text(encoding="utf-8")

        for element_id in (
            "securityIpFilter",
            "securityStatusFilter",
            "securityScopeFilter",
            "securityPrev",
            "securityNext",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("/api/v1/media/admin/security/reban", script)
        self.assertIn("page_size", script)
        self.assertIn("event.reason", script)

    def test_single_expanded_module_and_permanent_ban_controls_are_exposed(self):
        html = (ROOT / "static" / "media" / "admin.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "js" / "admin.js").read_text(encoding="utf-8")
        style = (ROOT / "static" / "css" / "admin.css").read_text(encoding="utf-8")

        self.assertGreaterEqual(html.count('class="module-heading"'), 6)
        self.assertEqual(html.count('class="admin-module'), 6)
        self.assertIn("function expandAdminModule(target)", script)
        self.assertIn("module === target && shouldExpand", script)
        self.assertIn('id="permanentBanForm"', html)
        self.assertIn("/api/v1/media/admin/security/permanent-ban", script)
        self.assertIn("overflow-y: scroll", style)
        self.assertIn("scrollbar-width: auto", style)


if __name__ == "__main__":
    unittest.main()
