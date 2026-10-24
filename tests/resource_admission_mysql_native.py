"""Actual disposable MySQL Master/native preparation and rollback transactions."""
import asyncio
import json
import os
import re
import time
import uuid
from unittest.mock import patch

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.services import resource_migration_schema as s
from app.services.federation import schema as fs
from app.services.resource_plan_store import PlanError
from tests import test_resource_migration_admission as fixtures
from tests import test_resource_migration_planning as planning


async def main():
    url=make_url(os.environ['FC_DB_MYSQL_URL'])
    if (os.environ.get('FC_DB_MYSQL_RUNTIME')!='1' or url.host!='mysql' or url.database!='frontiercloud_db_test'):
        raise RuntimeError('Refusing outside disposable mysql/frontiercloud_db_test')
    started=time.monotonic();checks=[];admin=create_async_engine(url,pool_size=2,max_overflow=0)
    async def run(method):
        prefix='fc_admission_'+uuid.uuid4().hex[:12]
        names=[prefix+'_'+name for name in ('master','source','target')]
        async with admin.begin() as conn:
            for name in names:
                if not re.fullmatch(r'fc_admission_[a-f0-9]{12}_(master|source|target)',name):raise RuntimeError('unsafe fixture schema')
                await conn.execute(text(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin'))
        remaining=iter(names)
        def factory(_url):return create_async_engine(url.set(database=next(remaining)),pool_size=3,max_overflow=0)
        fixture=fixtures.MigrationAdmissionTests('runTest');ready=False
        try:
            with patch.object(planning,'create_sqlite_engine',side_effect=factory):
                await fixture.asyncSetUp();ready=True
            await method(fixture)
        finally:
            if ready:await fixture.asyncTearDown()
            async with admin.begin() as conn:
                for name in names:await conn.execute(text(f'DROP DATABASE `{name}`'))
    try:
        methods=('test_whole_parent_preparation_never_authorizes_transfer_or_opens_keys',
            'test_lost_begin_reply_retries_same_native_authority_after_new_coordinator',
            'test_failed_final_abort_audit_rolls_back_master_only_and_retries',
            'test_restart_after_incomplete_final_scan_rechecks_without_growing_spool')
        for name in methods:
            await run(getattr(fixtures.MigrationAdmissionTests,name));checks.append(name)
            print(json.dumps(dict(passed=name)),flush=True)
        async def concurrent(fixture):
            await fixture.preview();f=fixture.f;task=None
            try:
                async with f.master.database.begin() as conn:
                    await f.master.store.lock(conn)
                    await conn.execute(insert(fs.upload_sessions).values(upload_id='1'*32,storage_member_id=f.target.store.node['node_id'],
                        media_id='2'*64,media_path='music/free/existing.mp3',path_locator='3'*64,object_kind='audio',expected_bytes=50,
                        content_encrypted=0,state='reserved',expires_at=int(time.time())+1800,created_at=1,updated_at=1))
                    await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==f.target.store.node['node_id']).values(reserved_bytes=50))
                    task=asyncio.create_task(fixture.admission._create(fixture.plan['plan_id'],fixture.plan['review_sha256'],'native'))
                    await asyncio.sleep(.1)
                    if task.done():raise AssertionError('admission did not wait for identity row')
                    checks.append('master_admission_waits_for_independent_writer')
                try:await asyncio.wait_for(task,5)
                except PlanError:checks.append('committed_writer_refuses_master_admission')
                else:raise AssertionError('master admitted over upload reservation')
                async with f.master.database.connect() as conn:
                    if await conn.scalar(select(func.count()).select_from(s.tasks)):raise AssertionError('refused admission left task')
                    if (await fixture.quota())[0]!=50:raise AssertionError('refused admission altered writer quota')
                    checks.append('refusal_preserves_writer_and_has_no_task')
                # Fixture-owned setup session has no file or running writer.
                async with f.master.database.begin() as conn:
                    await conn.execute(update(fs.upload_sessions).where(fs.upload_sessions.c.upload_id=='1'*32).values(state='cancelled',path_locator=None))
                    await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==f.target.store.node['node_id']).values(reserved_bytes=0))
                await fixture.prepare();await fixture.admission.cancel_unstarted(fixture.plan['plan_id'],'native')
                if await fixture.quota()!=(0,0):raise AssertionError('controlled abort left quota')
                checks.append('admission_and_controlled_abort_after_writer_settlement')
            finally:
                if task and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        await run(concurrent)
        print(json.dumps(dict(format='frontiercloud-python-mysql-master-admission',version=1,count=len(checks),checks=checks,
            actual_mysql=True,separate_node_databases=True,execution_authorized=False,elapsed_seconds=round(time.monotonic()-started,4)),sort_keys=True))
    finally:await admin.dispose()


if __name__=='__main__':asyncio.run(main())
