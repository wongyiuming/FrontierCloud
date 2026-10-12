from pathlib import Path
import unittest

from fastapi import FastAPI

from app.api.v1 import endpoints as api_endpoints


ROOT = Path(__file__).resolve().parents[1]


class AdminSystemModulesContractTests(unittest.TestCase):
    def read(self, path: str) -> str:
        return (ROOT / path).read_text(encoding="utf-8")

    def test_software_update_module_is_retired(self):
        page = self.read("app/api/v1/admin_page_integrity.py")
        html = self.read("static/media/admin.html")
        self.assertNotIn("release-admin.js", page)
        self.assertNotIn("release-version-ui.js", page)
        self.assertNotIn("systemVersionPanel", html)
        self.assertFalse((ROOT / "static/js/release-admin.js").exists())

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
        maintenance = application.openapi().get("paths", {}).get(
            "/api/v1/media/admin/site/maintenance", {}
        )

        self.assertIn('router = APIRouter(prefix="/site")', api)
        self.assertIn("get", maintenance)
        self.assertIn("post", maintenance)
        self.assertIn("NATIVE_MAINTENANCE", service)
        self.assertIn("控制台不能解除围栏", service)
        self.assertIn("站点开放状态", client)
        self.assertIn("进入维护", client)
        self.assertIn("结束维护", client)

    def test_all_existing_brand_logo_management_is_preserved(self):
        brand = self.read("static/js/brand-admin.js")
        self.assertIn("前沿娱乐 / 前沿媒体 / 前沿音乐", brand)
        self.assertIn("state.items.map(card)", brand)


if __name__ == "__main__":
    unittest.main()
