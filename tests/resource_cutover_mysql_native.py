"""Actual MySQL owner/quota transactions and idempotent native commit journals."""
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

from tests import test_resource_cutover as fixtures,test_resource_migration_planning as planning
from tests import test_resource_migration_cache as cache_fixtures
from tests import test_resource_readers as reader_fixtures


def cases():
    methods=[(fixtures.CutoverTests,method) for method in ('test_atomic_owner_and_exact_master_quota_keep_live_business_and_crypto_facts',
        'test_recording_parent_rebind_preserves_opaque_footer_and_current_master_business',
        'test_lost_native_reply_and_new_runner_do_not_charge_target_twice',
        'test_final_audit_failure_rolls_back_all_owner_and_quota_updates',
        'test_native_audit_failure_rolls_back_capacity_then_retry_succeeds',
        'test_unrelated_target_reservations_and_usage_are_preserved')]
    methods.extend((cache_fixtures.MigrationCacheTests,method) for method in (
        'test_committed_ownership_changes_namespace_without_redis_generation_write',
        'test_native_acknowledgement_progress_does_not_discard_warm_target_catalog'))
    methods.extend((reader_fixtures.ReaderLeaseTests,method) for method in (
        'test_authenticated_get_and_head_keep_durable_leases_after_asgi_returns',
        'test_recording_stream_and_download_protect_the_native_relationship_namespace',
        'test_expired_reap_is_bounded_and_keeps_live_migration_and_other_member_leases',
        'test_cleanup_intent_blocks_new_read_but_keeps_the_original_and_existing_lease',
        'test_released_historical_cleanup_does_not_block_a_later_round_trip',
        'test_symlink_hardlink_and_missing_resources_never_create_a_lease',
        'test_relationship_and_registry_changes_after_authorization_are_revalidated',
        'test_capability_expiring_while_file_is_opened_rolls_back_the_lease',
        'test_cleanup_fence_waits_until_read_lease_is_committed'))
    return methods


async def run_cases(methods,*,scope='cutover-cache-readers',source_cleanup=False,finalization=False):
    url=make_url(os.environ['FC_DB_MYSQL_URL'])
    if (os.environ.get('FC_DB_MYSQL_RUNTIME')!='1' or url.host!='mysql' or url.database!='frontiercloud_db_test'):
        raise RuntimeError('Refusing outside disposable mysql/frontiercloud_db_test')
    started=time.monotonic();admin=create_async_engine(url,pool_size=2,max_overflow=0);checks=[]
    try:
        for fixture_class,method in methods:
            prefix='fc_cutover_'+uuid.uuid4().hex[:12];names=[prefix+'_'+name for name in ('master','source','target')]
            async with admin.begin() as conn:
                for name in names:
                    if not re.fullmatch(r'fc_cutover_[a-f0-9]{12}_(master|source|target)',name):raise RuntimeError('unsafe fixture database')
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
        print(json.dumps(dict(format='frontiercloud-python-finalization-mysql' if finalization else
            'frontiercloud-python-source-cleanup-mysql' if source_cleanup else 'frontiercloud-python-resource-cutover-mysql',
            version=1,count=len(checks),checks=checks,actual_mysql=True,separate_node_databases=True,source_files_deleted=source_cleanup or finalization,
            real_cleanup_grace=False,per_unit_source_cleanup=source_cleanup or finalization,task_completion=finalization,global_cutover=True,scope=scope,
            elapsed_seconds=round(time.monotonic()-started,4)),sort_keys=True))
    finally:await admin.dispose()


if __name__=='__main__':asyncio.run(run_cases(cases()))
