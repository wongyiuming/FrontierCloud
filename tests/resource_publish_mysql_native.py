"""Real MySQL publication journals, inode reconciliation and lost acknowledgements."""
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

from tests import test_resource_publish as fixtures,test_resource_migration_planning as planning
from tests import test_resource_source_verify as source_fixtures


async def main():
    url=make_url(os.environ['FC_DB_MYSQL_URL'])
    if (os.environ.get('FC_DB_MYSQL_RUNTIME')!='1' or url.host!='mysql' or url.database!='frontiercloud_db_test'):
        raise RuntimeError('Refusing outside disposable mysql/frontiercloud_db_test')
    started=time.monotonic();admin=create_async_engine(url,pool_size=2,max_overflow=0);checks=[]
    methods=[(fixtures.PublicationTests,method) for method in ('test_whole_plain_cipher_parent_publishes_without_copying_payload_or_switching_master',
        'test_link_then_unlink_failure_resumes_same_inode_without_payload_copy',
        'test_directory_move_then_lost_sql_ack_reconciles_existing_target',
        'test_new_receiver_resumes_registration_and_preserves_original_encryption',
        'test_lost_master_reply_retries_target_receipt_with_fresh_file_check')]
    methods.extend((source_fixtures.SourceVerificationTests,method) for method in (
        'test_plain_cipher_roster_and_live_metadata_verified_without_payload_hashing_or_keys',
        'test_new_source_service_restarts_roster_and_closes_old_directory',
        'test_lost_master_audit_reply_repeats_fresh_source_verification'))
    try:
        for fixture_class,method in methods:
            prefix='fc_publish_'+uuid.uuid4().hex[:12];names=[prefix+'_'+name for name in ('master','source','target')]
            async with admin.begin() as conn:
                for name in names:
                    if not re.fullmatch(r'fc_publish_[a-f0-9]{12}_(master|source|target)',name):raise RuntimeError('unsafe fixture database')
                    await conn.execute(text(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin'))
            remaining=iter(names)
            def factory(_url):return create_async_engine(url.set(database=next(remaining)),pool_size=3,max_overflow=0)
            fixture=fixture_class('runTest');ready=False
            try:
                with patch.object(planning,'create_sqlite_engine',side_effect=factory):await fixture.asyncSetUp();ready=True
                await getattr(fixture,method)();checks.append(method);print(json.dumps(dict(passed=method)),flush=True)
            finally:
                if ready:await fixture.asyncTearDown()
                async with admin.begin() as conn:
                    for name in names:await conn.execute(text(f'DROP DATABASE `{name}`'))
        print(json.dumps(dict(format='frontiercloud-python-target-publication-mysql',version=1,count=len(checks),checks=checks,
            actual_mysql=True,separate_node_databases=True,global_cutover=False,capacity_settled=False,
            elapsed_seconds=round(time.monotonic()-started,4)),sort_keys=True))
    finally:await admin.dispose()


if __name__=='__main__':asyncio.run(main())
