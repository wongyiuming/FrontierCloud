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


if __name__ == "__main__":
    unittest.main()
