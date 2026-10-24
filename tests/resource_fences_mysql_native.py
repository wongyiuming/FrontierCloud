"""Actual disposable MySQL node-row serialization and resource write exclusions.

These transactions prove guard ordering, not the unfinished migration admission.
"""
import asyncio
import json
import os
import time
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import insert, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import db
from app.services import resource_fences as guards, resource_migration_schema as s
from app.services.federation import schema as fs
from app.services.federation.state import State
from app.services.resource_observation import digest


async def main():
    url = make_url(os.environ['FC_DB_MYSQL_URL'])
    if os.environ.get('FC_DB_MYSQL_RUNTIME') != '1' or url.host != 'mysql' or url.database != 'frontiercloud_db_test':
        raise RuntimeError('Refusing guard fixture outside disposable mysql/frontiercloud_db_test')
    started = time.monotonic()
    database = create_async_engine(url,pool_size=3,max_overflow=0)
    jobs = []
    try:
        with patch.object(db,'engine',database):
            await db.init_db()
        store = State(database,Fernet.generate_key())
        await store.initialize()
        member,task = 'a'*32,'b'*32
        async with database.begin() as conn:
            await conn.execute(update(fs.identity).values(role='Master'))
            await conn.execute(insert(fs.storage_members).values(member_id=member,relationship_id=None,
                member_kind='Follower',transport='Direct',storage_enabled=1,allocated_bytes=8*1024**3,
                used_bytes=0,reserved_bytes=0,physical_free_bytes=16*1024**3,health='online',writable=1,updated_at=1))
            await conn.execute(insert(s.scopes).values(scope_id=digest('scope'),task_id=task,member_id=member,
                resource_kind='media',parent_path='music/album_100%',created_at=1))
        checks = []
        async with database.begin() as conn:
            try:
                await guards.new_upload(conn,member,'media','music/album_100%/song.mp3')
            except guards.FenceConflict:
                checks.append('exact_parent_fenced')
            else:
                raise AssertionError('missing_exact_parent_guard')
            await guards.new_upload(conn,member,'media','music/albumX100Z/song.mp3')
            await guards.new_upload(conn,member,'media','music/album_100%/disc/song.mp3')
            checks.append('like_metacharacters_and_nested_parent_preserved')
            try:
                await guards.paths(conn,'media',[('music',True)])
            except guards.FenceConflict:
                checks.append('ancestor_rename_fenced')
            else:
                raise AssertionError('missing_ancestor_guard')
        async with database.begin() as conn:
            await conn.execute(insert(s.drains).values(member_id=member,owner_id=task,reason='migration',created_at=1))
        async with database.begin() as conn:
            try:
                await guards.new_upload(conn,member,'media','music/free/song.mp3')
            except guards.FenceConflict as error:
                assert error.code == 'migration_member_draining'
                checks.append('drain_rejects_new_upload')
            else:
                raise AssertionError('missing_drain_guard')
        async with database.begin() as conn:
            await conn.execute(s.drains.delete())
            await conn.execute(s.scopes.delete())
        entered,release = asyncio.Event(),asyncio.Event()
        async def ordinary_writer():
            async with database.begin() as conn:
                await guards.new_upload(conn,member,'media','music/race/song.mp3')
                entered.set()
                await release.wait()
                await conn.execute(insert(fs.upload_sessions).values(upload_id='c'*32,storage_member_id=member,
                    media_id='d'*64,media_path='music/race/song.mp3',path_locator='e'*64,object_kind='audio',
                    expected_bytes=123,state='reserved',expires_at=int(time.time())+300,created_at=1,updated_at=1))
                await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id == member).values(reserved_bytes=123))
        async def prospective_installer():
            async with database.begin() as conn:
                await guards.lock_node(conn)
                row = (await conn.execute(select(fs.upload_sessions.c.upload_id).where(
                    fs.upload_sessions.c.storage_member_id == member,fs.upload_sessions.c.state == 'reserved')
                    .limit(1).with_for_update())).first()
                return bool(row)
        writer = asyncio.create_task(ordinary_writer())
        jobs.append(writer)
        await asyncio.wait_for(entered.wait(),5)
        installer = asyncio.create_task(prospective_installer())
        jobs.append(installer)
        await asyncio.sleep(.05)
        assert not installer.done(),'node_identity_row_did_not_serialize_transactions'
        release.set()
        await asyncio.wait_for(writer,5)
        assert await asyncio.wait_for(installer,5)
        checks.append('installer_sees_committed_reservation_after_node_lock')
        async with database.begin() as conn:
            await conn.execute(insert(s.scopes).values(scope_id=digest('after-writer'),task_id=task,member_id=member,
                resource_kind='media',parent_path='music/new',created_at=1))
        async with database.begin() as conn:
            try:
                await guards.new_upload(conn,member,'media','music/new/song.mp3')
            except guards.FenceConflict:
                checks.append('writer_sees_committed_scope_after_node_lock')
            else:
                raise AssertionError('scope_not_observed')
            assert await conn.scalar(select(fs.storage_members.c.reserved_bytes).where(fs.storage_members.c.member_id == member)) == 123
        print(json.dumps(dict(format='frontiercloud-python-mysql-resource-guards',version=1,
            checks=checks,count=len(checks),elapsed_seconds=round(time.monotonic()-started,4),
            reservation_preserved=True,migration_admission_proven=False,media_transferred=False),sort_keys=True))
    finally:
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs,return_exceptions=True)
        await database.dispose()


if __name__ == '__main__':
    asyncio.run(main())
