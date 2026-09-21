import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
        self.preference = 1

    async def execute(self, statement, _params=None):
        sql = str(statement)
        params = _params or {}
        if "INSERT INTO media_playback_stats" in sql and "value" in params:
            self.preference = params["value"]
        if "INSERT IGNORE INTO media_playback_events" in sql:
            return _Result(rowcount=self.insert_rowcount)
        if "SET play_score=play_score + 1" in sql:
            self.increment_count += 1
            return _Result()
        if "SELECT play_score" in sql:
            return _Result(row={"play_score": 8, "preference": self.preference})
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
    def test_preference_range_extends_from_negative_seven_through_five_hundred(self):
        self.assertEqual(playback.MIN_PREFERENCE, -7)
        self.assertEqual(playback.MAX_PREFERENCE, 500)

    def test_threshold_uses_half_duration_with_five_and_thirty_second_bounds(self):
        self.assertEqual(playback.valid_playback_threshold(6), 5)
        self.assertEqual(playback.valid_playback_threshold(40), 20)
        self.assertEqual(playback.valid_playback_threshold(600), 30)

    def test_sort_uses_manual_preference_and_stable_tie_order_without_play_count(self):
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
        self.assertEqual([item["media_id"] for item in first], [item["media_id"] for item in second])
        tied = [item["media_id"] for item in first if item["preference"] == 0]
        self.assertEqual(set(tied), {"a", "c", "d"})

    def test_media_path_validation_rejects_escape_and_accepts_real_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            category = root / "music" / "artist"
            category.mkdir(parents=True)
            track = category / "song.mp3"
            track.write_bytes(b"ID3")

            normalized, validated = playback.validate_media_path(root, "music/artist/song.mp3")
            self.assertEqual(normalized, "music/artist/song.mp3")
            self.assertEqual(validated, track.resolve())
            with self.assertRaises(ValueError):
                playback.validate_media_path(root, "../outside.mp3")

    def test_statistics_reject_nonmedia_roots_extensions_and_invalid_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ("lyrics/song.lrc", "music/song.mp3", "music/artist/song.lrc",
                         "music/artist/.song.mp3", "vido/artist/song.mp3", "music/artist/sub/deep/song.mp3"):
                file = root / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(b"data")
                with self.subTest(path=path), self.assertRaises(ValueError):
                    playback.validate_media_path(root, path)

    def test_session_ids_are_canonical_uuids(self):
        value = playback.normalize_session_id("D8088F10-4238-4A62-96F8-F5DD9C981FC1")
        self.assertEqual(value, "d8088f10-4238-4a62-96f8-f5dd9c981fc1")
        with self.assertRaises(ValueError):
            playback.normalize_session_id("not-a-session")

    def test_database_constraint_migration_preserves_rows_and_expands_the_ceiling(self):
        source = (Path(__file__).resolve().parents[1] / "app" / "core" / "db.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("CHECK (preference BETWEEN -7 AND 500)", source)
        self.assertIn("preference SMALLINT", source)
        self.assertIn("DROP CHECK chk_media_preference", source)
        self.assertNotIn("UPDATE media_playback_stats", source)


class PlaybackIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def _record(self, insert_rowcount):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "music" / "artist").mkdir(parents=True)
        (root / "music" / "artist" / "song.mp3").write_bytes(b"ID3")
        connection = _PlaybackConnection(insert_rowcount)
        with (
            patch.object(playback, "engine", _Engine(connection)),
            patch.object(
                playback.media_objects,
                "ensure_object",
                new=AsyncMock(return_value="stable-media-id"),
            ),
        ):
            result = await playback.record_playback(
                root,
                "music/artist/song.mp3",
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

    async def test_admin_preference_change_and_audit_share_the_business_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            track = root / "music" / "artist" / "song.mp3"
            track.parent.mkdir(parents=True)
            track.write_bytes(b"ID3")
            connection = _PlaybackConnection(insert_rowcount=0)
            audit = AsyncMock()
            with (
                patch.object(playback, "engine", _Engine(connection)),
                patch.object(playback.media_objects, "ensure_object",
                             new=AsyncMock(return_value="stable-media-id")),
            ):
                result = await playback.set_preference(
                    root, "music/artist/song.mp3", 300, audit=audit,
                )

        self.assertEqual(result["preference"], 300)
        audit.assert_awaited_once()
        self.assertIs(audit.await_args.args[0], connection)
        self.assertEqual(audit.await_args.args[1:3], ("success", 1))


if __name__ == "__main__":
    unittest.main()
