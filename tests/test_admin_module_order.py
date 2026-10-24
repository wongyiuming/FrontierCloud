"""Regression contract for functional grouping in the Admin console."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AdminShellAssetsTests(unittest.IsolatedAsyncioTestCase):
    async def test_effective_admin_route_resolves_the_migration_client_content_hash(self):
        from fastapi import FastAPI
        import httpx
        from app.api.v1.admin import require_session
        from app.api.v1.endpoints import router
        from app.core.static_assets import static_asset_url
        application=FastAPI();application.include_router(router,prefix='/api/v1')
        application.dependency_overrides[require_session]=lambda:'test-admin'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application,client=('198.51.100.9',4321)),base_url='https://master.4399.com') as client:
            response=await client.get('/api/v1/media/admin/')
        self.assertEqual(response.status_code,200)
        self.assertIn(static_asset_url('js/migrations.js'),response.text)
        self.assertNotIn('{{MIGRATIONS_JS_URL}}',response.text)

EXPECTED_ORDER = [
    ("media", 10),
    ("priority", 20),
    ("lyrics", 30),
    ("users", 40),
    ("security", 50),
    ("network", 60),
    ("nodes", 70),
    ("migrations", 75),
    ("site", 80),
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
        self.assertEqual(names[6:9], ["nodes", "migrations", "site"])
        self.assertEqual(names[-1], "brand")

    def test_static_and_runtime_modules_use_the_order_contract_names(self):
        html = (ROOT / "static/media/admin.html").read_text(encoding="utf-8")
        maintenance = (ROOT / "static/js/maintenance-admin.js").read_text(encoding="utf-8")
        brand = (ROOT / "static/js/brand-admin.js").read_text(encoding="utf-8")

        for module in ("media", "priority", "lyrics", "users", "security", "network", "nodes", "migrations", "key"):
            self.assertIn(f'data-admin-module="{module}"', html)
        self.assertIn("panel.dataset.adminModule = 'site'", maintenance)
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
        page = (ROOT / "app/api/v1/admin_page_integrity.py").read_text(encoding="utf-8")
        self.assertIn('static_asset_url("css/admin-system-modules.css")', page)
        self.assertIn('content.replace("</head>"', page)
        maintenance_pos = page.index('static_asset_url("js/maintenance-admin.js")')
        brand_pos = page.index('static_asset_url("js/brand-admin.js")')
        focus_pos = page.index('static_asset_url("js/admin-focus.js")')
        self.assertLess(maintenance_pos, focus_pos)
        self.assertLess(brand_pos, focus_pos)


if __name__ == "__main__":
    unittest.main()
