"""Actual verified TLS planner across three disposable SQLite node stores.

Two HTTPS ASGI apps have independent state selected through a test-only request
context. This is one CPU-limited fixture, not a multi-host transfer acceptance.
"""
import asyncio
import contextvars
import hashlib
import json
import os
from pathlib import Path
import resource
import ssl
import time
import uuid
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI
import httpx
from sqlalchemy import update
import uvicorn

from app.api import internal_nodes, internal_resource_inventory as inventory_api
from app.api import internal_resource_observation as observation_api, internal_resource_registry as registry_api
from app.services import media_crypto as crypto
from app.services.business_traffic import BusinessTrafficMiddleware, Monitor
from app.services.federation import schema as fs
from app.services.federation.transport import Transport
from tests.certificate_fixtures import TestCA
from tests.test_resource_migration_planning import MigrationPlanningTests


async def main():
    started,cpu = time.monotonic(),time.process_time()
    fixture = MigrationPlanningTests('test_ready_plan_keeps_plain_ciphertext_identity_and_all_bytes_without_admission')
    await fixture.asyncSetUp()
    tasks,servers,reapers = [],[],[]
    selected = contextvars.ContextVar('native_planning_peer')
    wire = {name:dict(received=0,sent=0,requests=0) for name in ('source','target')}
    class Proxy:
        def __init__(self,name):
            self.name = name
        def __getattr__(self,name):
            return getattr(getattr(selected.get(),self.name),name)
    class Bound:
        def __init__(self,app,peer):
            self.app,self.peer = app,peer
        async def __call__(self,scope,receive,send):
            token = selected.set(self.peer)
            if scope['type'] == 'http':
                wire[self.peer.name]['requests'] += 1
            async def received():
                value = await receive()
                wire[self.peer.name]['received'] += len(value.get('body',b''))
                return value
            async def sent(value):
                wire[self.peer.name]['sent'] += len(value.get('body',b''))
                await send(value)
            try:
                await self.app(scope,received,sent)
            finally:
                selected.reset(token)
    root = fixture.root
    ca,wrong = TestCA(),TestCA()
    chain,key = ca.pair()
    for name,value in (('chain.pem',chain),('key.pem',key),
        ('ca.pem',ca.root.public_bytes(serialization.Encoding.PEM)),
        ('wrong.pem',wrong.root.public_bytes(serialization.Encoding.PEM))):
        (root/name).write_bytes(value)
    os.chmod(root/'key.pem',0o600)
    cases = [await fixture.media(b'ID3'+bytes(8*1024*1024)),
             await fixture.media(b'private audio'*6000,path='music/album/encrypted.mp3',encrypted=True)]
    transport = Transport()
    fixture.planner.transport = transport
    fixture.planner.traffic = Monitor()
    fixture.planner.wall = fixture.planner.spool.wall = time.time
    patches = [patch.object(internal_nodes,'state',Proxy('store')),
        patch.object(inventory_api,'state',Proxy('store')),patch.object(inventory_api,'inventory',Proxy('inventory')),
        patch.object(observation_api,'observer',Proxy('observer')),patch.object(registry_api,'registry',Proxy('registry')),
        patch.object(internal_nodes.settings,'TLS_ENABLED',True),patch.dict(os.environ,{'SSL_CERT_FILE':str(root/'ca.pem')})]
    try:
        for item in patches:
            item.start()
        for port,peer in enumerate((fixture.source,fixture.target),18743):
            origin = f'https://node.4399.com:{port}'
            peer.store.node['endpoint'] = origin
            async with peer.database.begin() as conn:
                await conn.execute(update(fs.identity).values(endpoint=origin))
            async with fixture.master.database.begin() as conn:
                await conn.execute(update(fs.relationships).where(fs.relationships.c.peer_id == peer.store.node['node_id']).values(peer_endpoint=origin))
            peer.inventory.traffic = peer.observer.traffic = Monitor()
            app = FastAPI()
            for router in (internal_nodes.router,inventory_api.router,observation_api.router,registry_api.router):
                app.include_router(router)
            app.add_middleware(BusinessTrafficMiddleware,monitor=peer.inventory.traffic)
            server = uvicorn.Server(uvicorn.Config(Bound(app,peer),host='127.0.0.1',port=port,
                ssl_certfile=str(root/'chain.pem'),ssl_keyfile=str(root/'key.pem'),lifespan='off',
                access_log=False,log_level='error',workers=1))
            servers.append(server)
            tasks.append(asyncio.create_task(server.serve()))
            reapers.append(asyncio.create_task(peer.inventory.reap()))
        async with asyncio.timeout(5):
            while not all(server.started for server in servers):
                for task in tasks:
                    if task.done():
                        await task
                        raise AssertionError('native_tls_server_stopped')
                await asyncio.sleep(.01)
        for name,origin,context in (
            ('wrong_ca','https://node.4399.com:18743',ssl.create_default_context(cafile=str(root/'wrong.pem'))),
            ('wrong_san','https://wrong.4399.com:18743',ssl.create_default_context(cafile=str(root/'ca.pem')))):
            async with httpx.AsyncClient(verify=context,trust_env=False,timeout=3) as client:
                try:
                    await client.get(origin+'/internal/v1/identity',params={'challenge':'a'*32})
                except httpx.ConnectError:
                    pass
                else:
                    raise AssertionError(name+'_accepted')
        with (patch.object(crypto,'open_premaster',side_effect=AssertionError('premaster accessed')),
              patch.object(crypto.Manager,'file_key',side_effect=AssertionError('media decrypted'))):
            await fixture.planner.start(uuid.uuid4().hex,fixture.source.store.node['node_id'],fixture.target.store.node['node_id'],'native')
            await asyncio.wait_for(asyncio.shield(fixture.planner.task),75)
            ready = await fixture.planner.status(fixture.planner.plan_id)
            assert ready['state'] == 'ready',ready
            assert ready['object_count'] == 2 and ready['unit_count'] == 1
            target = fixture.target.roots['media']/'music/album/nested/conflict.bin'
            target.parent.mkdir(parents=True)
            target.write_bytes(b'preserve target')
            await fixture.planner.start(uuid.uuid4().hex,fixture.source.store.node['node_id'],fixture.target.store.node['node_id'],'native')
            await asyncio.wait_for(asyncio.shield(fixture.planner.task),30)
            blocked = await fixture.planner.status(fixture.planner.plan_id)
            assert blocked['state'] == 'blocked'
            assert 'migration_target_parent_occupied' in {item['code'] for item in blocked['report']['blockers']}
            assert target.read_bytes() == b'preserve target'
        for _,path,data in cases:
            assert path.read_bytes() == data
        assert sum(value['sent'] for value in wire.values()) < sum(len(value[2]) for value in cases)
        print(json.dumps(dict(format='frontiercloud-python-native-readonly-plan',version=1,
            tls_verified=True,wrong_ca_rejected=True,wrong_san_rejected=True,
            source_target_stores=2,master_store=True,quiet_startup_wait=True,
            objects=ready['object_count'],bytes=ready['total_bytes'],units=ready['unit_count'],
            full_inventory_rounds=2,ready=True,target_conflict_blocked=True,
            media_decrypted=False,source_changed=False,task_admitted=False,wire=wire,
            elapsed_seconds=round(time.monotonic()-started,4),cpu_seconds=round(time.process_time()-cpu,4),
            peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),sort_keys=True))
    finally:
        await transport.close()
        await fixture.planner.stop()
        for reaper in reapers:
            reaper.cancel()
        await asyncio.gather(*reapers,return_exceptions=True)
        for server in servers:
            server.should_exit = True
        await asyncio.wait_for(asyncio.gather(*tasks),5)
        for item in reversed(patches):
            item.stop()
        await fixture.asyncTearDown()


if __name__ == '__main__':
    asyncio.run(main())
