import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import playback


class _Result:
    def __init__(self, rowcount=0, row=None):
        self.rowcount = rowcount
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _PlaybackConnection:
    def __init__(self, insert_rowcount):
        self.insert_rowcount = insert_rowcount
        self.increment_count = 0

    async def execute(self, statement, _params=None):
        sql = str(statement)
        if "INSERT IGNORE INTO media_playback_events" in sql:
            return _Result(rowcount=self.insert_rowcount)
        if "INSERT INTO media_playback_stats" in sql:
            self.increment_count += 1
            return _Result()
        if "SELECT play_score" in sql:
            return _Result(row={"play_score": 8, "preference": 1})
        return _Result()


class _Transaction:
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
        return _Transaction(self.connection)


class PlaybackPolicyTests(unittest.TestCase):
    def test_preference_range_extends_from_negative_two_through_seven(self):
        self.assertEqual(playback.MIN_PREFERENCE, -2)
        self.assertEqual(playback.MAX_PREFERENCE, 7)

    def test_threshold_uses_half_duration_with_five_and_thirty_second_bounds(self):
        self.assertEqual(playback.valid_playback_threshold(6), 5)
        self.assertEqual(playback.valid_playback_threshold(40), 20)
        self.assertEqual(playback.valid_playback_threshold(600), 30)

    def test_sort_prefers_preference_then_lower_score_with_stable_tie_order(self):
        session_id = "d8088f10-4238-4a62-96f8-f5dd9c981fc1"
        items = [
            {"media_id": "a", "preference": 0, "play_score": 1},
            {"media_id": "b", "preference": 1, "play_score": 99},
            {"media_id": "c", "preference": 0, "play_score": 0},
            {"media_id": "d", "preference": 0, "play_score": 0},
        ]
        first = playback.sort_media(items, session_id)
        second = playback.sort_media(list(reversed(items)), session_id)

        self.assertEqual(first[0]["media_id"], "b")
        self.assertEqual(first[-1]["media_id"], "a")
        self.assertEqual([item["media_id"] for item in first], [item["media_id"] for item in second])

    def test_media_path_validation_rejects_escape_and_accepts_real_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            category = root / "music"
            category.mkdir()
            track = category / "song.mp3"
            track.write_bytes(b"ID3")

            normalized, media_id = playback.validate_media_path(root, "music/song.mp3")
            self.assertEqual(normalized, "music/song.mp3")
            self.assertEqual(media_id, playback.media_id_for_path(normalized))
            with self.assertRaises(ValueError):
                playback.validate_media_path(root, "../outside.mp3")

    def test_session_ids_are_canonical_uuids(self):
        value = playback.normalize_session_id("D8088F10-4238-4A62-96F8-F5DD9C981FC1")
        self.assertEqual(value, "d8088f10-4238-4a62-96f8-f5dd9c981fc1")
        with self.assertRaises(ValueError):
            playback.normalize_session_id("not-a-session")

    def test_database_constraint_migration_preserves_rows_and_expands_the_ceiling(self):
        source = (Path(__file__).resolve().parents[1] / "app" / "core" / "db.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("CHECK (preference BETWEEN -2 AND 7)", source)
        self.assertIn("DROP CHECK chk_media_preference", source)
        self.assertNotIn("UPDATE media_playback_stats", source)


class PlaybackIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def _record(self, insert_rowcount):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "music").mkdir()
        (root / "music" / "song.mp3").write_bytes(b"ID3")
        connection = _PlaybackConnection(insert_rowcount)
        with patch.object(playback, "engine", _Engine(connection)):
            result = await playback.record_playback(
                root,
                "music/song.mp3",
                "d8088f10-4238-4a62-96f8-f5dd9c981fc1",
                played_seconds=20,
                duration=40,
            )
        return result, connection

    async def test_first_session_media_event_increments_score(self):
        result, connection = await self._record(insert_rowcount=1)
        self.assertTrue(result["counted"])
        self.assertEqual(connection.increment_count, 1)

    async def test_duplicate_session_media_event_does_not_increment_score(self):
        result, connection = await self._record(insert_rowcount=0)
        self.assertFalse(result["counted"])
        self.assertEqual(connection.increment_count, 0)


if __name__ == "__main__":
    unittest.main()
