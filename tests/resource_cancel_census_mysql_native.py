"""Actual MySQL upgrade recovery and three-store native cancellation census."""
import asyncio
import json
import os
import re
import time
import unittest
import uuid
from contextlib import suppress
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests import test_resource_cancel_agreement as agreement,test_resource_cancel_catalog as catalog,test_resource_cancel_census as native
from tests import test_resource_cancel_census_schema as schema,test_resource_cancel_source_release as release,test_resource_migration_planning as planning
from tests import test_resource_cancel_target_release as target_release
from tests import test_resource_cancel_finalize_master as master_release
from tests import test_node_retirement_cancel_history as cancel_history
from tests import test_resource_cancel_routing as routing
from tests import test_resource_pipeline as pipeline
from tests import test_resource_cancel_partial as partial


async def run():
    url=make_url(os.environ['FC_DB_MYSQL_URL'])
    if os.environ.get('FC_DB_MYSQL_RUNTIME')!='1' or url.host!='mysql' or url.database!='frontiercloud_db_test':
        raise RuntimeError('Refusing outside disposable mysql/frontiercloud_db_test')
    admin=create_async_engine(url,pool_size=2,max_overflow=0);checks=[];started=time.monotonic()
    cases=[(case,method) for case in (schema.CancellationCensusSchemaTests,native.SourceCancellationCensusTests,native.SourceRecordingCensusTests,native.CensusClientTests,
        catalog.CurrentCancellationCatalogTests,catalog.CurrentRecordingCatalogTests,catalog.CommittedEncryptedCatalogTests,
        agreement.MasterAgreementTests,release.SourceReleaseTests,target_release.TargetReleaseTests,
        master_release.MasterCancellationFinalizationTests,cancel_history.CancelledMasterHistoryTests,cancel_history.CancelledNativeHistoryTests,
        routing.CancellationRoutingTests,routing.CommittedCancellationTests,pipeline.PipelineTests,partial.PartialCancellationTests)
        for method in unittest.defaultTestLoader.getTestCaseNames(case)]
    lock_case=(schema.CancellationCensusSchemaTests,'test_actual_mysql_lock_name_is_compatible_exclusive_and_released')
    tombstones=os.environ.get('FC_CANCEL_CATALOG_TOMBSTONE_NATIVE')=='1'
    release_profile=os.environ.get('FC_CANCEL_SOURCE_RELEASE_NATIVE')=='1'
    target_profile=os.environ.get('FC_CANCEL_TARGET_RELEASE_NATIVE')=='1'
    master_profile=os.environ.get('FC_CANCEL_MASTER_RELEASE_NATIVE')=='1'
    history_profile=os.environ.get('FC_CANCEL_HISTORY_NATIVE')=='1'
    routing_profile=os.environ.get('FC_CANCEL_ROUTING_NATIVE')=='1'
    pipeline_profile=os.environ.get('FC_PARENT_PIPELINE_NATIVE')=='1'
    partial_profile=os.environ.get('FC_PARTIAL_CANCEL_NATIVE')=='1'
    if partial_profile:
        cases=[entry for entry in cases if entry[0] is partial.PartialCancellationTests]
        if len(cases)!=6:raise RuntimeError('Incomplete mixed partial cancellation inventory')
    elif pipeline_profile:
        cases=[entry for entry in cases if entry[0] is pipeline.PipelineTests]
        if len(cases)!=5:raise RuntimeError('Incomplete per-parent pipeline inventory')
    elif routing_profile:
        cases=[entry for entry in cases if entry[0] in (routing.CancellationRoutingTests,routing.CommittedCancellationTests)]
        if len(cases)!=7:raise RuntimeError('Incomplete cancellation routing inventory')
    elif history_profile:
        cases=[entry for entry in cases if entry[0] in (cancel_history.CancelledMasterHistoryTests,cancel_history.CancelledNativeHistoryTests)]
        if len(cases)!=13:raise RuntimeError('Incomplete cancellation history inventory')
    elif master_profile:
        cases=[entry for entry in cases if entry[0] is master_release.MasterCancellationFinalizationTests]
        if len(cases)!=8:raise RuntimeError('Incomplete Master release inventory')
    elif target_profile:
        cases=[entry for entry in cases if entry[0] is target_release.TargetReleaseTests]
        if len(cases)!=8:raise RuntimeError('Incomplete target release inventory')
    elif release_profile:
        cases=[entry for entry in cases if entry[0] in (agreement.MasterAgreementTests,release.SourceReleaseTests)]
        if len(cases)!=16:raise RuntimeError('Incomplete source release inventory')
    elif tombstones:
        names={'test_live_orphan_descriptor_still_refuses_after_native_registry_disappears',
            'test_deleted_encrypted_objects_keep_retired_key_records_and_agree',
            'test_missing_master_catalog_with_live_descriptor_still_refuses',
            'test_committed_encrypted_cleanup_keeps_native_tombstone_and_allows_current_agreement'}
        cases=[entry for entry in cases if entry[1] in names]
        if len(cases)!=4:raise RuntimeError('Incomplete focused tombstone inventory')
    else:cases.extend([(*lock_case,'short'),(*lock_case,'unicode')])
    release_tested=any(entry[0] is release.SourceReleaseTests for entry in cases)
    master_tested=any(entry[0] is master_release.MasterCancellationFinalizationTests for entry in cases)
    target_tested=any(entry[0] is target_release.TargetReleaseTests for entry in cases)
    try:
        for entry in cases:
            case,method,*variant=entry;variant=variant[0] if variant else ''
            sides=('schema',) if case is schema.CancellationCensusSchemaTests else ('master','source','target')
            token=uuid.uuid4().hex[:12]
            prefix='fcc_'+token if variant=='short' else 'fc_'+token+'界'*10 if variant=='unicode' else 'fc_cancel_census_'+token
            names=[prefix+'_'+side for side in sides];created=[]
            fixture=case('runTest');ready=False
            try:
                async with admin.begin() as conn:
                    for name in names:
                        if not re.fullmatch(r'(?:fc_cancel_census_[a-f0-9]{12}_(schema|master|source|target)|fcc_[a-f0-9]{12}_schema|fc_[a-f0-9]{12}界{10}_schema)',name):raise RuntimeError('Unsafe fixture database')
                        await conn.execute(text(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin'));created.append(name)
                remaining=iter(names)
                def factory(_url):return create_async_engine(url.set(database=next(remaining)),pool_size=3,max_overflow=0)
                module=schema if case is schema.CancellationCensusSchemaTests else planning
                with patch.object(module,'create_sqlite_engine',side_effect=factory):
                    await fixture.asyncSetUp();ready=True;await getattr(fixture,method)()
                checks.append(case.__name__+'.'+method+('.'+variant if variant else ''));print(json.dumps(dict(passed=checks[-1])),flush=True)
            finally:
                if ready:await fixture.asyncTearDown()
                else:
                    with suppress(Exception):await fixture.asyncTearDown()
                async with admin.begin() as conn:
                    for name in created:await conn.execute(text(f'DROP DATABASE `{name}`'))
        print(json.dumps(dict(format='frontiercloud-python-cancel-census-mysql',version=1,count=len(checks),checks=checks,
            actual_mysql=True,separate_node_databases=True,actual_https=False,
            current_source_census=any(entry[0] is native.SourceCancellationCensusTests for entry in cases),
            current_master_catalog=any(entry[0] is catalog.CurrentCancellationCatalogTests for entry in cases),
            focused_tombstone_profile=tombstones,
            source_release_profile=release_profile,native_source_release_tested=release_tested,master_terminal_cancellation=master_tested,
            target_release_profile=target_profile,native_target_release_tested=target_tested,
            master_release_profile=master_profile,master_terminal_cancellation_tested=master_tested,
            cancellation_history_profile=history_profile,master_readonly_history_tested=history_profile,
            cancellation_routing_profile=routing_profile,
            parent_pipeline_profile=pipeline_profile,
            mixed_partial_cancellation_profile=partial_profile,
            media_payload_read_during_census=False,transfer_copies_payload=True,native_usage_reconciled=release_tested,source_drain_released=release_tested,
            full_cancellation_acceptance=False,virtual_cleanup_grace=True,elapsed_seconds=round(time.monotonic()-started,4)),sort_keys=True))
    finally:await admin.dispose()


if __name__=='__main__':asyncio.run(run())
