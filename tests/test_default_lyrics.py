import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


class DefaultLyricTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_file_is_rebuilt_with_required_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            lyric_root = root / "lyrics"
            with patch.object(lyrics, "LYRICS_ROOT", lyric_root):
                relative, path = lyrics.ensure_default_lyric_file()
                self.assertEqual(relative, "lyrics/default.lrc")
                self.assertEqual(path.read_text(encoding="utf-8"), "[00:00.00]建设中，暂无歌词\n")
                path.write_text("[00:00.00]wrong\n", encoding="utf-8")
                lyrics.ensure_default_lyric_file()
                self.assertEqual(
                    lyrics.parse_lrc_bytes(path.read_bytes()),
                    [{"time": 0.0, "text": "建设中，暂无歌词"}],
                )

    async def test_clearing_track_relation_rebinds_default_lyric(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            music_root = root / "music"
            lyric_root = root / "lyrics"
            album = music_root / "album"
            album.mkdir(parents=True)
            lyric_root.mkdir()
            track_path = "music/album/song.mp3"
            (album / "song.mp3").write_bytes(b"ID3")
            connection = _Connection()
            with (
                patch.object(lyrics, "MEDIA_ROOT", root),
                patch.object(lyrics, "MUSIC_ROOT", music_root),
                patch.object(lyrics, "LYRICS_ROOT", lyric_root),
                patch.object(lyrics, "engine", _Engine(connection)),
                patch.object(lyrics, "_track_identity", new=AsyncMock(return_value=(track_path, "a" * 64))),
                patch.object(lyrics.media_objects, "ensure_object", new=AsyncMock(return_value="b" * 64)),
                patch.object(media_manager, "ensure_media_mutations_ready", return_value=None),
            ):
                count = await lyrics.replace_relations("track", track_path, [])

        self.assertEqual(count, 1)
        inserts = [params for statement, params in connection.executed if "INSERT INTO media_lyric_links" in statement]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0]["lyric_path"], lyrics.DEFAULT_LYRIC_PATH)


if __name__ == "__main__":
    unittest.main()
