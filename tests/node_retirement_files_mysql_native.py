"""Actual MySQL selection and native-file census; no terminal retirement claim."""
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

from tests import test_node_retirement_files as fixtures


async def run():
    url=make_url(os.environ['FC_DB_MYSQL_URL'])
    if os.environ.get('FC_DB_MYSQL_RUNTIME')!='1' or url.host!='mysql' or url.database!='frontiercloud_db_test':
        raise RuntimeError('Refusing outside disposable mysql/frontiercloud_db_test')
    admin=create_async_engine(url,pool_size=2,max_overflow=0);checks=[];started=time.monotonic()
    methods=[name for name in dir(fixtures.RetirementFilesTests) if name.startswith('test_') and name not in {
        'test_actual_selected_database_sidecars_are_retained_and_double_roots_are_not_added_twice',
        'test_selected_sqlite_copy_does_not_replace_the_original_database_authority'}]
    try:
        for method in sorted(methods):
            name='fc_retire_files_'+uuid.uuid4().hex[:12]
            if not re.fullmatch(r'fc_retire_files_[a-f0-9]{12}',name):raise RuntimeError('Unsafe fixture database')
            async with admin.begin() as conn:await conn.execute(text(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin'))
            fixture=fixtures.RetirementFilesTests('runTest');ready=False
            try:
                def factory(_url):return create_async_engine(url.set(database=name),pool_size=2,max_overflow=0)
                with patch.object(fixtures,'create_sqlite_engine',side_effect=factory):await fixture.asyncSetUp();ready=True
                self_selection=fixture.authority.selection
                await getattr(fixture,method)();checks.append(method)
                assert self_selection and fixture.database.dialect.name=='mysql'
                print(json.dumps(dict(passed=method,actual_mysql=True)),flush=True)
            finally:
                if ready:await fixture.asyncTearDown()
                else:
                    if getattr(fixture,'authority',None) is not None:fixture.authority.close()
                    if getattr(fixture,'runtime',None) is not None:fixture.runtime.close()
                    if getattr(fixture,'native',None) is not None:fixture.native.close()
                    if getattr(fixture,'database',None) is not None:await fixture.database.dispose()
                    if getattr(fixture,'temporary',None) is not None:fixture.temporary.cleanup()
                async with admin.begin() as conn:await conn.execute(text(f'DROP DATABASE `{name}`'))
        print(json.dumps(dict(format='frontiercloud-python-retirement-files-mysql',version=1,count=len(checks),checks=checks,
            actual_mysql=True,physical_inspection=True,terminal_retirement=False,elapsed_seconds=round(time.monotonic()-started,4)),sort_keys=True))
    finally:await admin.dispose()


if __name__=='__main__':asyncio.run(run())
