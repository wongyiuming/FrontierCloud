import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            (root / "music").mkdir()
            (root / "music" / "song.mp3").write_bytes(b"ID3")
            playback_connection = _PlaybackConnection()
            fake_engine = _Engine([_CleanupFailureConnection(), playback_connection])
            with (
                patch.object(playback, "engine", fake_engine),
                patch.object(playback, "append_admin_log"),
            ):
                result = await playback.record_playback(
                    root,
                    "music/song.mp3",
                    "d8088f10-4238-4a62-96f8-f5dd9c981fc1",
                    played_seconds=20,
                    duration=40,
                )

        self.assertTrue(result["counted"])
        self.assertEqual(fake_engine.begin_count, 2)
        exact_delete = playback_connection.executed[0]
        self.assertIn("playback_session_id=:session_id", exact_delete[0])
        self.assertIn("media_id=:media_id", exact_delete[0])

    async def test_expired_current_event_is_deleted_in_accounting_transaction(self):
        source = __import__("inspect").getsource(playback.record_playback)
        delete_at = source.index("playback_session_id=:session_id")
        insert_at = source.index("INSERT IGNORE INTO media_playback_events")
        self.assertLess(delete_at, insert_at)
        self.assertIn("AND expires_at <= :now", source)


if __name__ == "__main__":
    unittest.main()
