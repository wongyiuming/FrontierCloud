import unittest

import main


class RootRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_redirects_to_public_media_page(self):
        response = await main.root()

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/api/v1/media")

    async def test_karaoke_shell_uses_content_hashed_native_assets(self):
        response = await main.karaoke_page()
        content = response.body.decode("utf-8")

        self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertRegex(content, r'/static/css/karaoke\.css\?v=[0-9a-f]{16}')
        self.assertRegex(content, r'/static/js/karaoke\.js\?v=[0-9a-f]{16}')
        self.assertNotIn("{{KARAOKE_", content)
        self.assertIn("麦克风 → Web Audio 人声处理 → 录音", content)


if __name__ == "__main__":
    unittest.main()
