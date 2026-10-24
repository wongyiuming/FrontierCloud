import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import insert,select

from app.core import db
from app.core.sqlite_compat import create_sqlite_engine
from app.services import karaoke_storage, resource_pool
from app.services.federation import schema as fs
from app.services.federation.state import state


class _Connection:
    def __init__(self, database, transaction,connection):
        self.database = database
        self.transaction = transaction
        self.connection=connection

    async def scalar(self,statement,*args,**kwargs):
        return await self.connection.scalar(statement,*args,**kwargs)

    async def execute(self, statement, *args, **kwargs):
        kind = statement.__class__.__name__
        if kind == "Update":
            self.database.updates.append(self.transaction)
            if self.database.fail_update_transaction == self.transaction:
                raise RuntimeError("injected storage counter failure")
        return await self.connection.execute(statement,*args,**kwargs)


class _Begin:
    def __init__(self, database):
        self.database = database
        self.transaction = 0

    async def __aenter__(self):
        self.database.transactions += 1
        self.transaction = self.database.transactions
        self.context=self.database.engine.begin()
        return _Connection(self.database, self.transaction,await self.context.__aenter__())

    async def __aexit__(self, exc_type, exc, tb):
        return await self.context.__aexit__(exc_type,exc,tb)


class _Database:
    def __init__(self,engine, *, fail_update_transaction=None):
        self.engine=engine
        self.transactions = 0
        self.updates = []
        self.fail_update_transaction = fail_update_transaction

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

    async def database(self,*,fail_update_transaction=None):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);engine=create_sqlite_engine('sqlite+aiosqlite:///'+str(root/'state.db'))
        self.addAsyncCleanup(engine.dispose)
        with patch.object(db,'engine',engine),patch.object(db,'SECRET_DIR',root/'unused-secrets'):await db.init_db()
        async with engine.begin() as conn:
            await conn.execute(insert(fs.identity).values(singleton=1,node_id='d'*32,role='Follower',endpoint='https://storage.4399.com',private_key='unused-by-storage-counters',created_at=1))
            await conn.execute(insert(fs.storage_members).values(member_id='d'*32,relationship_id=self.relationship,
                member_kind='Follower',transport='Direct',storage_enabled=1,allocated_bytes=4*1024**3,used_bytes=0,reserved_bytes=0,
                physical_free_bytes=8*1024**3,health='online',writable=1,updated_at=1))
        return _Database(engine,fail_update_transaction=fail_update_transaction)

    async def reservation_released(self,database):
        async with database.engine.connect() as conn:
            self.assertEqual(tuple((await conn.execute(select(fs.storage_members.c.used_bytes,fs.storage_members.c.reserved_bytes))).one()),(0,0))

    async def test_existing_target_releases_follower_reservation(self):
        database = await self.database()
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
            await self.reservation_released(database)

    async def test_database_failure_after_publish_removes_recording_and_releases_reservation(self):
        database = await self.database(fail_update_transaction=2)
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
            await self.reservation_released(database)

    async def test_active_recording_lease_blocks_cleanup_even_when_part_is_old(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(karaoke_storage, "ROOT", Path(directory).resolve()):
            target = karaoke_storage._path(self.relationship, self.user, self.recording)
            target.parent.mkdir(parents=True)
            temporary = target.with_suffix(".part")
            temporary.write_bytes(b"active writer")
            with karaoke_storage.recording_lease(self.relationship, self.user, self.recording):
                with self.assertRaises(HTTPException) as raised:
                    karaoke_storage.remove(self.relationship, self.user, self.recording)
                self.assertEqual(raised.exception.status_code, 409)
                self.assertTrue(temporary.exists())
            self.assertEqual(karaoke_storage.remove(self.relationship, self.user, self.recording), 0)
            self.assertFalse(temporary.exists())

    async def test_user_cleanup_acquires_all_leases_before_removing_any_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(karaoke_storage, "ROOT", Path(directory).resolve()):
            first = karaoke_storage._path(self.relationship, self.user, "a" * 32)
            active = karaoke_storage._path(self.relationship, self.user, "e" * 32)
            first.parent.mkdir(parents=True)
            first.write_bytes(b"retained")
            active.with_suffix(".part").write_bytes(b"active")
            with karaoke_storage.recording_lease(self.relationship, self.user, "e" * 32):
                with self.assertRaises(HTTPException):
                    karaoke_storage.remove_user(self.relationship, self.user)
                self.assertEqual(first.read_bytes(), b"retained")
                self.assertTrue(active.with_suffix(".part").exists())
            self.assertEqual(karaoke_storage.remove_user(self.relationship, self.user), len(b"retained"))


if __name__ == "__main__":
    unittest.main()
