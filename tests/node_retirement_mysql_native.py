"""Two actual MySQL stores exercise retirement preparation and retained fences."""
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

from tests import test_node_retirement_state as fixtures


async def run():
    url=make_url(os.environ['FC_DB_MYSQL_URL'])
    if os.environ.get('FC_DB_MYSQL_RUNTIME')!='1' or url.host!='mysql' or url.database!='frontiercloud_db_test':
        raise RuntimeError('Refusing outside disposable mysql/frontiercloud_db_test')
    admin=create_async_engine(url,pool_size=2,max_overflow=0);checks=[];started=time.monotonic()
    try:
        for method in sorted(name for name in dir(fixtures.RetirementStateTests) if name.startswith('test_')):
            prefix='fc_retirement_'+uuid.uuid4().hex[:12];names=[prefix+'_'+side for side in ('master','peer')]
            created=[];fixture=fixtures.RetirementStateTests('runTest');ready=False
            try:
                async with admin.begin() as conn:
                    for name in names:
                        if not re.fullmatch(r'fc_retirement_[a-f0-9]{12}_(master|peer)',name):raise RuntimeError('Unsafe fixture name')
                        await conn.execute(text(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin'));created.append(name)
                remaining=iter(names)
                def factory(_url):return create_async_engine(url.set(database=next(remaining)),pool_size=3,max_overflow=0)
                with patch.object(fixtures,'create_sqlite_engine',side_effect=factory):await fixture.asyncSetUp();ready=True
                await getattr(fixture,method)();checks.append(method)
                print(json.dumps(dict(passed=method,actual_mysql=True,separate_node_databases=True)),flush=True)
            finally:
                if ready:await fixture.asyncTearDown()
                else:
                    if getattr(fixture,'gate',None) is not None:fixture.gate.close()
                    for store in getattr(fixture,'nodes',[]):await store.database.dispose()
                    if getattr(fixture,'temporary',None) is not None:fixture.temporary.cleanup()
                async with admin.begin() as conn:
                    for name in created:await conn.execute(text(f'DROP DATABASE `{name}`'))
        print(json.dumps(dict(format='frontiercloud-python-retirement-state-mysql',version=1,count=len(checks),checks=checks,
            actual_mysql=True,separate_node_databases=True,phase='preparation_only',retirement_completion=False,
            elapsed_seconds=round(time.monotonic()-started,4)),sort_keys=True))
    finally:await admin.dispose()


if __name__=='__main__':asyncio.run(run())
