"""Actual HTTPS and independent Target process against three disposable MySQLs."""
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

from tests import resource_transfer_native as native,test_resource_migration_planning as stores


async def run():
    url=make_url(os.environ['FC_DB_MYSQL_URL'])
    if os.environ.get('FC_DB_MYSQL_RUNTIME')!='1' or url.host!='mysql' or url.database!='frontiercloud_db_test':
        raise RuntimeError('Refusing outside disposable mysql/frontiercloud_db_test')
    prefix='fc_cancel_https_'+uuid.uuid4().hex[:12];names=[prefix+'_'+side for side in ('master','source','target')]
    admin=create_async_engine(url,pool_size=2,max_overflow=0);created=[];started=time.monotonic()
    try:
        async with admin.begin() as conn:
            for name in names:
                if not re.fullmatch(r'fc_cancel_https_[a-f0-9]{12}_(master|source|target)',name):raise RuntimeError('Unsafe fixture database')
                await conn.execute(text(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin'));created.append(name)
        remaining=iter(names)
        def factory(_url):return create_async_engine(url.set(database=next(remaining)),pool_size=3,max_overflow=0)
        with patch.object(stores,'create_sqlite_engine',side_effect=factory),patch.dict(os.environ,
                {'FRONTIERCLOUD_PUBLISH_NATIVE':'1','FRONTIERCLOUD_SHADOW_CANCEL_NATIVE':'1'}):
            await native.main()
        kind=next((name for flag,name in (
            ('FRONTIERCLOUD_CANCEL_MASTER_RELEASE_NATIVE','master-release'),
            ('FRONTIERCLOUD_CANCEL_TARGET_RELEASE_NATIVE','target-release'),
            ('FRONTIERCLOUD_CANCEL_SOURCE_RELEASE_NATIVE','source-release'),
            ('FRONTIERCLOUD_CANCEL_CENSUS_NATIVE','census'),('FRONTIERCLOUD_CANCEL_MIXED_NATIVE','mixed'),('FRONTIERCLOUD_CANCEL_MASTER_NATIVE','master'),
            ('FRONTIERCLOUD_UNIT_SETTLE_NATIVE','unit'),('FRONTIERCLOUD_SOURCE_RETAIN_NATIVE','source'))
            if os.environ.get(flag)=='1'),'target')
        print(json.dumps(dict(format='frontiercloud-python-cancel-'+kind+'-https-mysql',version=1,
            actual_mysql=True,separate_node_databases=True,actual_https=True,independent_target_process=True,
            master_terminal_cancellation=os.environ.get('FRONTIERCLOUD_CANCEL_MASTER_RELEASE_NATIVE')=='1',
            elapsed_seconds=round(time.monotonic()-started,4)),sort_keys=True))
    finally:
        async with admin.begin() as conn:
            for name in created:await conn.execute(text(f'DROP DATABASE `{name}`'))
        await admin.dispose()


if __name__=='__main__':asyncio.run(run())
