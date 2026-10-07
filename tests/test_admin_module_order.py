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
    ("brand", 110),
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
        self.assertEqual(names[-1], "brand")

    def test_static_and_runtime_modules_use_the_order_contract_names(self):
        html = (ROOT / "static/media/admin.html").read_text(encoding="utf-8")
        release = (ROOT / "static/js/release-admin.js").read_text(encoding="utf-8")
        maintenance = (ROOT / "static/js/maintenance-admin.js").read_text(encoding="utf-8")
        brand = (ROOT / "static/js/brand-admin.js").read_text(encoding="utf-8")

        for module in ("media", "priority", "lyrics", "users", "security", "network", "nodes", "key"):
            self.assertIn(f'data-admin-module="{module}"', html)
        self.assertIn("panel.dataset.adminModule = 'site'", maintenance)
        self.assertIn("panel.dataset.adminModule = 'release'", release)
        self.assertIn("panel.dataset.adminModule = 'brand'", brand)
        self.assertIn("consoleRoot?.append(panel)", brand)

    def test_dom_reorder_matches_visual_order(self):
        focus = (ROOT / "static/js/admin-focus.js").read_text(encoding="utf-8")
        match = re.search(r"const MODULE_ORDER = \[(?P<body>.*?)\];", focus, re.S)
        self.assertIsNotNone(match)
        names = re.findall(r"'([^']+)'", match.group("body"))
        self.assertEqual(names, [name for name, _order in EXPECTED_ORDER])
        self.assertIn("for (const module of sortedModules) consoleRoot.append(module);", focus)

    def test_dynamic_reorder_is_idempotent_and_top_level_only(self):
        focus = (ROOT / "static/js/admin-focus.js").read_text(encoding="utf-8")
        # Re-appending an already sorted module list creates new childList records.
        # Guard the reorder itself and observe only direct Admin children so a
        # late site/release/brand panel cannot create a recursive DOM move loop.
        self.assertIn(
            "if (modules.every((module, index) => module === sortedModules[index])) return;",
            focus,
        )
        self.assertIn("moduleObserver.observe(consoleRoot, {childList: true});", focus)
        self.assertNotIn(".observe(document.body, {childList: true, subtree: true});", focus)
        self.assertIn("if (reorderScheduled) return;", focus)

    def test_toggle_marker_is_pinned_to_module_far_right(self):
        css = (ROOT / "static/css/admin-system-modules.css").read_text(encoding="utf-8")
        marker = re.search(
            r"\.admin-console \.admin-module \.module-heading b\s*\{(?P<body>.*?)\}",
            css,
            re.S,
        )
        self.assertIsNotNone(marker)
        body = marker.group("body")
        self.assertIn("position: absolute;", body)
        self.assertIn("top: 14px;", body)
        self.assertIn("right: 14px;", body)
        self.assertRegex(css, r"\.module-heading\s*\{[^}]*padding-right:\s*54px;", re.S)
        self.assertRegex(css, r"\.security-header-actions\s*\{[^}]*margin-right:\s*40px;", re.S)

    def test_system_styles_and_dom_reorder_load_after_dynamic_modules(self):
        page = (ROOT / "internal/httpapi/admin.go").read_text(encoding="utf-8")
        self.assertIn('"css/admin-system-modules.css"', page)
        self.assertIn('strings.Replace(content, "</head>"', page)
        release_pos = page.index('"js/release-admin.js"')
        maintenance_pos = page.index('"js/maintenance-admin.js"')
        brand_pos = page.index('"js/brand-admin.js"')
        focus_pos = page.index('"js/admin-focus.js"')
        self.assertLess(release_pos, focus_pos)
        self.assertLess(maintenance_pos, focus_pos)
        self.assertLess(brand_pos, focus_pos)


if __name__ == "__main__":
    unittest.main()
