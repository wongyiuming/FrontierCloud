import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import karaoke_accounts, karaoke_storage
from app.services.federation import protocol


class KaraokeAccountContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_password_policy_and_scrypt_round_trip(self):
        for invalid in ("short", "alllowercase123!", "ALLUPPERCASE123!", "NoDigitsHere!", "NoSpecial123"):
            with self.assertRaises(ValueError):
                karaoke_accounts.validate_password(invalid)
        encoded = await karaoke_accounts.hash_password("Huawei@123")
        self.assertTrue(await karaoke_accounts.verify_password("Huawei@123", encoded))
        self.assertFalse(await karaoke_accounts.verify_password("Huawei@124", encoded))
        self.assertNotIn("Huawei@123", encoded)
        self.assertTrue(await karaoke_accounts.verify_password(
            "FrontierCloud@Invalid1", karaoke_accounts.DUMMY_PASSWORD_HASH
        ))

    def test_recording_capability_is_operation_and_size_bound(self):
        credential = protocol.encode(b"k" * 48)
        token = protocol.recording_token(
            credential, "a" * 32, "b" * 32, "c" * 32, "d" * 32, "upload", 1000, size=123,
        )
        value = protocol.verify_recording_token(credential, token, 1001)
        self.assertEqual(value["op"], "upload")
        self.assertEqual(value["size"], 123)
        self.assertEqual(value["ct"], "application/octet-stream")
        with self.assertRaises(protocol.ProtocolError):
            protocol.verify_recording_token(protocol.encode(b"x" * 48), token, 1001)

    def test_download_trailer_restores_title_and_lyrics(self):
        metadata = json.dumps({
            "version": 1, "title": "测试歌曲", "lyrics": [{"time": 1.25, "text": "第一句"}],
        }, ensure_ascii=False, separators=(",", ":")).encode()
        payload = b"WEBM" + metadata + len(metadata).to_bytes(8, "big") + karaoke_storage.TRAILER_MAGIC
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.bin"
            path.write_bytes(payload)
            self.assertEqual(karaoke_storage.parse_trailer(path), {
                "title": "测试歌曲", "lyrics": [{"time": 1.25, "text": "第一句"}],
            })

    def test_recording_storage_path_cannot_escape_root(self):
        with self.assertRaises(Exception):
            karaoke_storage._path("../escape", "b" * 32, "c" * 32)


if __name__ == "__main__":
    unittest.main()
