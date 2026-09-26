from pathlib import Path

path = Path("tests/test_player_ui.py")
text = path.read_text(encoding="utf-8")
marker = "    def test_android_portrait_layout_stacks_player_above_sidebar(self):\n"
method = '''    def test_secondary_catalog_logo_reveals_hidden_media_after_fifteen_clicks(self):
        browser = (ROOT / "static" / "js" / "media-browser.js").read_text(encoding="utf-8")
        category = (ROOT / "static" / "media" / "category.html").read_text(encoding="utf-8")
        home = (ROOT / "static" / "media" / "index.html").read_text(encoding="utf-8")
        api = (ROOT / "app" / "api" / "v1" / "media.py").read_text(encoding="utf-8")

        self.assertIn("HIDDEN_REVEAL_CLICK_LIMIT = 15", browser)
        self.assertIn("HIDDEN_REVEAL_WINDOW_MS = 60 * 1000", browser)
        self.assertIn("pageBrandLogo", browser)
        self.assertIn("/api/v1/media/catalog/reveal?media_type=", browser)
        self.assertIn("X-Frontier-Hidden-Reveal", browser)
        self.assertIn("window.location.reload()", browser)
        self.assertIn("frontierCloudCatalogRevealKind", category)
        self.assertIn("brandKind === 'music' ? 'music'", category)
        self.assertIn("brandKind === 'media' ? 'video'", category)
        self.assertNotIn("frontierCloudCatalogRevealKind", home)
        self.assertIn('@router.post("/catalog/reveal")', api)
        self.assertIn("frontier_hidden_music", api)
        self.assertIn("frontier_hidden_video", api)
        self.assertIn("include_hidden=include_hidden", api)
        self.assertIn("allow_hidden=_hidden_reveal_enabled(request, reveal_type)", api)

'''
if text.count(marker) != 1:
    raise SystemExit(f"player test marker count={text.count(marker)}")
path.write_text(text.replace(marker, method + marker, 1), encoding="utf-8")

Path("tests/test_hidden_media_reveal.py").write_text('''import unittest

from starlette.requests import Request

from app.api.v1 import media


class HiddenMediaRevealTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _request(cookie: str = "", reveal_header: bool = False) -> Request:
        headers = []
        if cookie:
            headers.append((b"cookie", cookie.encode("ascii")))
        if reveal_header:
            headers.append((b"x-frontier-hidden-reveal", b"1"))
        return Request({
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/api/v1/media/catalog/reveal",
            "raw_path": b"/api/v1/media/catalog/reveal",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("example.test", 443),
        })

    def test_reveal_cookie_is_scoped_by_media_type(self):
        request = self._request("frontier_hidden_music=1")
        self.assertTrue(media._hidden_reveal_enabled(request, "music"))
        self.assertFalse(media._hidden_reveal_enabled(request, "video"))

    async def test_reveal_endpoint_sets_httponly_session_cookie(self):
        response = await media.reveal_hidden_catalog(self._request(reveal_header=True), media_type="music")
        cookie = response.headers.get("set-cookie", "")
        self.assertIn("frontier_hidden_music=1", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertNotIn("Max-Age", cookie)

    async def test_reveal_endpoint_rejects_missing_gesture_header(self):
        with self.assertRaises(media.HTTPException) as context:
            await media.reveal_hidden_catalog(self._request(), media_type="video")
        self.assertEqual(context.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
''', encoding="utf-8")
