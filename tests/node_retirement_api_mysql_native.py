"""ASGI retirement HMAC, nonce and terminal paths on two real MySQL stores."""
import asyncio
import json
import os
import re
import time
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests import test_node_retirement_api as fixtures
from tests.test_node_retirement_preflight import RetirementPreflightTests
from tests.test_node_retirement_admin import RetirementAdminTests


async def run():
    url=make_url(os.environ['FC_DB_MYSQL_URL'])
    if os.environ.get('FC_DB_MYSQL_RUNTIME')!='1' or url.host!='mysql' or url.database!='frontiercloud_db_test':
        raise RuntimeError('Refusing outside disposable mysql/frontiercloud_db_test')
    admin=create_async_engine(url,pool_size=2,max_overflow=0);checks=[];started=time.monotonic()
    try:
        cases=[(case,method) for case in (fixtures.RetirementAPITests,RetirementPreflightTests,RetirementAdminTests)
            for method in unittest.defaultTestLoader.getTestCaseNames(case)]
        for case,method in cases:
            prefix='fc_retire_api_'+uuid.uuid4().hex[:12];names=[prefix+'_'+side for side in ('peer','master')]
            fixture=case('runTest');ready=False;created=[]
            try:
                async with admin.begin() as conn:
                    for name in names:
                        if not re.fullmatch(r'fc_retire_api_[a-f0-9]{12}_(master|peer)',name):raise RuntimeError('Unsafe fixture database')
                        await conn.execute(text(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin'));created.append(name)
                remaining=iter(names)
                def factory(_url):return create_async_engine(url.set(database=next(remaining)),pool_size=3,max_overflow=0)
                with patch.object(fixtures.fixtures.files,'create_sqlite_engine',side_effect=factory),patch.object(fixtures.fixtures,'create_sqlite_engine',side_effect=factory):
                    await fixture.asyncSetUp();ready=True
                await getattr(fixture,method)();checks.append(method);print(json.dumps(dict(passed=method)),flush=True)
            finally:
                if ready:await fixture.asyncTearDown()
                else:
                    owned=getattr(fixture,'f',None)
                    if owned is not None and getattr(owned,'database',None) is not None:await owned.asyncTearDown()
                    for item in reversed(getattr(fixture,'patches',[])):item.stop()
                async with admin.begin() as conn:
                    for name in created:await conn.execute(text(f'DROP DATABASE `{name}`'))
        print(json.dumps(dict(format='frontiercloud-python-retirement-api-mysql',version=1,count=len(checks),checks=checks,
            actual_mysql=True,separate_node_databases=True,actual_asgi=True,actual_hmac=True,actual_https=False,
            retirement_api=True,admin_gui=False,full_retirement_acceptance=False,elapsed_seconds=round(time.monotonic()-started,4)),sort_keys=True))
    finally:await admin.dispose()


if __name__=='__main__':asyncio.run(run())
