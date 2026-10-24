"""Disposable real MySQL native preparation and concurrent node-row admission."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import db
from app.services import resource_fences, resource_migration_schema as s
from app.services.federation import protocol as p, schema as fs
from app.services.federation.state import State
from app.services.resource_peer_reservations import INITIAL_UNITS_SHA256, NativeReservations, ReservationError, unit_transcript
from app.services.resource_observation import digest


async def main():
    url=make_url(os.environ['FC_DB_MYSQL_URL'])
    if (os.environ.get('FC_DB_MYSQL_RUNTIME')!='1' or url.host!='mysql' or url.database!='frontiercloud_db_test'):
        raise RuntimeError('Refusing native reservation test outside disposable mysql/frontiercloud_db_test')
    started=time.monotonic();checks=[];database=create_async_engine(url,pool_size=3,max_overflow=0)
    def check(name,value):
        if not value:raise AssertionError(name)
        checks.append(name);print(json.dumps(dict(passed=name)),flush=True)
    job=None
    try:
        async with database.connect() as conn:
            for name in await db._table_names(conn):
                if not name.replace('_','').isalnum():raise RuntimeError('unsafe fixture table')
                await conn.execute(text(f'DROP TABLE `{name}`'));await conn.commit()
        with patch.object(db,'engine',database):await db.init_db()
        key=Fernet.generate_key();store=State(database,key);await store.initialize()
        own=store.node['node_id'];master,source,relationship,source_rel=[char*32 for char in 'abcd']
        async with database.begin() as conn:
            await conn.execute(update(fs.identity).values(role='Follower',endpoint='https://target.4399.com'))
            await conn.execute(insert(fs.relationships).values(relationship_id=relationship,peer_id=master,
                peer_endpoint='https://master.4399.com',peer_key=p.public_key(p.new_key()),credential=store.seal(p.encode(os.urandom(32))),
                direction='upstream',mode='Direct',state='active',status='online',last_heartbeat=int(time.time()),rtt_ms=1,
                failures=0,recoveries=0,peer_version=p.APP_VERSION,protocol=p.PROTOCOL_VERSION,summary={},created_at=1))
            await conn.execute(insert(fs.storage_members).values(member_id=own,relationship_id=None,member_kind='Follower',transport='Local',
                storage_enabled=1,allocated_bytes=8*1024**3,used_bytes=0,reserved_bytes=0,physical_free_bytes=16*1024**3,health='online',writable=1,updated_at=1))
        await store.initialize();relation=await store.relationship(relationship)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);media=root/'media';recordings=root/'recordings';media.mkdir();recordings.mkdir()
            untouched=media/'unrelated-original';untouched.write_bytes(b'keep original bytes')
            service=NativeReservations(store,root_provider=lambda:dict(media=media,recordings=recordings))
            unit=dict(unit_id=digest(['media',source,'music/album']),resource_kind='media',source_parent='music/album',target_parent='music/album',
                source_version='f'*64,object_count=1,total_bytes=123)
            request=dict(task_id=uuid.uuid4().hex,master_id=master,source_id=source,target_id=own,source_relationship=source_rel,
                target_relationship=relationship,side='target',plan_sha256='e'*64,unit_count=1,object_count=1,total_bytes=123,
                units_sha256=unit_transcript(INITIAL_UNITS_SHA256,unit))
            args=(store.node,relation)
            # An independent transaction already owns the native identity row.
            # Begin must wait and subsequently observe its committed writer.
            async with database.begin() as conn:
                await store.lock(conn)
                await conn.execute(insert(fs.upload_sessions).values(upload_id='1'*32,storage_member_id=own,media_id='2'*64,
                    media_path='music/free/existing.mp3',path_locator=digest('existing'),object_kind='audio',expected_bytes=50,
                    content_encrypted=0,state='reserved',expires_at=int(time.time())+1800,created_at=1,updated_at=1))
                await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==own).values(reserved_bytes=50))
                job=asyncio.create_task(service.begin(*args,uuid.uuid4().hex,request))
                await asyncio.sleep(.1)
                check('waits_for_independent_node_row_writer',not job.done())
            try:await asyncio.wait_for(job,5)
            except ReservationError as error:check('sees_committed_writer_before_reserving','writer_pending' in str(error))
            else:raise AssertionError('admitted_over_existing_writer')
            async with database.begin() as conn:
                check('refused_prepare_preserves_writer_quota',await conn.scalar(select(fs.storage_members.c.reserved_bytes))==50)
                check('refused_prepare_has_no_native_authority',await conn.scalar(select(func.count()).select_from(s.peer_reservations))==0)
                # Test-owned setup session has no data-plane writer or file.
                await conn.execute(update(fs.upload_sessions).values(state='cancelled',path_locator=None))
                await conn.execute(update(fs.storage_members).values(reserved_bytes=0))
            first=await service.begin(*args,uuid.uuid4().hex,request)
            retry=await service.begin(*args,uuid.uuid4().hex,request)
            check('idempotent_mysql_prepare_keeps_revision',first['revision']==retry['revision'])
            await service.add_unit(*args,uuid.uuid4().hex,request['task_id'],unit)
            ready=await service.seal(*args,uuid.uuid4().hex,request['task_id'])
            check('complete_parent_is_prepared_without_copy_authority',ready['state']=='prepared' and not ready['execution_authorized'])
            async with database.begin() as conn:
                try:await resource_fences.new_upload(conn,own,'media','music/album/new.mp3')
                except resource_fences.FenceConflict:check('mysql_scope_stops_selected_upload',True)
                else:raise AssertionError('missing_native_scope')
                await resource_fences.new_upload(conn,own,'media','music/free/new.mp3')
                check('mysql_scope_keeps_other_parent_writable',True)
                check('mysql_capacity_reserved_once',await conn.scalar(select(fs.storage_members.c.reserved_bytes))==123)
            with patch.object(store,'log',side_effect=RuntimeError('abort audit fault')):
                try:await service.abort_unstarted(*args,uuid.uuid4().hex,request['task_id'])
                except RuntimeError:pass
                else:raise AssertionError('audit_failure_not_applied')
            async with database.connect() as conn:
                check('failed_abort_audit_keeps_all_authority_and_quota',
                    await conn.scalar(select(fs.storage_members.c.reserved_bytes))==123 and
                    await conn.scalar(select(func.count()).select_from(s.scopes))==1 and
                    await conn.scalar(select(s.peer_reservations.c.state))=='prepared')
            reloaded=State(database,key);await reloaded.initialize()
            again=await NativeReservations(reloaded).status(reloaded.node,relation,uuid.uuid4().hex,request['task_id'])
            check('fresh_store_loads_same_json_binding_and_preparation',again['state']=='prepared' and again['binding_sha256']==digest(request))
            async with database.begin() as conn:
                try:await resource_fences.node_clear(conn)
                except resource_fences.FenceConflict:check('prepared_native_identity_is_fenced',True)
                else:raise AssertionError('native_reset_not_fenced')
            await service.abort_unstarted(*args,uuid.uuid4().hex,request['task_id'])
            async with database.begin() as conn:
                await resource_fences.node_clear(conn)
                check('controlled_abort_settles_scope_and_capacity',await conn.scalar(select(fs.storage_members.c.reserved_bytes))==0 and
                    await conn.scalar(select(func.count()).select_from(s.scopes))==0 and
                    await conn.scalar(select(func.count()).select_from(s.capacity))==0)
            terminal=await service.begin(*args,uuid.uuid4().hex,request)
            check('old_request_cannot_revive_aborted_preparation',terminal['state']=='aborted')
            check('unrelated_native_bytes_unchanged',untouched.read_bytes()==b'keep original bytes')
        print(json.dumps(dict(format='frontiercloud-python-mysql-native-reservation',version=1,checks=checks,count=len(checks),
            seconds=round(time.monotonic()-started,4),execution_authorized=False)))
    finally:
        if job is not None and not job.done():job.cancel();await asyncio.gather(job,return_exceptions=True)
        await database.dispose()


if __name__=='__main__':asyncio.run(main())
