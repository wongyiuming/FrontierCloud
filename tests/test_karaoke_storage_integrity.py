import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.services import karaoke_storage, resource_pool
from app.services.federation.state import state


class _Result:
    def __init__(self, member=None):
        self.member = member

    def mappings(self):
        return self

    def first(self):
        return self.member


class _Connection:
    def __init__(self, database, transaction):
        self.database = database
        self.transaction = transaction

    async def execute(self, statement, *args, **kwargs):
        kind = statement.__class__.__name__
        if kind == "Select":
            return _Result(dict(self.database.member))
        if kind == "Update":
            self.database.updates.append(self.transaction)
            if self.database.fail_update_transaction == self.transaction:
                raise RuntimeError("injected storage counter failure")
            return _Result()
        return _Result()


class _Begin:
    def __init__(self, database):
        self.database = database
        self.transaction = 0

    async def __aenter__(self):
        self.database.transactions += 1
        self.transaction = self.database.transactions
        return _Connection(self.database, self.transaction)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Database:
    def __init__(self, *, fail_update_transaction=None):
        self.transactions = 0
        self.updates = []
        self.fail_update_transaction = fail_update_transaction
        self.member = {
            "allocated_bytes": 4 * 1024 ** 3,
            "used_bytes": 0,
            "reserved_bytes": 0,
            "storage_enabled": 1,
            "writable": 1,
        }

    def begin(self):
        return _Begin(self)


class _Request:
    def __init__(self, payload=b""):
        self.payload = payload

    async def stream(self):
        yield self.payload


class KaraokeStorageIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        karaoke_storage._usage_cache.clear()
        self.relationship = "a" * 32
        self.user = "b" * 32
        self.recording = "c" * 32
        self.relation = {
            "relationship_id": self.relationship,
            "allocated_capacity_bytes": 4 * 1024 ** 3,
        }
        self.value = {
            "size": 5,
            "u": self.user,
            "i": self.recording,
        }

    async def test_existing_target_releases_follower_reservation(self):
        database = _Database()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(karaoke_storage, "ROOT", Path(directory).resolve()), \
             patch.object(state, "node", {"role": "Follower", "node_id": "d" * 32}), \
             patch.object(state, "database", database), \
             patch.object(resource_pool, "physical_free", return_value=8 * 1024 ** 3):
            target = karaoke_storage._path(self.relationship, self.user, self.recording)
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old")
            with self.assertRaises(HTTPException) as raised:
                await karaoke_storage.receive(_Request(b"new!!"), self.relation, self.value)
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(database.updates, [1, 2])
            self.assertEqual(target.read_bytes(), b"old")

    async def test_database_failure_after_publish_removes_recording_and_releases_reservation(self):
        database = _Database(fail_update_transaction=2)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(karaoke_storage, "ROOT", Path(directory).resolve()), \
             patch.object(state, "node", {"role": "Follower", "node_id": "d" * 32}), \
             patch.object(state, "database", database), \
             patch.object(resource_pool, "physical_free", return_value=8 * 1024 ** 3):
            target = karaoke_storage._path(self.relationship, self.user, self.recording)
            with self.assertRaises(RuntimeError):
                await karaoke_storage.receive(_Request(b"voice"), self.relation, self.value)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".part").exists())
            self.assertEqual(database.updates, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
