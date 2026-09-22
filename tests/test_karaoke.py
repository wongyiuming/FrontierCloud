import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.api.v1 import media


class KaraokeContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_context_reuses_selected_media_and_linked_lyrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            track = root / "music" / "album" / "song.mp3"
            track.parent.mkdir(parents=True)
            track.write_bytes(b"ID3")
            with (
                patch.object(media, "MEDIA_ROOT", root),
                patch.object(media, "ensure_media_mutations_ready"),
                patch.object(media, "_local_stream_metadata", new=AsyncMock(return_value={
                    "resource_id": "a" * 64, "owner_id": "b" * 32, "media_id": "c" * 64,
                })),
                patch.object(media.lyrics, "attach_links", new=AsyncMock(return_value=[{
                    "media_id": "c" * 64, "has_lyrics": True,
                }])),
            ):
                response = await media.get_karaoke_context("music/album/song.mp3", None)
        payload = json.loads(response.body)
        self.assertEqual(payload["stream_url"], "/api/v1/media/stream?file_path=music%2Falbum%2Fsong.mp3")
        self.assertEqual(payload["lyrics_url"], "/api/v1/media/lyrics/content?track=music%2Falbum%2Fsong.mp3")
        self.assertEqual(payload["type"], "audio")
        self.assertIn("no-store", response.headers["cache-control"])

    async def test_remote_context_keeps_media_and_lyrics_on_same_resource(self):
        resource_id = "d" * 64
        row = {"payload": {"type": "audio", "has_lyrics": True}}
        with (
            patch.object(media, "require_https"),
            patch.object(media.node_routing, "resolve", new=AsyncMock(return_value=(row, {}))) as resolve,
            patch.object(media.lyrics, "attach_links", new=AsyncMock()) as local_links,
        ):
            response = await media.get_karaoke_context(
                "music/shared/song.mp3", resource_id, request=object()
            )
        payload = json.loads(response.body)
        resolve.assert_awaited_once_with(resource_id, "music/shared/song.mp3")
        local_links.assert_not_awaited()
        self.assertIn("resource_id=" + resource_id, payload["stream_url"])
        self.assertIn("resource_id=" + resource_id, payload["lyrics_url"])


if __name__ == "__main__":
    unittest.main()
