import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy.exc import SQLAlchemyError

from app.services import playback


class _Result:
    def __init__(self, rowcount=0, row=None):
        self.rowcount = rowcount
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class _CleanupFailureConnection:
    async def execute(self, *_args, **_kwargs):
        raise SQLAlchemyError("cleanup lock timeout")


class _PlaybackConnection:
    def __init__(self):
        self.executed = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append((sql, params or {}))
        if "INSERT IGNORE INTO media_playback_events" in sql:
            return _Result(rowcount=1)
        if "SELECT play_score" in sql:
            return _Result(row={"play_score": 1, "preference": 0})
        return _Result()


class _Context:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class _Engine:
    def __init__(self, connections):
        self.connections = list(connections)
        self.begin_count = 0

    def begin(self):
        connection = self.connections[min(self.begin_count, len(self.connections) - 1)]
        self.begin_count += 1
        return _Context(connection)


class PlaybackTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_housekeeping_failure_does_not_rollback_valid_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "music" / "artist").mkdir(parents=True)
            (root / "music" / "artist" / "song.mp3").write_bytes(b"ID3")
            playback_connection = _PlaybackConnection()
            fake_engine = _Engine([_CleanupFailureConnection(), playback_connection])
            with (
                patch.object(playback, "engine", fake_engine),
                patch.object(playback, "_next_cleanup_at", 0.0),
                patch.object(playback, "append_admin_log"),
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

        self.assertTrue(result["counted"])
        self.assertEqual(fake_engine.begin_count, 2)
        self.assertFalse(any("DELETE FROM media_playback_events" in sql for sql, _ in playback_connection.executed))

    async def test_expired_current_event_is_deleted_in_accounting_transaction(self):
        class ExpiredConnection(_PlaybackConnection):
            inserts = 0

            async def execute(self, statement, params=None):
                sql = str(statement)
                result = await super().execute(statement, params)
                if "INSERT IGNORE INTO media_playback_events" in sql:
                    self.inserts += 1
                    return _Result(rowcount=0 if self.inserts == 1 else 1)
                if "DELETE FROM media_playback_events" in sql:
                    return _Result(rowcount=1)
                return result

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            track = root / "music/artist/song.mp3"
            track.parent.mkdir(parents=True)
            track.write_bytes(b"ID3")
            conn = ExpiredConnection()
            with patch.object(playback, "engine", _Engine([conn])), \
                 patch.object(playback, "_cleanup_expired_events", new=AsyncMock()), \
                 patch.object(playback.media_objects, "ensure_object", new=AsyncMock(return_value="a" * 64)):
                result = await playback.record_playback(root, "music/artist/song.mp3",
                    "d8088f10-4238-4a62-96f8-f5dd9c981fc1", 30, 60)
        self.assertTrue(result["counted"])
        sequence = [sql for sql, _ in conn.executed if "media_playback_events" in sql]
        self.assertEqual(len(sequence), 3)
        self.assertIn("INSERT IGNORE", sequence[0])
        self.assertIn("expires_at <= :now", sequence[1])
        self.assertIn("INSERT IGNORE", sequence[2])

    async def test_expiry_housekeeping_is_reserved_once_per_thirty_seconds(self):
        conn = _PlaybackConnection()
        fake_engine = _Engine([conn])
        with patch.object(playback, "engine", fake_engine), patch.object(playback, "_next_cleanup_at", 0.0), \
             patch.object(playback.time, "monotonic", side_effect=[100.0, 100.1, 129.9, 130.0]):
            for _ in range(4):
                await playback._cleanup_expired_events(playback._utcnow())
        self.assertEqual(fake_engine.begin_count, 2)


if __name__ == "__main__":
    unittest.main()
