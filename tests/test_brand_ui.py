import unittest
from pathlib import Path



ROOT = Path(__file__).resolve().parents[1]


class BrandUIContractTests(unittest.TestCase):
    def test_built_in_logo_assets_and_public_ui_contract(self):
        for name in (
            "frontier-entertainment.webp",
            "frontier-media.webp",
            "frontier-music.webp",
        ):
            self.assertTrue((ROOT / "static" / "brand" / name).is_file())

        index = (ROOT / "static" / "media" / "index.html").read_text(encoding="utf-8")
        category = (ROOT / "static" / "media" / "category.html").read_text(encoding="utf-8")
        karaoke = (ROOT / "static" / "media" / "karaoke.html").read_text(encoding="utf-8")

        self.assertIn("/api/v1/media/brand/logo/entertainment", index)
        self.assertIn("/api/v1/media/brand/logo/music", index)
        self.assertIn("/api/v1/media/brand/logo/media", index)
        self.assertIn('id="refreshHotspot"', index)
        self.assertIn('id="elevateHotspot"', index)
        self.assertIn("/api/v1/media/brand/logo/${brandKind}", category)
        self.assertIn("/api/v1/media/brand/logo/entertainment", karaoke)

    def test_home_keeps_small_main_brand_and_full_area_child_cards(self):
        index = (ROOT / "static" / "media" / "index.html").read_text(encoding="utf-8")
        self.assertIn("grid-template-rows:auto minmax(0,1fr)", index)
        self.assertIn(".brand-main{", index)
        self.assertIn("width:clamp(220px,34vw,460px)", index)
        self.assertIn("height:clamp(72px,10vw,132px)", index)
        self.assertIn(".card-grid{display:grid;min-height:0;grid-template-columns:repeat(2,minmax(0,1fr))", index)
        self.assertIn(".card{position:relative;display:grid;min-width:0;min-height:0;place-items:center", index)
        self.assertEqual(index.count('class="card"'), 2)
        self.assertEqual(index.count('class="card-logo"'), 2)
        self.assertIn("pointer-events:none", index)
        self.assertNotIn("aspect-ratio:", index)
        self.assertNotIn("brand-backdrop", index)
        self.assertNotIn("brand-foreground", index)

    def test_admin_logo_management_is_registered(self):
        endpoints = (ROOT / "internal/httpapi/admin.go").read_text(encoding="utf-8") + (ROOT / "internal/httpapi/public.go").read_text(encoding="utf-8")
        shell = (ROOT / "internal/httpapi/admin.go").read_text(encoding="utf-8")
        client = (ROOT / "static" / "js" / "brand-admin.js").read_text(encoding="utf-8")

        self.assertIn('"/api/v1/media/brand/logo/:kind"', endpoints)
        self.assertIn('"/brand"', endpoints)
        self.assertIn('"/upload/brand/:kind"', endpoints)
        self.assertIn("js/brand-admin.js", shell)
        self.assertIn("/api/v1/media/admin/upload/brand/", client)
        self.assertIn("method: 'POST'", client)
        self.assertIn("method: 'DELETE'", client)
        self.assertIn("/download", client)
        self.assertIn("删除后恢复内置默认", client)

    def test_native_logo_contract_has_runtime_regressions(self):
        native = (ROOT / "internal/httpapi/brand.go").read_text(encoding="utf-8")
        tests = (ROOT / "internal/httpapi/brand_test.go").read_text(encoding="utf-8")
        self.assertIn("public, max-age=31536000, immutable", native)
        self.assertIn("c.Redirect(307, logo.URL)", native)
        self.assertIn("Test", tests)
        self.assertTrue((ROOT / "internal/brand/service_test.go").is_file())


if __name__ == "__main__":
    unittest.main()
