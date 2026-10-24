"""Actual three-node MySQL histories, with virtual cleanup grace clearly scoped."""
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

from tests import test_node_retirement_history as cases,test_resource_migration_planning as planning


async def main():
    url=make_url(os.environ['FC_DB_MYSQL_URL'])
    if os.environ.get('FC_DB_MYSQL_RUNTIME')!='1' or url.host!='mysql' or url.database!='frontiercloud_db_test':
        raise RuntimeError('Refusing outside disposable mysql/frontiercloud_db_test')
    started=time.monotonic();admin=create_async_engine(url,pool_size=2,max_overflow=0);passed=[]
    try:
        for cls in (cases.NativeHistoryTests,cases.MasterHistoryTests):
            for method in unittest.defaultTestLoader.getTestCaseNames(cls):
                prefix='fc_history_'+uuid.uuid4().hex[:12]
                # Two cancellation cases deliberately create two independent
                # three-node lifecycles; unused databases remain empty.
                names=[prefix+'_'+str(index) for index in range(6)]
                async with admin.begin() as conn:
                    for name in names:
                        if not re.fullmatch(r'fc_history_[a-f0-9]{12}_[0-5]',name):raise RuntimeError('unsafe fixture database')
                        await conn.execute(text(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin'))
                remaining=iter(names);fixture=cls('runTest');ready=False
                def factory(_url):return create_async_engine(url.set(database=next(remaining)),pool_size=3,max_overflow=0)
                try:
                    with patch.object(planning,'create_sqlite_engine',side_effect=factory):
                        await fixture.asyncSetUp();ready=True
                        await getattr(fixture,method)()
                    passed.append(cls.__name__+'.'+method);print(json.dumps(dict(passed=passed[-1])),flush=True)
                finally:
                    if ready:await fixture.asyncTearDown()
                    async with admin.begin() as conn:
                        for name in names:await conn.execute(text(f'DROP DATABASE `{name}`'))
        print(json.dumps(dict(format='frontiercloud-python-retirement-history-mysql',version=1,count=len(passed),checks=passed,
            actual_mysql=True,separate_node_databases=True,real_cleanup_grace=False,readonly_history=True,terminal_retirement=False,
            elapsed_seconds=round(time.monotonic()-started,4)),sort_keys=True))
    finally:await admin.dispose()


if __name__=='__main__':asyncio.run(main())
