"""Regression contract for functional grouping in the Admin console."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_ORDER = [
    ("media", 10),
    ("priority", 20),
    ("lyrics", 30),
    ("users", 40),
    ("security", 50),
    ("network", 60),
    ("nodes", 70),
    ("site", 80),
    ("release", 90),
    ("key", 100),
]


class AdminModuleOrderTests(unittest.TestCase):
    def test_all_modules_have_one_explicit_visual_order(self):
        css = (ROOT / "static/css/admin-system-modules.css").read_text(encoding="utf-8")
        for module, order in EXPECTED_ORDER:
            selector = (
                rf'\.admin-console\s*>\s*\.admin-module\[data-admin-module="{module}"\]'
                rf'\s*\{{\s*order:\s*{order};\s*\}}'
            )
            self.assertRegex(css, selector, f"missing canonical order for Admin module {module}")

    def test_related_modules_remain_adjacent(self):
        names = [name for name, _order in EXPECTED_ORDER]
        self.assertEqual(names[0:3], ["media", "priority", "lyrics"])
        self.assertEqual(names[4:6], ["security", "network"])
        self.assertEqual(names[6:9], ["nodes", "site", "release"])

    def test_static_and_runtime_modules_use_the_order_contract_names(self):
        html = (ROOT / "static/media/admin.html").read_text(encoding="utf-8")
        release = (ROOT / "static/js/release-admin.js").read_text(encoding="utf-8")
        maintenance = (ROOT / "static/js/maintenance-admin.js").read_text(encoding="utf-8")

        for module in ("media", "priority", "lyrics", "users", "security", "network", "nodes", "key"):
            self.assertIn(f'data-admin-module="{module}"', html)
        self.assertIn("panel.dataset.adminModule = 'site'", maintenance)
        self.assertIn("panel.dataset.adminModule = 'release'", release)

    def test_system_order_stylesheet_is_loaded_after_base_admin_css(self):
        page = (ROOT / "app/api/v1/admin_page_integrity.py").read_text(encoding="utf-8")
        self.assertIn('static_asset_url("css/admin-system-modules.css")', page)
        self.assertIn('content.replace("</head>"', page)


if __name__ == "__main__":
    unittest.main()
