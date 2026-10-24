"""Independent Master controller recovery against an existing disposable task."""
import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import time

from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.core.sqlite_compat import create_sqlite_engine
from app.services import resource_migration_schema as s
from app.services.business_traffic import Monitor
from app.services.federation import protocol as p,schema as fs
from app.services.federation.state import State
from app.services.federation.transport import Transport
from app.services.resource_migration_scheduler import Scheduler


async def run(path):
    private=json.loads(Path(path).read_text());url=make_url(private['url'])
    task_id=private['task_id'];mode=private['mode']
    if not p.IDENTIFIER.fullmatch(task_id) or mode not in {'units','terminal'}:raise RuntimeError('Invalid fixture task or recovery mode')
    if url.get_backend_name()=='sqlite':database=create_sqlite_engine(url)
    else:
        from sqlalchemy.ext.asyncio import create_async_engine
        if url.get_backend_name()!='mysql' or url.host!='mysql' or not url.database.startswith('fc_cancel_https_'):
            raise RuntimeError('Refusing an unguarded Master child database')
        database=create_async_engine(url,pool_size=3,max_overflow=0)
    store=State(database,base64.b64decode(private['vault_key']));await store.initialize()
    if store.node['role']!='Master':raise RuntimeError('Fixture identity is not a Master')
    inputs=[]
    class RecordedTransport(Transport):
        metadata_bytes=0
        def open(self):
            previous=self.client;super().open()
            if previous is None:
                async def record(request):
                    inputs.append(dict(method=request.method,url=str(request.url),path=request.url.raw_path.decode(),
                        headers={name:request.headers.get(name,'') for name in ('x-node-relationship','x-node-time','x-node-nonce','x-node-signature')},
                        value=json.loads(request.content) if request.content else None))
                self.client.event_hooks['request'].append(record)
        async def request(self,*args,**kwargs):
            self.metadata_bytes+=len(p.canonical(kwargs.get('value')))
            value=await super().request(*args,**kwargs);self.metadata_bytes+=len(p.canonical(value));return value
    transport=RecordedTransport();scheduler=Scheduler(store,transport,traffic=Monitor(),fenced=lambda:False)
    started=time.monotonic();last_heartbeat=0;waits=set()
    async def snapshot():
        async with database.connect() as conn:
            task=dict((await conn.execute(select(s.tasks).where(s.tasks.c.task_id==task_id))).mappings().one())
            units=[dict(row) for row in (await conn.execute(select(s.units).where(s.units.c.task_id==task_id).order_by(s.units.c.unit_id))).mappings()]
            runner=dict((await conn.execute(select(s.runners).where(s.runners.c.task_id==task_id))).mappings().one())
        return task,units,runner
    async def heartbeat(task):
        for side in ('source','target'):
            async with database.connect() as conn:
                relation=dict((await conn.execute(select(fs.relationships).where(fs.relationships.c.relationship_id==task[side+'_relationship']))).mappings().one())
            value=await transport.request(relation['peer_endpoint'],'/internal/v1/heartbeat',method='POST',value={},
                credential=store.unseal(relation['credential']),relation=relation['relationship_id'])
            await store.heartbeat(relation['relationship_id'],True,summary=value)
    try:
        _,_,before=await snapshot()
        async with asyncio.timeout(180):
            while True:
                task,units,runner=await snapshot()
                if time.monotonic()-last_heartbeat>=10:await heartbeat(task);last_heartbeat=time.monotonic()
                if task['state']=='failed':raise AssertionError(task['error_code'])
                closed=all(unit['state'] in {'committed','cleaned','cancelled'} and unit['native_acknowledged']==1 for unit in units)
                if ((mode=='units' and closed and runner['epoch']>before['epoch']) or
                        (mode=='terminal' and task['state']=='cancelled')):break
                code=task['error_code']
                if code and code not in waits:
                    waits.add(code);print(json.dumps(dict(waiting=code,pid=os.getpid(),task_id=task_id)),file=sys.stderr,flush=True)
                progress=await scheduler.tick()
                await asyncio.sleep(.01 if progress else .2)
        await scheduler.stop();task,units,runner=await snapshot()
        value=dict(format='frontiercloud-python-independent-master-recovery',version=1,pid=os.getpid(),node_id=store.node['node_id'],
            task_id=task_id,mode=mode,state=task['state'],previous_epoch=before['epoch'],epoch=runner['epoch'],runner_expires_at=runner['expires_at'],
            unit_states=[dict(unit_id=unit['unit_id'],state=unit['state'],native_acknowledged=unit['native_acknowledged']) for unit in units],
            actual_http_inputs=inputs,metadata_bytes=transport.metadata_bytes,waits=sorted(waits),
            committed_bytes=task['committed_bytes'],elapsed_seconds=round(time.monotonic()-started,4))
        Path(private['result']).write_text(json.dumps(value,sort_keys=True))
    finally:
        await scheduler.stop();await transport.close();await database.dispose()


if __name__=='__main__':asyncio.run(run(sys.argv[1]))
