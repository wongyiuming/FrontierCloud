import unittest
from unittest.mock import AsyncMock, patch

from pathlib import Path

from app.api.v1 import media


class HiddenMediaRevealTests(unittest.IsolatedAsyncioTestCase):
    def test_category_url_can_publicly_request_hidden_entries(self):
        url = media._category_url("music", "music/hidden", include_hidden=True)
        self.assertIn("path=music%2Fhidden", url)
        self.assertIn("include_hidden=true", url)

    async def test_category_catalog_forwards_public_include_hidden_flag(self):
        loader = AsyncMock(return_value=[])
        with patch.object(media, "get_media_categories", new=loader):
            response = await media.get_media_categories_data(media_type="music", include_hidden=True)
        loader.assert_awaited_once_with("music", media.AUDIO_EXTS, include_hidden=True)
        self.assertEqual(response.status_code, 200)

    async def test_media_catalog_forwards_public_include_hidden_flag(self):
        validate = AsyncMock(return_value=("music", "hidden"))
        loader = AsyncMock(return_value=[])
        with (
            patch.object(media, "_public_category_parts", new=validate),
            patch.object(media, "_player_entries", new=loader),
        ):
            response = await media.get_media_catalog_data(
                media_type="music",
                path="music/hidden",
                playback_session_id="test-session",
                include_hidden=True,
            )
        validate.assert_awaited_once_with("music/hidden", "music")
        loader.assert_awaited_once_with(
            "music/hidden",
            "music",
            "test-session",
            include_hidden=True,
        )
        self.assertEqual(response.status_code, 200)

    def test_hidden_state_is_presentation_only_not_stream_or_lyrics_acl(self):
        source = Path(media.__file__).read_text(encoding="utf-8")
        self.assertNotIn("SELECT EXISTS(SELECT 1 FROM media_visibility", source)
        self.assertNotIn("_is_publicly_hidden(normalized_track, await _hidden_set())", source)


if __name__ == "__main__":
    unittest.main()
