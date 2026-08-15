import unittest

import main


class RootRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_redirects_to_public_media_page(self):
        response = await main.root()

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/api/v1/media")


if __name__ == "__main__":
    unittest.main()
