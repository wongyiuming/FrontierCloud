"""Verified TLS retirement and runtime restart against actual MySQL stores."""
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

from tests import node_retirement_api_native as native,test_node_retirement_complete as stores


async def run():
    url=make_url(os.environ['FC_DB_MYSQL_URL'])
    if os.environ.get('FC_DB_MYSQL_RUNTIME')!='1' or url.host!='mysql' or url.database!='frontiercloud_db_test':
        raise RuntimeError('Refusing outside disposable mysql/frontiercloud_db_test')
    admin=create_async_engine(url,pool_size=2,max_overflow=0);cases=[];started=time.monotonic()
    try:
        for operation in (native.active_request,native.lost_reply_restart):
            prefix='fc_retire_https_'+uuid.uuid4().hex[:12];names=[prefix+'_'+side for side in ('peer','master')];created=[]
            try:
                async with admin.begin() as conn:
                    for name in names:
                        if not re.fullmatch(r'fc_retire_https_[a-f0-9]{12}_(master|peer)',name):raise RuntimeError('Unsafe fixture database')
                        await conn.execute(text(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin'));created.append(name)
                remaining=iter(names)
                def factory(_url):return create_async_engine(url.set(database=next(remaining)),pool_size=3,max_overflow=0)
                with patch.object(stores.files,'create_sqlite_engine',side_effect=factory),patch.object(stores,'create_sqlite_engine',side_effect=factory):
                    value=await operation()
                value['actual_mysql']=True;cases.append(value)
            finally:
                async with admin.begin() as conn:
                    for name in created:await conn.execute(text(f'DROP DATABASE `{name}`'))
        print(json.dumps(dict(format='frontiercloud-python-retirement-api-https-mysql',version=1,cases=cases,
            actual_mysql=True,separate_node_databases=True,actual_retirement_http=True,admin_gui=False,
            full_retirement_acceptance=False,elapsed_seconds=round(time.monotonic()-started,4)),sort_keys=True))
    finally:await admin.dispose()


if __name__=='__main__':asyncio.run(run())
