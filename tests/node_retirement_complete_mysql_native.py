"""Two actual MySQL databases and native files exercise the terminal core."""
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

from tests import test_node_retirement_complete as fixtures,test_node_retirement_files as files


async def run():
    url=make_url(os.environ['FC_DB_MYSQL_URL'])
    if os.environ.get('FC_DB_MYSQL_RUNTIME')!='1' or url.host!='mysql' or url.database!='frontiercloud_db_test':
        raise RuntimeError('Refusing outside disposable mysql/frontiercloud_db_test')
    admin=create_async_engine(url,pool_size=2,max_overflow=0);checks=[];started=time.monotonic()
    try:
        for method in unittest.defaultTestLoader.getTestCaseNames(fixtures.RetirementCompletionTests):
            prefix='fc_retire_complete_'+uuid.uuid4().hex[:12];names=[prefix+'_'+side for side in ('peer','master')]
            fixture=fixtures.RetirementCompletionTests('runTest');ready=False;created=[]
            try:
                async with admin.begin() as conn:
                    for name in names:
                        if not re.fullmatch(r'fc_retire_complete_[a-f0-9]{12}_(master|peer)',name):raise RuntimeError('Unsafe fixture database')
                        await conn.execute(text(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin'));created.append(name)
                remaining=iter(names)
                def factory(_url):return create_async_engine(url.set(database=next(remaining)),pool_size=3,max_overflow=0)
                with patch.object(files,'create_sqlite_engine',side_effect=factory),patch.object(fixtures,'create_sqlite_engine',side_effect=factory):
                    await fixture.asyncSetUp();ready=True
                assert fixture.file.database.dialect.name==fixture.database.dialect.name=='mysql'
                await getattr(fixture,method)();checks.append(method);print(json.dumps(dict(passed=method)),flush=True)
            finally:
                if ready:await fixture.asyncTearDown()
                else:
                    if getattr(fixture,'database',None) is not None:await fixture.database.dispose()
                    owned=getattr(fixture,'file',None)
                    if owned is not None:
                        for field in ('authority','runtime','native'):
                            resource=getattr(owned,field,None)
                            if resource is not None:resource.close()
                        if getattr(owned,'database',None) is not None:await owned.database.dispose()
                        if getattr(owned,'temporary',None) is not None:owned.temporary.cleanup()
                    if getattr(fixture,'mode',None) is not None:fixture.mode.stop()
                async with admin.begin() as conn:
                    for name in created:await conn.execute(text(f'DROP DATABASE `{name}`'))
        print(json.dumps(dict(format='frontiercloud-python-retirement-complete-mysql',version=1,count=len(checks),checks=checks,
            actual_mysql=True,separate_node_databases=True,native_physical_census=True,terminal_core=True,
            actual_http=False,retirement_authentication=False,admin_gui=False,full_retirement_acceptance=False,
            elapsed_seconds=round(time.monotonic()-started,4)),sort_keys=True))
    finally:await admin.dispose()


if __name__=='__main__':asyncio.run(run())
