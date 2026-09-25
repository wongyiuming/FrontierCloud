from pathlib import Path
import unittest

from fastapi import FastAPI

from app.api.v1 import endpoints as api_endpoints


ROOT = Path(__file__).resolve().parents[1]


class AdminSystemModulesContractTests(unittest.TestCase):
    def read(self, path: str) -> str:
        return (ROOT / path).read_text(encoding="utf-8")

    def test_release_management_is_a_standalone_realtime_module(self):
        release = self.read("static/js/release-admin.js")
        page = self.read("app/api/v1/admin_page_integrity.py")
        css = self.read("static/css/admin-system-modules.css")
        self.assertIn("systemVersionPanel", release)
        self.assertIn("系统版本管理", release)
        self.assertIn("集群实时进度", release)
        self.assertIn("release-node-list", release)
        self.assertIn("schedule(masterBusy ? 1500 : 5000)", release)
        self.assertIn('static_asset_url("js/release-admin.js")', page)
        self.assertIn("#nodeReleasePanel { display: none !important; }", css)

    def test_expanded_admin_module_owns_the_viewport(self):
        css = self.read("static/css/admin-system-modules.css")
        focus = self.read("static/js/admin-focus.js")
        self.assertIn("min-height: calc(100dvh - 28px)", css)
        self.assertIn("flex-basis: calc(100dvh - 28px)", css)
        self.assertIn("scrollIntoView", focus)

    def test_site_maintenance_is_independently_visible_and_controllable(self):
        api = self.read("app/api/v1/admin_site.py")
        service = self.read("app/services/site_control.py")
        client = self.read("static/js/maintenance-admin.js")
        application = FastAPI()
        application.include_router(api_endpoints.router, prefix="/api/v1")
        maintenance_methods = set()
        for route in application.routes:
            if getattr(route, "path", "") == "/api/v1/media/admin/site/maintenance":
                maintenance_methods.update(getattr(route, "methods", set()) or set())

        self.assertIn('router = APIRouter(prefix="/site")', api)
        self.assertTrue({"GET", "POST"}.issubset(maintenance_methods))
        self.assertIn("FORCE_OPEN", service)
        self.assertIn("版本发布正在执行", service)
        self.assertIn("站点开放状态", client)
        self.assertIn("进入维护", client)
        self.assertIn("结束维护", client)

    def test_all_existing_brand_logo_management_is_preserved(self):
        brand = self.read("static/js/brand-admin.js")
        self.assertIn("前沿娱乐 / 前沿媒体 / 前沿音乐", brand)
        self.assertIn("state.items.map(card)", brand)


if __name__ == "__main__":
    unittest.main()
