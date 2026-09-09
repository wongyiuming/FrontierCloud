import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, UploadFile

from app.services import lyrics, media_manager


class _Result:
    def mappings(self):
        return self

    def all(self):
        return []


class _Connection:
    def __init__(self):
        self.executed = []

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params or {}))
        return _Result()


class _Context:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class _Engine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return _Context(self.connection)


class LyricFormatTests(unittest.TestCase):
    def test_txt_and_both_supported_json_shapes_are_normalized(self):
        self.assertEqual(lyrics.parse_lyric_bytes("\n甲\n乙\n".encode(), ".txt"), ["甲", "乙"])
        self.assertEqual(lyrics.parse_lyric_bytes(json.dumps(["甲", "乙"]).encode(), ".json"), ["甲", "乙"])
        self.assertEqual(
            lyrics.parse_lyric_bytes(json.dumps({"lines": ["甲", "乙"]}).encode(), ".json"),
            ["甲", "乙"],
        )

    def test_invalid_encoding_and_non_string_json_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            lyrics.parse_lyric_bytes(b"\xff", ".txt")
        with self.assertRaisesRegex(ValueError, "字符串数组"):
            lyrics.parse_lyric_bytes(b'["ok", 3]', ".json")


class LyricUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_is_flat_validated_and_world_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            lyric_root = root / "lyrics"
            lyric_root.mkdir()
            upload = UploadFile(filename="共享歌词.txt", file=io.BytesIO("第一行\n第二行".encode()))
            with (
                patch.object(media_manager, "MEDIA_ROOT", root),
                patch.object(media_manager, "LYRICS_ROOT", lyric_root),
            ):
                saved = await media_manager.MediaManager.upload_lyric(upload)
            await upload.close()

            self.assertEqual(saved, "lyrics/共享歌词.txt")
            self.assertEqual((lyric_root / "共享歌词.txt").read_text(encoding="utf-8"), "第一行\n第二行")
            self.assertTrue((lyric_root / "共享歌词.txt").stat().st_mode & 0o004)

    async def test_upload_rejects_unsupported_extension(self):
        upload = UploadFile(filename="lyrics.lrc", file=io.BytesIO(b"line"))
        with self.assertRaises(HTTPException) as raised:
            await media_manager.MediaManager.upload_lyric(upload)
        await upload.close()
        self.assertEqual(raised.exception.status_code, 400)


class LyricRelationTests(unittest.IsolatedAsyncioTestCase):
    async def test_track_relation_replacement_is_one_database_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            music_root = root / "music"
            lyric_root = root / "lyrics"
            album = music_root / "album"
            album.mkdir(parents=True)
            lyric_root.mkdir()
            (album / "song.mp3").write_bytes(b"ID3")
            (lyric_root / "shared.json").write_text('["one"]', encoding="utf-8")
            connection = _Connection()
            with (
                patch.object(lyrics, "MEDIA_ROOT", root),
                patch.object(lyrics, "MUSIC_ROOT", music_root),
                patch.object(lyrics, "LYRICS_ROOT", lyric_root),
                patch.object(lyrics, "engine", _Engine(connection)),
            ):
                count = await lyrics.replace_relations(
                    "track", "music/album/song.mp3", ["lyrics/shared.json"],
                )

        self.assertEqual(count, 1)
        sql = "\n".join(statement for statement, _params in connection.executed)
        self.assertIn("DELETE FROM media_lyric_links", sql)
        self.assertIn("INSERT INTO media_lyric_links", sql)
        insert_params = connection.executed[-1][1]
        self.assertEqual(insert_params["media_path"], "music/album/song.mp3")
        self.assertEqual(insert_params["lyric_path"], "lyrics/shared.json")

    async def test_one_track_cannot_link_multiple_lyrics(self):
        with self.assertRaisesRegex(ValueError, "最多关联一份"):
            await lyrics.replace_relations("track", "missing", ["one", "two"])


if __name__ == "__main__":
    unittest.main()
