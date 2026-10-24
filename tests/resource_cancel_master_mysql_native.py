"""Cancellation coordinator recovery on three real disposable MySQL stores."""
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

from tests import test_resource_cancel_master as fixtures,test_resource_migration_planning as planning


async def run():
    url=make_url(os.environ['FC_DB_MYSQL_URL'])
    if os.environ.get('FC_DB_MYSQL_RUNTIME')!='1' or url.host!='mysql' or url.database!='frontiercloud_db_test':
        raise RuntimeError('Refusing outside disposable mysql/frontiercloud_db_test')
    admin=create_async_engine(url,pool_size=2,max_overflow=0);checks=[];started=time.monotonic()
    cases=[(case,method) for case in (fixtures.MasterCancellationTests,fixtures.MasterCancellationVariantsTests)
        for method in unittest.defaultTestLoader.getTestCaseNames(case)]
    try:
        for case,method in cases:
            prefix='fc_cancel_master_'+uuid.uuid4().hex[:12];names=[prefix+'_'+side for side in ('master','source','target')];created=[]
            fixture=case('runTest');ready=False
            try:
                async with admin.begin() as conn:
                    for name in names:
                        if not re.fullmatch(r'fc_cancel_master_[a-f0-9]{12}_(master|source|target)',name):raise RuntimeError('Unsafe fixture database')
                        await conn.execute(text(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin'));created.append(name)
                remaining=iter(names)
                def factory(_url):return create_async_engine(url.set(database=next(remaining)),pool_size=3,max_overflow=0)
                with patch.object(planning,'create_sqlite_engine',side_effect=factory):
                    await fixture.asyncSetUp();ready=True;await getattr(fixture,method)()
                checks.append(method);print(json.dumps(dict(passed=method)),flush=True)
            finally:
                if ready:await fixture.asyncTearDown()
                else:
                    with suppress(Exception):await fixture.asyncTearDown()
                async with admin.begin() as conn:
                    for name in created:await conn.execute(text(f'DROP DATABASE `{name}`'))
        print(json.dumps(dict(format='frontiercloud-python-cancel-master-mysql',version=1,count=len(checks),checks=checks,
            actual_mysql=True,separate_node_databases=True,actual_asgi_hmac=True,actual_https=False,
            native_unit_cancellation=True,source_files_deleted=False,master_terminal_cancellation=False,full_cancellation_acceptance=False,
            elapsed_seconds=round(time.monotonic()-started,4)),sort_keys=True))
    finally:await admin.dispose()


if __name__=='__main__':asyncio.run(run())
