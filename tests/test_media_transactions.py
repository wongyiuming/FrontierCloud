import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile
from sqlalchemy.exc import SQLAlchemyError

from app.services import media_manager


class _Result:
    lastrowid = 1


class _Connection:
    def __init__(self, failure=None):
        self.failure = failure
        self.executed = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append((sql, params or {}))
        if self.failure is not None and "DELETE events" in sql:
            raise self.failure
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


class MediaDeleteTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        invalidation = patch.object(media_manager, "invalidate_media_catalog", new=AsyncMock())
        self.invalidate_catalog = invalidation.start()
        self.addCleanup(invalidation.stop)

    async def test_repeated_cancellation_waits_for_media_reconciliation(self):
        started = asyncio.Event()
        finish = asyncio.Event()
        restored = []

        async def cleanup():
            started.set()
            await finish.wait()
            restored.append(True)

        cleanup_task = asyncio.create_task(cleanup())
        waiter = asyncio.create_task(media_manager._finish_media_cleanup(cleanup_task))
        await started.wait()
        waiter.cancel()
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        self.assertFalse(cleanup_task.cancelled())
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(restored, [True])

    def _tree(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        album = root / "music" / "album"
        album.mkdir(parents=True)
        track = album / "song.mp3"
        track.write_bytes(b"ID3track")
        return root, album, track

    async def test_parent_child_selection_is_one_atomic_delete(self):
        root, album, track = self._tree()
        connection = _Connection()
        with (
            patch.object(media_manager, "MEDIA_ROOT", root),
            patch.object(media_manager, "engine", _Engine(connection)),
        ):
            deleted = await media_manager.MediaManager.delete([
                "music/album",
                "music/album/song.mp3",
            ])

        self.assertEqual(deleted, 1)
        self.invalidate_catalog.assert_awaited_once()
        self.assertFalse(album.exists())
        self.assertFalse(track.exists())
        self.assertEqual(list(root.glob(media_manager.DELETE_QUARANTINE_PREFIX + "*")), [])
        sql = "\n".join(statement for statement, _params in connection.executed)
        self.assertIn("DELETE events", sql)
        self.assertIn("DELETE FROM media_playback_stats", sql)
        self.assertIn("DELETE FROM media_visibility", sql)

    async def test_database_failure_restores_every_staged_object(self):
        root, album, track = self._tree()
        connection = _Connection(SQLAlchemyError("metadata failure"))
        with (
            patch.object(media_manager, "MEDIA_ROOT", root),
            patch.object(media_manager, "engine", _Engine(connection)),
            patch.object(
                media_manager.MediaManager,
                "_journal_state",
                new=AsyncMock(return_value="pending"),
            ),
        ):
            with self.assertRaises(SQLAlchemyError):
                await media_manager.MediaManager.delete(["music/album"])

        self.assertTrue(album.is_dir())
        self.assertEqual(track.read_bytes(), b"ID3track")
        self.assertEqual(list(root.glob(media_manager.DELETE_QUARANTINE_PREFIX + "*")), [])

    async def test_cancellation_restores_staged_files_before_propagating(self):
        root, album, track = self._tree()
        connection = _Connection(asyncio.CancelledError())
        with (
            patch.object(media_manager, "MEDIA_ROOT", root),
            patch.object(media_manager, "engine", _Engine(connection)),
            patch.object(
                media_manager.MediaManager,
                "_journal_state",
                new=AsyncMock(return_value="pending"),
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await media_manager.MediaManager.delete(["music/album"])

        self.assertTrue(album.is_dir())
        self.assertTrue(track.is_file())

    async def test_unknown_commit_state_defers_recovery_without_restoring_files(self):
        root, album, track = self._tree()
        connection = _Connection(SQLAlchemyError("commit outcome unknown"))
        delete_journal = AsyncMock()
        with (
            patch.object(media_manager, "MEDIA_ROOT", root),
            patch.object(media_manager, "engine", _Engine(connection)),
            patch.object(
                media_manager.MediaManager,
                "_journal_state",
                new=AsyncMock(side_effect=SQLAlchemyError("database unavailable")),
            ),
            patch.object(
                media_manager.MediaManager,
                "_delete_journal",
                new=delete_journal,
            ),
        ):
            with self.assertRaises(SQLAlchemyError):
                await media_manager.MediaManager.delete(["music/album"])

            with self.assertRaises(HTTPException) as blocked:
                media_manager.ensure_media_mutations_ready()
            self.assertEqual(blocked.exception.status_code, 503)

        self.assertFalse(album.exists())
        self.assertFalse(track.exists())
        quarantines = list(root.glob(media_manager.DELETE_QUARANTINE_PREFIX + "*"))
        self.assertEqual(len(quarantines), 1)
        self.assertEqual((quarantines[0] / "0" / "song.mp3").read_bytes(), b"ID3track")
        delete_journal.assert_not_awaited()

    async def test_underscore_in_path_is_not_a_sql_wildcard(self):
        root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        selected = root / "music" / "a_b"
        sibling = root / "music" / "axb"
        selected.mkdir(parents=True)
        sibling.mkdir(parents=True)
        connection = _Connection()
        with (
            patch.object(media_manager, "MEDIA_ROOT", root),
            patch.object(media_manager, "engine", _Engine(connection)),
        ):
            await media_manager.MediaManager.delete(["music/a_b"])

        self.assertFalse(selected.exists())
        self.assertTrue(sibling.is_dir())
        metadata = [
            (sql, params)
            for sql, params in connection.executed
            if "media_visibility" in sql or "media_playback_stats" in sql
        ]
        self.assertTrue(metadata)
        self.assertTrue(all(" LIKE " not in sql.upper() for sql, _params in metadata))
        self.assertTrue(any(params.get("prefix") == "music/a_b/" for _sql, params in metadata))


class FolderUploadTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_upload_does_not_leave_new_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "music").mkdir()
            upload = UploadFile(filename="ignored.wav", file=io.BytesIO(b"not a wav"))
            with patch.object(media_manager, "MEDIA_ROOT", root):
                target, name = media_manager.MediaManager.folder_upload_target(
                    "",
                    "music/new-album/song.wav",
                )
                upload.filename = name
                with self.assertRaises(HTTPException):
                    await media_manager.MediaManager.upload_one(upload, target)
            await upload.close()

            self.assertFalse((root / "music" / "new-album").exists())
            self.assertEqual(list(root.glob(".upload-*.part")), [])


class _RecoveryResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _RecoveryConnection(_Connection):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append((sql, params or {}))
        if "SELECT operation_id" in sql:
            return _RecoveryResult(self.rows)
        return _Result()


class _RecoveryEngine(_Engine):
    def connect(self):
        return _Context(self.connection)


class MediaDeleteRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_journal_restores_quarantined_media_on_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operation_id = "a" * 32
            quarantine = root / f"{media_manager.DELETE_QUARANTINE_PREFIX}{operation_id}"
            quarantine.mkdir()
            (quarantine / "0").write_bytes(b"ID3recover")
            manifest = [{
                "relative_path": "music/album/song.mp3",
                "slot": "0",
                "is_directory": False,
            }]
            connection = _RecoveryConnection([{
                "operation_id": operation_id,
                "state": "pending",
                "manifest": manifest,
            }])
            with (
                patch.object(media_manager, "MEDIA_ROOT", root),
                patch.object(media_manager, "engine", _RecoveryEngine(connection)),
            ):
                await media_manager.recover_interrupted_media_deletions()

            restored = root / "music" / "album" / "song.mp3"
            self.assertEqual(restored.read_bytes(), b"ID3recover")
            self.assertFalse(quarantine.exists())
            self.assertTrue(any(
                "DELETE FROM media_delete_operations" in sql
                for sql, _params in connection.executed
            ))

    async def test_missing_original_and_quarantine_slot_keeps_recovery_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operation_id = "b" * 32
            quarantine = root / f"{media_manager.DELETE_QUARANTINE_PREFIX}{operation_id}"
            quarantine.mkdir()
            manifest = [{
                "relative_path": "music/album/missing.mp3",
                "slot": "0",
                "is_directory": False,
            }]
            connection = _RecoveryConnection([{
                "operation_id": operation_id,
                "state": "pending",
                "manifest": manifest,
            }])
            with (
                patch.object(media_manager, "MEDIA_ROOT", root),
                patch.object(media_manager, "engine", _RecoveryEngine(connection)),
                patch.object(media_manager, "append_admin_log") as admin_log,
            ):
                with self.assertRaises(RuntimeError):
                    await media_manager.recover_interrupted_media_deletions()
                with self.assertRaises(HTTPException) as blocked:
                    media_manager.ensure_media_mutations_ready()
                self.assertEqual(blocked.exception.status_code, 503)

            self.assertTrue(quarantine.exists())
            self.assertFalse(any(
                "DELETE FROM media_delete_operations" in sql
                for sql, _params in connection.executed
            ))
            self.assertTrue(any(
                "recovery deferred" in str(call.args[0])
                for call in admin_log.call_args_list
            ))


if __name__ == "__main__":
    unittest.main()
