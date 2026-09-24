import json
import unittest
from unittest.mock import AsyncMock, patch

from app.api.v1 import karaoke
from app.services import karaoke_identity


class KaraokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_context_uses_master_lyrics_independent_of_placement(self):
        resource_id = "a" * 64
        row = {
            "path": "music/shared/song.mp3",
            "payload": {"type": "audio", "has_lyrics": False},
        }
        with (
            patch.object(karaoke.karaoke_identity, "resolve", return_value=("global", resource_id)),
            patch.object(karaoke.media_api, "require_https"),
            patch.object(karaoke.node_routing, "resolve", AsyncMock(return_value=(row, {}))),
            patch.object(karaoke.node_routing, "lyric_entries", AsyncMock(return_value=[{"time": 1, "text": "master"}])) as master_lyrics,
        ):
            resolved = await karaoke._resolve("opaque", request=object())
        self.assertTrue(resolved["has_lyrics"])
        master_lyrics.assert_awaited_once_with(resource_id)

    async def test_context_exposes_only_opaque_identity_and_api_urls(self):
        opaque = "A" * 100
        with patch.object(karaoke, "_resolve", AsyncMock(return_value={
            "kind": "standalone", "identifier": "b" * 64, "path": "music/shared/song.mp3",
            "type": "audio", "has_lyrics": True,
        })):
            payload = await karaoke.context(opaque, request=object())
        data = payload.model_dump()
        self.assertEqual(data["id"], opaque)
        self.assertEqual(data["title"], "song")
        self.assertEqual(data["stream_url"], f"/api/v1/karaoke/stream?media={opaque}")
        self.assertEqual(data["lyrics_url"], f"/api/v1/karaoke/lyrics?media={opaque}")
        serialized = json.dumps(data)
        self.assertNotIn("music/shared", serialized)
        self.assertNotIn("resource_id", serialized)

    async def test_video_without_lyrics_is_valid(self):
        with patch.object(karaoke, "_resolve", AsyncMock(return_value={
            "kind": "global", "identifier": "c" * 64, "path": "vido/live/video.mp4",
            "type": "video", "has_lyrics": False,
        })):
            payload = await karaoke.context("D" * 100, request=object())
        self.assertFalse(payload.has_lyrics)
        self.assertIsNone(payload.lyrics_url)

    async def test_global_lyrics_use_master_relation(self):
        opaque = "E" * 100
        entries = [{"time": 1.25, "text": "line"}]
        with patch.object(karaoke, "_resolve", AsyncMock(return_value={
            "kind": "global", "identifier": "f" * 64, "path": "music/shared/song.mp3",
            "type": "audio", "has_lyrics": True,
        })), patch.object(karaoke.node_routing, "lyric_entries", AsyncMock(return_value=entries)) as master_lyrics:
            response = await karaoke.lyric_entries(opaque, request=object())
        self.assertEqual(json.loads(response.body), {"entries": entries})
        master_lyrics.assert_awaited_once_with("f" * 64)

    def test_identity_is_encrypted_and_round_trips_without_path(self):
        payload = None
        def seal(value):
            nonlocal payload
            payload = value
            return "opaque-token"
        with patch.object(karaoke_identity.state, "seal_client_identity", side_effect=seal), \
             patch.object(karaoke_identity.state, "unseal_client_identity", side_effect=lambda _token: payload):
            token = karaoke_identity.issue(media_id="a" * 64)
            self.assertEqual(token, "opaque-token")
            self.assertEqual(karaoke_identity.resolve(token), ("standalone", "a" * 64))
            self.assertNotIn("media_path", payload)
if __name__ == "__main__":
    unittest.main()
