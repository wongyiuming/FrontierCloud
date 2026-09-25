import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import brand_assets


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

    def test_home_brand_art_is_the_rounded_card_surface(self):
        index = (ROOT / "static" / "media" / "index.html").read_text(encoding="utf-8")
        self.assertIn(".brand-surface{", index)
        self.assertIn("overflow:hidden", index)
        self.assertIn("border-radius:clamp(", index)
        self.assertIn(".brand-backdrop", index)
        self.assertIn(".brand-foreground", index)
        self.assertIn("object-fit:cover", index)
        self.assertGreaterEqual(index.count('class="brand-backdrop"'), 3)
        self.assertGreaterEqual(index.count('class="brand-foreground"'), 3)
        self.assertNotIn("card-title brand", index)

    def test_admin_logo_management_is_registered(self):
        endpoints = (ROOT / "app" / "api" / "v1" / "endpoints.py").read_text(encoding="utf-8")
        shell = (ROOT / "app" / "api" / "v1" / "admin_page_integrity.py").read_text(encoding="utf-8")
        client = (ROOT / "static" / "js" / "brand-admin.js").read_text(encoding="utf-8")

        self.assertIn('prefix="/media/brand"', endpoints)
        self.assertIn('prefix="/media/admin/brand"', endpoints)
        self.assertIn('prefix="/media/admin/upload/brand"', endpoints)
        self.assertIn("js/brand-admin.js", shell)
        self.assertIn("/api/v1/media/admin/upload/brand/", client)
        self.assertIn("method: 'POST'", client)
        self.assertIn("method: 'DELETE'", client)
        self.assertIn("/download", client)
        self.assertIn("删除后恢复内置默认", client)

    def test_custom_logo_overrides_default_and_delete_restores_default(self):
        default_dir = ROOT / "static" / "brand"
        with tempfile.TemporaryDirectory() as directory:
            custom_dir = Path(directory)
            with patch.object(brand_assets, "DEFAULT_BRAND_DIR", default_dir), patch.object(
                brand_assets, "CUSTOM_BRAND_DIR", custom_dir
            ):
                original = brand_assets.effective_logo("music")
                self.assertEqual(original.source, "default")

                payload = b"RIFF" + (20).to_bytes(4, "little") + b"WEBP" + b"x" * 16
                changed = brand_assets.store_custom("music", payload)
                self.assertEqual(changed.source, "custom")
                self.assertTrue(changed.path.is_file())

                self.assertTrue(brand_assets.delete_custom("music"))
                restored = brand_assets.effective_logo("music")
                self.assertEqual(restored.source, "default")

    def test_upload_signature_and_size_validation(self):
        self.assertEqual(
            brand_assets.inspect_upload(b"\x89PNG\r\n\x1a\n" + b"x" * 20)[0],
            ".png",
        )
        self.assertEqual(
            brand_assets.inspect_upload(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"x" * 20)[0],
            ".webp",
        )
        with self.assertRaises(ValueError):
            brand_assets.inspect_upload(b"not-an-image")


if __name__ == "__main__":
    unittest.main()
