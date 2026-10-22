"""Real MySQL cancellation transactions, owned inode cleanup and lost acknowledgements."""
import asyncio
import json
import os
import re
import time
import uuid
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests import test_resource_transfer_cancel as fixtures,test_resource_migration_planning as planning


async def main():
    url=make_url(os.environ['FC_DB_MYSQL_URL'])
    if (os.environ.get('FC_DB_MYSQL_RUNTIME')!='1' or url.host!='mysql' or url.database!='frontiercloud_db_test'):
        raise RuntimeError('Refusing outside disposable mysql/frontiercloud_db_test')
    started=time.monotonic();admin=create_async_engine(url,pool_size=2,max_overflow=0);checks=[]
    methods=('test_partial_cancel_cleans_only_owned_stage_and_settles_all_reservations',
        'test_unlink_then_lost_audit_ack_retries_absence_without_releasing_quota_early',
        'test_final_master_audit_failure_rolls_back_only_its_settlement',
        'test_unrelated_reserved_capacity_and_playback_readers_are_preserved')
    try:
        for method in methods:
            prefix='fc_cancel_'+uuid.uuid4().hex[:12];names=[prefix+'_'+name for name in ('master','source','target')]
            async with admin.begin() as conn:
                for name in names:
                    if not re.fullmatch(r'fc_cancel_[a-f0-9]{12}_(master|source|target)',name):raise RuntimeError('unsafe fixture database')
                    await conn.execute(text(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin'))
            remaining=iter(names)
            def factory(_url):return create_async_engine(url.set(database=next(remaining)),pool_size=3,max_overflow=0)
            fixture=fixtures.TransferCancellationTests('runTest');ready=False
            try:
                with patch.object(planning,'create_sqlite_engine',side_effect=factory):await fixture.asyncSetUp();ready=True
                await getattr(fixture,method)();checks.append(method);print(json.dumps(dict(passed=method)),flush=True)
            finally:
                if ready:await fixture.asyncTearDown()
                async with admin.begin() as conn:
                    for name in names:await conn.execute(text(f'DROP DATABASE `{name}`'))
        print(json.dumps(dict(format='frontiercloud-python-transfer-cancel-mysql',version=1,count=len(checks),checks=checks,
            actual_mysql=True,separate_node_databases=True,source_files_removed=False,elapsed_seconds=round(time.monotonic()-started,4)),sort_keys=True))
    finally:await admin.dispose()


if __name__=='__main__':asyncio.run(main())
