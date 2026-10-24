"""Actual TLS Source/Target bytes, partial staging and restarted Target process."""
import asyncio
import base64
import contextvars
import hashlib
import json
import os
from pathlib import Path
import ssl
import sys
import time
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI
from fastapi.responses import Response
import httpx
from sqlalchemy import func,select,text,update
from sqlalchemy.engine import make_url
import uvicorn

from app.api import internal_nodes, internal_resource_inventory as inventory_api
from app.api import internal_resource_observation as observation_api, internal_resource_registry as registry_api
from app.api import internal_resource_reservations as reservation_api, internal_resource_transfer as transfer_api
from app.core.sqlite_compat import create_sqlite_engine
from app.services import media_crypto as crypto, resource_migration_schema as s
from app.services.business_traffic import BusinessTrafficMiddleware,Monitor
from app.services.federation import protocol as p,schema as fs
from app.services.federation.state import State
from app.services.federation.transport import Transport
from app.services.resource_transfer_files import DIRECTORY
from app.services.resource_transfer_master import TransferMaster
from app.services.resource_transfer_peers import TransferPeers
from app.services.resource_transfer_cancel import Cancellation
from app.services.resource_publish_master import Publication
from app.services.resource_cutover_master import Cutover
from app.services.resource_transfer_protocol import TransferError
from tests.certificate_fixtures import TestCA
from tests import test_resource_transfer as fixtures


async def child(path):
    from app.api.v1 import media as media_api
    from app.middleware import native_storage
    from app.services import media_manager
    private=json.loads(Path(path).read_text());url=make_url(private['url'])
    if url.get_backend_name()=='sqlite':database=create_sqlite_engine(url)
    else:
        from sqlalchemy.ext.asyncio import create_async_engine
        if url.get_backend_name()!='mysql' or url.host!='mysql' or not url.database.startswith('fc_cancel_https_'):
            raise RuntimeError('Refusing an unguarded native child database')
        database=create_async_engine(url,pool_size=3,max_overflow=0)
    store=State(database,base64.b64decode(private['vault_key']));await store.initialize()
    traffic=Monitor();service=TransferPeers(store,root_provider=lambda:{name:Path(value) for name,value in private['roots'].items()},traffic=traffic)
    inventory=None
    patches=[patch.object(internal_nodes,'state',store),patch.object(inventory_api,'state',store),
        patch.object(transfer_api,'transfers',service),patch.object(internal_nodes.settings,'TLS_ENABLED',True),
        patch.object(native_storage,'state',store),patch.object(native_storage.settings,'DEPLOYMENT_MODE','only_stroge'),
        patch.object(media_api,'engine',database),patch.object(media_api,'node_state',store),
        patch.object(media_api,'MEDIA_ROOT',Path(private['roots']['media'])),
        patch.object(media_manager,'MEDIA_ROOT',Path(private['roots']['media'])),
        patch.object(crypto,'open_premaster',side_effect=AssertionError('native child opened premaster')),
        patch.object(crypto.Manager,'file_key',side_effect=AssertionError('native child decrypted media'))]
    if private.get('all_controls') is True:
        from app.services.resource_inventory import Inventory
        from app.services.resource_registry import NativeRegistry
        inventory=Inventory(root_provider=service.root_provider,traffic=traffic)
        registry=NativeRegistry(store,service.observer,inventory)
        patches.extend([patch.object(inventory_api,'state',store),patch.object(inventory_api,'inventory',inventory),
            patch.object(observation_api,'observer',service.observer),patch.object(registry_api,'registry',registry),
            patch.object(reservation_api,'reservations',service.native)])
    try:
        for item in patches:item.start()
        app=FastAPI();app.include_router(internal_nodes.router);app.include_router(transfer_api.router);counted_app=None
        if inventory is not None:
            from fastapi import Request
            from fastapi.responses import JSONResponse
            for router in (inventory_api.router,observation_api.router,registry_api.router,reservation_api.router):app.include_router(router)
            wire=dict(requests=0,received=0,sent=0,data_sent=0)
            @app.post('/internal/v1/fixture-wire')
            async def fixture_wire(request:Request):
                await internal_nodes.authenticated(request)
                return JSONResponse(p.sign(store.unseal(store.node['private_key']),dict(node_id=store.node['node_id'],pid=os.getpid(),
                    traffic=traffic.snapshot(),**wire)))
            wire_app=app
            async def counted(scope,receive,send):
                data=False
                if scope['type']=='http':wire['requests']+=1
                async def incoming():
                    value=await receive();wire['received']+=len(value.get('body',b''));return value
                async def outgoing(value):
                    nonlocal data
                    if value['type']=='http.response.start':
                        data=scope['path'].endswith('/transfer/source') and (b'content-type',b'application/octet-stream') in value['headers']
                    length=len(value.get('body',b''));wire['sent']+=length
                    if data:wire['data_sent']+=length
                    await send(value)
                await wire_app(scope,incoming,outgoing)
            counted_app=counted
        app.add_middleware(native_storage.NativeStorageDeliveryMiddleware)
        app.add_middleware(BusinessTrafficMiddleware,monitor=traffic)
        if counted_app is not None:app=counted_app
        if os.environ.get('FRONTIERCLOUD_CANCEL_CENSUS_NATIVE')=='1':
            base_app=app
            async def protected(scope,receive,send):
                if scope['type']=='http' and scope['path'].endswith('/cancel-source-census'):
                    with patch('app.services.resource_observation.os.pread',side_effect=AssertionError('Census read media payload')):
                        await base_app(scope,receive,send)
                else:await base_app(scope,receive,send)
            app=protected
        await uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=private['port'],ssl_certfile=private['chain'],ssl_keyfile=private['key'],
            lifespan='off',workers=1,access_log=False,log_level='error')).serve()
    finally:
        if inventory is not None:await inventory.stop()
        await service.close();await database.dispose()
        for item in reversed(patches):item.stop()


async def main():
    publish=os.environ.get('FRONTIERCLOUD_PUBLISH_NATIVE')=='1'
    cutover=os.environ.get('FRONTIERCLOUD_CUTOVER_NATIVE')=='1'
    cleanup=os.environ.get('FRONTIERCLOUD_CLEANUP_NATIVE')=='1'
    shadow_cancel=os.environ.get('FRONTIERCLOUD_SHADOW_CANCEL_NATIVE')=='1'
    source_retain=os.environ.get('FRONTIERCLOUD_SOURCE_RETAIN_NATIVE')=='1'
    cancel_master=os.environ.get('FRONTIERCLOUD_CANCEL_MASTER_NATIVE')=='1'
    cancel_mixed=os.environ.get('FRONTIERCLOUD_CANCEL_MIXED_NATIVE')=='1'
    cancel_census=os.environ.get('FRONTIERCLOUD_CANCEL_CENSUS_NATIVE')=='1'
    if os.environ.get('FRONTIERCLOUD_CANCEL_SOURCE_RELEASE_NATIVE')=='1' and not cancel_census:
        raise RuntimeError('Source release requires the complete native current-census profile')
    if (os.environ.get('FRONTIERCLOUD_CANCEL_TARGET_RELEASE_NATIVE')=='1' and
            os.environ.get('FRONTIERCLOUD_CANCEL_SOURCE_RELEASE_NATIVE')!='1'):
        raise RuntimeError('Target release requires the complete native Source-release profile')
    if (os.environ.get('FRONTIERCLOUD_CANCEL_MASTER_RELEASE_NATIVE')=='1' and
            os.environ.get('FRONTIERCLOUD_CANCEL_TARGET_RELEASE_NATIVE')!='1'):
        raise RuntimeError('Master release requires the complete native Target-release profile')
    if cancel_census and not cancel_master:raise RuntimeError('Current census requires the complete native coordinator cancellation profile')
    if cancel_mixed and (not shadow_cancel or cutover):raise RuntimeError('Mixed cancellation requires uncommitted publication and a separate first-parent commit')
    if cancel_master and not shadow_cancel:raise RuntimeError('Coordinator cancellation requires actual uncommitted Target publication')
    if os.environ.get('FRONTIERCLOUD_UNIT_SETTLE_NATIVE')=='1' and not source_retain:
        raise RuntimeError('Unit settlement requires Source retention and native Target rollback')
    if cutover and not publish:raise RuntimeError('Cutover requires the complete publication fixture')
    if cleanup and not cutover:raise RuntimeError('Cleanup requires the complete ownership cutover fixture')
    if shadow_cancel and (not publish or cutover):raise RuntimeError('Target shadow cancellation requires uncommitted publication')
    if source_retain and not shadow_cancel:raise RuntimeError('Source retention requires actual Target rollback receipts')
    if publish and os.environ.get('FRONTIERCLOUD_CANCEL_NATIVE')=='1':
        raise RuntimeError('Publication and cancellation require separate fixture tasks')
    started=time.monotonic();fixture=fixtures.ResourceTransferTests('runTest');await fixture.asyncSetUp();f=fixture.f
    checks=[];servers=[];jobs=[];child_process=None;source_process=None;held_reader=None;cleanup_evidence={};selected=contextvars.ContextVar('direct_transfer_peer')
    wire={name:dict(received=0,sent=0,data_sent=0,requests=0) for name in ('source','target')};actual_inputs=[]
    def check(name,value):
        if not value:raise AssertionError(name)
        checks.append(name);print(json.dumps(dict(passed=name)),file=sys.stderr,flush=True)
    class Proxy:
        def __init__(self,name):object.__setattr__(self,'name',name)
        def __getattr__(self,name):return getattr(getattr(selected.get(),self.name),name)
        def __setattr__(self,name,value):setattr(getattr(selected.get(),self.name),name,value)
    class Bound:
        def __init__(self,app,peer):self.app,self.peer=app,peer
        async def __call__(self,scope,receive,send):
            token=selected.set(self.peer);data=False
            if scope['type']=='http':wire[self.peer.name]['requests']+=1
            async def received():
                value=await receive();wire[self.peer.name]['received']+=len(value.get('body',b''));return value
            async def sent(value):
                nonlocal data
                if value['type']=='http.response.start':
                    data=scope['path'].endswith('/transfer/source') and (b'content-type',b'application/octet-stream') in value['headers']
                length=len(value.get('body',b''));wire[self.peer.name]['sent']+=length
                if data:wire[self.peer.name]['data_sent']+=length
                await send(value)
            try:await self.app(scope,received,sent)
            finally:selected.reset(token)
    class MasterTransport(Transport):
        metadata_bytes=0
        discard_commit_ack=False
        def open(self):
            previous=self.client
            super().open()
            if shadow_cancel and previous is None:
                async def record(request):
                    actual_inputs.append(dict(method=request.method,url=str(request.url),path=request.url.raw_path.decode(),
                        headers={name:request.headers.get(name,'') for name in ('x-node-relationship','x-node-time','x-node-nonce','x-node-signature')},
                        value=json.loads(request.content) if request.content else None))
                self.client.event_hooks['request'].append(record)
        async def request(self,*args,**kwargs):
            self.metadata_bytes+=len(p.canonical(kwargs.get('value')))
            result=await super().request(*args,**kwargs);self.metadata_bytes+=len(p.canonical(result))
            if self.discard_commit_ack and args[1].endswith('/commit-ack'):
                self.discard_commit_ack=False;raise OSError('fixture discards received commit acknowledgement')
            return result
    transport=MasterTransport();ca,wrong=TestCA(),TestCA();chain,key=ca.pair();root=f.root
    for name,value in (('chain.pem',chain),('key.pem',key),('ca.pem',ca.root.public_bytes(serialization.Encoding.PEM)),
                       ('wrong.pem',wrong.root.public_bytes(serialization.Encoding.PEM))):
        (root/name).write_bytes(value)
    (root/'key.pem').chmod(0o600)
    patches=[patch.object(internal_nodes,'state',Proxy('store')),patch.object(inventory_api,'state',Proxy('store')),
        patch.object(inventory_api,'inventory',Proxy('inventory')),patch.object(observation_api,'observer',Proxy('observer')),
        patch.object(registry_api,'registry',Proxy('registry')),patch.object(reservation_api,'reservations',Proxy('reservations')),
        patch.object(transfer_api,'transfers',Proxy('transfers')),patch.object(internal_nodes.settings,'TLS_ENABLED',True),
        patch.dict(os.environ,{'SSL_CERT_FILE':str(root/'ca.pem')})]
    if cleanup or cancel_mixed:
        from app.api.v1 import media as media_api
        from app.middleware import native_storage
        from app.services import media_manager
        patches.extend([patch.object(native_storage,'state',Proxy('store')),
            patch.object(native_storage.settings,'DEPLOYMENT_MODE','only_stroge'),
            patch.object(media_api,'engine',Proxy('database')),patch.object(media_api,'node_state',Proxy('store')),
            patch.object(media_api,'MEDIA_ROOT',f.source.roots['media']),patch.object(media_manager,'MEDIA_ROOT',f.source.roots['media'])])
    try:
        for item in patches:item.start()
        master_traffic=Monitor();f.planner.transport=transport;f.planner.traffic=master_traffic
        f.planner.wall=f.planner.spool.wall=time.time
        master=TransferMaster(f.master.store,transport,traffic=master_traffic)
        master.preparation.preview_epoch=f.planner.epoch
        for port,peer in enumerate((f.source,f.target),18823):
            origin=f'https://node.4399.com:{port}';peer.store.node['endpoint']=origin;peer.traffic=Monitor()
            peer.inventory.traffic=peer.observer.traffic=peer.traffic
            peer.transfers=TransferPeers(peer.store,root_provider=lambda peer=peer:peer.roots,traffic=peer.traffic)
            fixture.services.append(peer.transfers)
            async with peer.database.begin() as conn:await conn.execute(update(fs.identity).values(endpoint=origin))
            async with f.master.database.begin() as conn:await conn.execute(update(fs.relationships).where(
                fs.relationships.c.peer_id==peer.store.node['node_id']).values(peer_endpoint=origin))
            app=FastAPI()
            for router in (internal_nodes.router,inventory_api.router,observation_api.router,registry_api.router,reservation_api.router,transfer_api.router):
                app.include_router(router)
            @app.get('/business-load')
            async def load():return Response(bytes(512*1024),media_type='application/octet-stream')
            if (cleanup or cancel_mixed) and peer.name=='source':app.add_middleware(native_storage.NativeStorageDeliveryMiddleware)
            app.add_middleware(BusinessTrafficMiddleware,monitor=peer.traffic)
            server=uvicorn.Server(uvicorn.Config(Bound(app,peer),host='127.0.0.1',port=port,ssl_certfile=str(root/'chain.pem'),
                ssl_keyfile=str(root/'key.pem'),lifespan='off',workers=1,access_log=False,log_level='error'))
            servers.append(server);jobs.append(asyncio.create_task(server.serve()))
        async with asyncio.timeout(5):
            while not all(server.started for server in servers):await asyncio.sleep(.01)
        for name,origin,context in (('wrong_ca_refused',f.source.store.node['endpoint'],ssl.create_default_context(cafile=str(root/'wrong.pem'))),
            ('wrong_hostname_refused','https://wrong.4399.com:18823',ssl.create_default_context(cafile=str(root/'ca.pem')))):
            async with httpx.AsyncClient(verify=context,trust_env=False,timeout=3) as client:
                try:await client.get(origin+'/internal/v1/identity?challenge='+'a'*32)
                except httpx.ConnectError:check(name,True)
                else:raise AssertionError(name)
        async with asyncio.timeout(40):
            while not all(monitor.snapshot()['can_run'] for monitor in (master_traffic,f.source.traffic,f.target.traffic)):await asyncio.sleep(.2)
        check('real_startup_quiet_interval_observed',time.monotonic()-started>=30)
        from app.services.resource_observation import digest
        parents=sorted(('music/native-first','music/native-second'),key=lambda parent:digest(['media',f.source.store.node['node_id'],parent]))
        originals=[await f.media(b'ID3'+b'actual managed bytes\x00'*(450000),path=parents[0]+'/first.mp3'),
            await f.media(b'private original'*3000,path=parents[1]+'/private.mp3',encrypted=True)]
        plan=await f.plan();check('full_native_preview_ready',plan['state']=='ready')
        await master.preparation.prepare(plan['plan_id'],plan['review_sha256'],'native')
        await master.preflight(plan['plan_id'],'native')
        check('verified_staging_and_direct_peer_path_queue_task',True)
        lease=await master.start(plan['plan_id'],'a'*32)
        written=0;original_write=f.target.transfers._write
        def fail_after_partial(descriptor,data,offset):
            nonlocal written
            original_write(descriptor,data,offset);written+=len(data)
            if written>=256*1024:raise OSError('native partial write fault')
        with patch.object(f.target.transfers,'_write',side_effect=fail_after_partial):
            try:await master.copy_one(lease,originals[0][0])
            except p.ProtocolError:check('partial_write_failure_has_no_master_verified_ack',True)
            else:raise AssertionError('partial write fault not reached')
        async with f.master.database.connect() as conn:
            row=(await conn.execute(select(s.tasks).where(s.tasks.c.task_id==plan['plan_id']))).mappings().one()
            check('failed_copy_preserves_master_runner_revision',row['revision']==lease.revision)
            check('failed_window_never_commits_ownership',row['committed_bytes']==0)
        parts=list(f.target.roots['media'].rglob('*.part'))
        check('actual_partial_file_is_preserved',len(parts)==1 and 0<parts[0].stat().st_size<len(originals[0][2]))
        async with asyncio.timeout(5):
            while f.source.transfers.source_lock.locked():await asyncio.sleep(.01)
        async with f.source.database.connect() as conn:
            check('disconnect_releases_migration_read_lease',await conn.scalar(select(func.count()).select_from(s.read_leases))==0)
        check('disconnect_returns_source_database_connection',f.source.database.sync_engine.pool.checkedout()==0)
        await transport.close()
        servers[1].should_exit=True;await asyncio.wait_for(jobs[1],5)
        private=root/'private-target.json';private.write_text(json.dumps(dict(url=str(f.target.database.url),
            vault_key=base64.b64encode(f.target.vault_key).decode(),roots={name:str(value) for name,value in f.target.roots.items()},
            chain=str(root/'chain.pem'),key=str(root/'key.pem'),port=18824)));private.chmod(0o600)
        async def restart_target():
            nonlocal child_process
            await transport.close()
            if child_process is not None:
                child_process.terminate();_,error=await asyncio.wait_for(child_process.communicate(),8)
                if child_process.returncode not in (0,-15):raise AssertionError(error.decode(errors='replace')[-2000:])
            child_process=await asyncio.create_subprocess_exec(sys.executable,'-m','tests.resource_transfer_native','--target-child',str(private),
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            async with asyncio.timeout(10):
                while True:
                    if child_process.returncode is not None:
                        _,error=await child_process.communicate();raise AssertionError(error.decode(errors='replace')[-2000:])
                    try:
                        await transport.identity(f.target.store.node['endpoint'],expected_id=f.target.store.node['node_id'],
                            expected_key=p.public_key(f.target.store.unseal(f.target.store.node['private_key'])),role='Follower')
                        break
                    except (httpx.HTTPError,p.ProtocolError):await asyncio.sleep(.05)
        async def restart_source():
            nonlocal source_process
            await transport.close()
            if source_process is None:
                servers[0].should_exit=True;await asyncio.wait_for(jobs[0],5);await f.source.transfers.close()
            else:
                source_process.terminate();_,error=await asyncio.wait_for(source_process.communicate(),8)
                if source_process.returncode not in (0,-15):raise AssertionError(error.decode(errors='replace')[-2000:])
            source_private=root/'private-source.json'
            source_private.write_text(json.dumps(dict(url=str(f.source.database.url),vault_key=base64.b64encode(f.source.vault_key).decode(),
                roots={name:str(value) for name,value in f.source.roots.items()},chain=str(root/'chain.pem'),key=str(root/'key.pem'),port=18823)))
            source_private.chmod(0o600)
            source_process=await asyncio.create_subprocess_exec(sys.executable,'-m','tests.resource_transfer_native','--peer-child',str(source_private),
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            async with asyncio.timeout(10):
                while True:
                    if source_process.returncode is not None:
                        _,error=await source_process.communicate();raise AssertionError(error.decode(errors='replace')[-2000:])
                    try:
                        await transport.identity(f.source.store.node['endpoint'],expected_id=f.source.store.node['node_id'],
                            expected_key=p.public_key(f.source.store.unseal(f.source.store.node['private_key'])),role='Follower')
                        break
                    except (httpx.HTTPError,p.ProtocolError):await asyncio.sleep(.05)
        async def refresh_heartbeats():
            for peer in (f.source,f.target):
                summary=await transport.request(peer.store.node['endpoint'],'/internal/v1/heartbeat',method='POST',value={},
                    credential=peer.credential,relation=peer.relationship)
                await f.master.store.heartbeat(peer.relationship,True,summary=summary)
        await restart_target()
        check('independent_target_process_reloads_same_identity_and_journal',True)
        # Child also observes its actual thirty-second startup quiet interval.
        await asyncio.sleep(30.1)
        await refresh_heartbeats()
        await master.journal.renew(lease,ttl=60)
        with (patch.object(crypto,'open_premaster',side_effect=AssertionError('premaster opened')),
              patch.object(crypto.Manager,'file_key',side_effect=AssertionError('decrypted media'))):
            for logical,path,data in originals:
                for _ in range(12):
                    lease,result=await master.copy_one(lease,logical)
                    if result['state']=='verified':break
                else:raise AssertionError('copy did not converge')
                check('verified_original_'+('ciphertext' if logical==originals[1][0] else 'plaintext'),result['sha256']==hashlib.sha256(data).hexdigest())
        for _,path,data in originals:check('source_preserved_'+path.name,path.read_bytes()==data)
        staged=list(f.target.roots['media'].rglob('*.part'))
        check('staging_bytes_match_both_complete_originals',sorted(hashlib.sha256(path.read_bytes()).hexdigest() for path in staged)==
              sorted(hashlib.sha256(data).hexdigest() for _,_,data in originals))
        check('no_media_published_on_target',not (f.target.roots['media']/'music').exists())
        async with f.master.database.connect() as conn:
            check('master_preserves_all_original_ownership',set((await conn.execute(select(fs.global_media.c.storage_member_id))).scalars())=={f.source.store.node['node_id']})
            check('master_records_verified_objects_without_committed_bytes',
                set((await conn.execute(select(s.objects.c.state))).scalars())=={'verified'} and await conn.scalar(select(s.tasks.c.committed_bytes))==0)
        check('source_data_plane_carried_actual_bytes',wire['source']['data_sent']>=sum(len(data) for _,_,data in originals))
        check('master_carried_only_bounded_control_metadata',transport.metadata_bytes<sum(len(data) for _,_,data in originals)//10)
        parents_published=False
        ownership_cutover=False;committed_bytes=0
        if publish:
            publication=Publication(f.master.store,transport,traffic=master_traffic)
            lease=await publication.enter(lease);lease=await publication.enter(lease)
            async with f.master.database.connect() as conn:
                units=(await conn.execute(select(s.units.c.unit_id).where(s.units.c.task_id==lease.task_id)
                    .order_by(s.units.c.unit_id))).scalars().all()
            original_inodes={path.name:next(item for item in staged if item.stem==logical).stat().st_ino
                for logical,path,_ in originals}
            restarted=False;selected_units=units if cutover or cancel_mixed else units[:1]
            coordinator=Cutover(f.master.store,transport,traffic=master_traffic)
            from tests.native_traffic_retry import low_traffic_step
            from app.services.resource_migration_scheduler import Scheduler
            async def keep_publication_live():
                await refresh_heartbeats()
                await publication.transfer.journal.renew(lease,ttl=60)
            for index,unit_id in enumerate(selected_units):
                for _ in range(100):
                    result=await low_traffic_step(lambda:publication.target_step(lease,unit_id),keep_publication_live)
                    if result['phase']=='register' and not restarted:
                        # Real second process restart after the whole directory
                        # move, before registry writes. No SQL phases are edited.
                        await restart_target()
                        await asyncio.sleep(30.1)
                        await refresh_heartbeats()
                        await publication.transfer.journal.renew(lease,ttl=60)
                        restarted=True
                    if result['target_published']:break
                else:raise AssertionError('native publication did not converge')
                async with f.master.database.connect() as conn:
                    already_committed=await conn.scalar(select(s.tasks.c.committed_bytes))
                    check('publication_preserves_prior_committed_bytes_'+str(index),already_committed==sum(
                        len(data) for _,_,data in originals[:index]) if cutover or cancel_mixed else already_committed==0)
                    check('publication_does_not_settle_current_parent_quota_'+str(index),await conn.scalar(select(
                        fs.storage_members.c.reserved_bytes).where(fs.storage_members.c.member_id==f.target.store.node['node_id']))==
                        plan['total_bytes']-already_committed)
                for _ in range(100):
                    result=await low_traffic_step(lambda:publication.source_step(lease,unit_id),keep_publication_live)
                    if result['source_verified']:break
                else:raise AssertionError('native Source verification did not converge')
                if cutover or cancel_mixed and index==0:
                    # A real traffic pause can outlive a signed publication
                    # proof. Refresh through the real peers, as the scheduler
                    # does; never extend a stored expiry or close the reader.
                    for _ in range(100):
                        async with f.master.database.connect() as conn:
                            current=(await conn.execute(select(s.units.c.publication).where(
                                s.units.c.task_id==lease.task_id,s.units.c.unit_id==unit_id))).scalar_one()
                        now=time.time()
                        target_proof=current.get('target_reply',{});source_proof=current.get('source_reply',{})
                        if target_proof.get('state')!='published' or not Scheduler._fresh(target_proof,lease,now):
                            await low_traffic_step(lambda:publication.target_step(lease,unit_id),keep_publication_live)
                        elif source_proof.get('phase')!='verified' or not Scheduler._fresh(source_proof,lease,now):
                            await low_traffic_step(lambda:publication.source_step(lease,unit_id),keep_publication_live)
                        else:break
                    else:raise AssertionError('native fresh publication proofs did not converge')
                    if cleanup and index==0:
                        from tests.resource_cleanup_acceptance import HeldReader
                        held_reader=await HeldReader.open(f,originals[0],check)
                    lease,result=await coordinator.commit(lease,unit_id)
                    check('master_commits_whole_parent_'+str(index),result['cutover'] and not result['source_files_removed'])
                    if index==0 and cutover:
                        transport.discard_commit_ack=True
                        try:await coordinator.acknowledge_one(lease,unit_id)
                        except OSError:check('controller_discards_received_native_ack_before_master_journal',True)
                        else:raise AssertionError('commit acknowledgement was not discarded')
                        restart_at=time.monotonic();await restart_target()
                    for _ in range(3):
                        if (await coordinator.acknowledge_one(lease,unit_id))['native_acknowledged']:break
                    else:raise AssertionError('native commit acknowledgement did not converge')
                    if index==0 and cutover:
                        check('light_commit_ack_recovers_after_real_restart_during_startup_quiet',time.monotonic()-restart_at<30)
                        if len(selected_units)>1:
                            await asyncio.sleep(30.1);await refresh_heartbeats()
                            await publication.transfer.journal.renew(lease,ttl=60)
            check('real_target_restart_after_atomic_directory_move_resumes_registration',restarted)
            published_originals=originals[:len(selected_units)]
            for logical,path,data in published_originals:
                target=f.target.roots['media']/path.relative_to(f.source.roots['media'])
                check('published_original_bytes_'+path.name,target.read_bytes()==data)
                check('published_original_inode_without_second_payload_copy_'+path.name,
                    target.stat().st_ino==original_inodes[path.name] and target.stat().st_nlink==1)
            async with f.target.database.connect() as conn:
                check('target_registered_only_published_native_media',await conn.scalar(text('SELECT COUNT(*) FROM media_objects'))==len(published_originals))
                # The native fixture uses TransferMaster directly; get the ID
                # from the immutable plan binding rather than a mocked grant.
                binding=(await conn.execute(select(s.peer_bindings.c.binding_json).where(
                    s.peer_bindings.c.logical_id==originals[1][0]))).scalar_one()
                obj=json.loads(binding)['object'];encrypted=await crypto.encryption_for_id(conn,obj['source_object'])
                if len(published_originals)==len(originals):
                    check('target_preserves_encrypted_descriptor_digest',encrypted is not None and
                        hashlib.sha256(p.canonical(encrypted.to_dict())).hexdigest()==obj['encryption_sha256'])
                else:check('private_encrypted_copy_has_no_published_descriptor',encrypted is None)
                check('target_never_imported_master_catalog',await conn.scalar(select(func.count()).select_from(fs.global_media))==0)
            if not cutover and not cancel_mixed:
                async with f.master.database.connect() as conn:
                    check('target_publication_does_not_switch_global_ownership',set((await conn.execute(
                        select(fs.global_media.c.storage_member_id))).scalars())=={f.source.store.node['node_id']})
                    check('target_publication_keeps_committed_bytes_zero',await conn.scalar(select(s.tasks.c.committed_bytes))==0)
            check('source_originals_unchanged_after_publication',all(path.read_bytes()==data for _,path,data in originals))
            check('unpublished_private_copies_are_retained',len(list(f.target.roots['media'].rglob('*.part')))==len(originals)-len(published_originals))
            check('fresh_source_unit_membership_and_metadata_verified_over_actual_tls',True)
            check('fresh_source_verification_preserves_all_originals',all(path.read_bytes()==data for _,path,data in originals))
            check('master_control_stays_bounded_after_fresh_source_verification',
                transport.metadata_bytes<sum(len(data) for _,_,data in originals)//10)
            parents_published=len(selected_units)==len(units)
            if cutover:
                async with f.master.database.connect() as conn:
                    owners=set((await conn.execute(select(fs.global_media.c.storage_member_id))).scalars())
                    committed_bytes=await conn.scalar(select(s.tasks.c.committed_bytes))
                    rows=[dict(row) for row in (await conn.execute(select(fs.global_media))).mappings()]
                    check('all_logical_ids_now_owned_by_original_target',owners=={f.target.store.node['node_id']} and
                        {row['media_id'] for row in rows}=={item[0] for item in originals})
                    check('master_committed_bytes_and_owned_reservation_settle_exactly',committed_bytes==sum(len(data) for _,_,data in originals) and
                        await conn.scalar(select(s.capacity.c.reserved_bytes))==0)
                for peer in (f.source,f.target):
                    async with peer.database.connect() as conn:
                        check('native_commit_keeps_fences_'+peer.name,await conn.scalar(select(func.count()).select_from(s.scopes))==len(units))
                        check('native_committed_units_'+peer.name,set((await conn.execute(select(s.units.c.state))).scalars())=={'committed'})
                async with f.target.database.connect() as conn:
                    check('native_target_quota_not_charged_twice_after_restart',await conn.scalar(select(fs.storage_members.c.used_bytes).where(
                        fs.storage_members.c.member_id==f.target.store.node['node_id']))==committed_bytes and await conn.scalar(select(s.capacity.c.reserved_bytes))==0)
                for peer in (f.source,f.target):
                    summary=await transport.request(peer.store.node['endpoint'],'/internal/v1/heartbeat',method='POST',
                        value={},credential=peer.credential,relation=peer.relationship)
                    await f.master.store.heartbeat(peer.relationship,True,summary=summary)
                    check('actual_authenticated_outbound_heartbeat_'+peer.name,summary.get('protocol')==p.PROTOCOL_VERSION)
                for index,unit_id in enumerate(units):
                    for _ in range(100):
                        try:result=await coordinator.verify_target(lease,unit_id)
                        except TransferError as exc:
                            if str(exc)!='migration_transfer_waiting_low_traffic':raise
                            await asyncio.sleep(.5);continue
                        if result['target_verified']:break
                    else:raise AssertionError('native post-commit Target verification did not converge')
                    check('fresh_target_unit_proof_over_actual_tls_'+str(index),not result['source_files_removed'])
                context=ssl.create_default_context(cafile=str(root/'ca.pem'))
                async with httpx.AsyncClient(verify=context,trust_env=False,timeout=5) as reader:
                    for logical,path,data in originals:
                        row=next(row for row in rows if row['media_id']==logical)
                        token=p.media_token(f.target.credential,f.target.relationship,f.master.store.node['node_id'],
                            f.target.store.node['node_id'],row['object_id'],logical,int(time.time()))
                        response=await reader.get(f.target.store.node['endpoint']+'/internal/v1/media/'+row['object_id'],
                            headers={'X-Media-Capability':token,'Range':'bytes=3-9','Origin':f.master.store.node['endpoint']})
                        check('real_native_media_range_after_cutover_'+path.name,response.status_code==206 and response.content==data[3:10] and
                            response.headers.get('content-range')==f'bytes 3-9/{len(data)}' and response.headers.get('x-media-resource-id')==logical)
                async with f.target.database.connect() as conn:
                    leases=[dict(row) for row in (await conn.execute(select(s.read_leases).where(s.read_leases.c.purpose=='playback'))).mappings()]
                    check('actual_tls_media_reads_keep_durable_leases_after_response',len(leases)==len(originals) and
                        {row['logical_id'] for row in leases}=={row[0] for row in originals} and
                        all(row['task_id']=='' and row['expires_at']>time.time() for row in leases))
                check('source_originals_retained_after_global_cutover',all(path.read_bytes()==data for _,path,data in originals))
                ownership_cutover=True
                if cleanup:
                    from tests.resource_cleanup_acceptance import finish
                    cleanup_evidence=await finish(f,transport,master_traffic,lease,units,originals,held_reader,check)
        if shadow_cancel:
            if cancel_mixed:
                from tests.resource_cancel_mixed_native import finish
                cleanup_evidence=await finish(f,transport,master_traffic,lease,units,originals,check,actual_inputs)
            elif cancel_master:
                from tests.resource_cancel_master_native import finish
                cleanup_evidence=await finish(f,transport,master_traffic,lease,units,originals,restart_source,restart_target,check,actual_inputs)
                if cancel_census:
                    from tests.resource_cancel_census_native import finish as finish_census
                    cleanup_evidence.update(await finish_census(f,transport,master_traffic,originals,restart_source,check,actual_inputs,
                        restart_target=restart_target))
            else:
                from tests.resource_cancel_target_native import finish
                cleanup_evidence=await finish(f,transport,master_traffic,lease,units,originals,restart_target,check,actual_inputs,
                    restart_source=restart_source if source_retain else None)
        cancelled=False
        if os.environ.get('FRONTIERCLOUD_CANCEL_NATIVE')=='1':
            cancellation=Cancellation(f.master.store,transport,traffic=master_traffic)
            unknown=staged[0].parent/'test-owned-unknown'
            unknown.write_bytes(b'preserve this test-owned marker')
            await cancellation.request(plan['plan_id'],lease.revision,'native')
            refused=False
            for _ in range(24):
                try:await cancellation.advance(plan['plan_id'],'native')
                except TransferError as exc:
                    check('signed_exact_unknown_stage_error_reaches_master',str(exc)=='migration_abort_unknown_stage')
                    refused=True;break
                await asyncio.sleep(.05)
            check('unknown_staging_blocks_final_settlement',refused and unknown.read_bytes()==b'preserve this test-owned marker')
            async with f.master.database.connect() as conn:
                check('unknown_stage_keeps_master_reservation',await conn.scalar(select(fs.storage_members.c.reserved_bytes).where(
                    fs.storage_members.c.member_id==f.target.store.node['node_id']))==plan['total_bytes'])
            # Fixture-owned injection is retained outside the task namespace;
            # the product never moves or deletes an unknown file.
            retained=root/'retained-test-owned-marker';unknown.rename(retained)
            for _ in range(24):
                status=await cancellation.advance(plan['plan_id'],'native')
                if status['state']=='cancelled':break
                await asyncio.sleep(.05)
            else:raise AssertionError('native cancellation did not converge')
            check('controlled_cancel_finishes_after_explicit_fixture_resolution',status['state']=='cancelled')
            check('unknown_marker_remains_intact',retained.read_bytes()==b'preserve this test-owned marker')
            check('all_registered_private_parts_removed',not list(f.target.roots['media'].rglob('*.part')))
            for peer in (f.master,f.source,f.target):
                async with peer.database.connect() as conn:
                    counts=[int(await conn.scalar(select(func.count()).select_from(table))) for table in (s.scopes,s.capacity,s.drains)]
                    check('no_owned_fences_or_quota_'+peer.name,sum(counts)==0)
            check('all_source_originals_still_unchanged_after_cancel',all(path.read_bytes()==data for _,path,data in originals))
            cancelled=True
        samples=[]
        def usage():return int(dict(line.split() for line in Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines())['usage_usec'])
        for _ in range(5):
            previous,wall=usage(),time.monotonic();await asyncio.sleep(1);samples.append(round((usage()-previous)/10000/(time.monotonic()-wall),3))
        check('five_whole_cgroup_quiescent_samples_under_twenty_percent',all(value<20 for value in samples))
        quota,period=Path('/sys/fs/cgroup/cpu.max').read_text().split();memory=int(Path('/sys/fs/cgroup/memory.max').read_text())
        check('actual_one_cpu_512mib_parent_and_child_limit',quota==period and memory==512*1024**2)
        memory_events={name:int(value) for name,value in (line.split() for line in Path('/sys/fs/cgroup/memory.events').read_text().splitlines())}
        check('whole_cgroup_has_no_oom_or_oom_kill',memory_events.get('oom',0)==0 and memory_events.get('oom_kill',0)==0)
        process_rss={}
        for name,process in (('master_fixture',None),('target',child_process),('source',source_process)):
            if name!='master_fixture':
                if process is None:continue
                check('native_peer_process_still_alive_'+name,process.returncode is None)
            pid=os.getpid() if process is None else process.pid
            status=dict(line.split(':',1) for line in Path('/proc/'+str(pid)+'/status').read_text().splitlines() if ':' in line)
            process_rss[name]=int(status['VmRSS'].split()[0])*1024
        memory_stat={name:int(value) for name,value in (line.split() for line in Path('/sys/fs/cgroup/memory.stat').read_text().splitlines())}
        print(json.dumps(dict(format='frontiercloud-python-direct-transfer-native',version=1,count=len(checks),checks=checks,
            tls_verified=True,target_process_restart=True,media_decrypted=False,published=ownership_cutover,committed_bytes=committed_bytes,
            controlled_cancel=cancelled,target_parents_published=parents_published,global_cutover=ownership_cutover,
            **dict(dict(source_cleanup=False,real_cleanup_grace=False,task_completion=False),**cleanup_evidence),
            source_bytes=sum(len(data) for _,_,data in originals),wire=wire,master_metadata_bytes=transport.metadata_bytes,cpu_samples=samples,
            cpu_quota=int(quota),cpu_period=int(period),memory_limit=memory,memory_peak=int(Path('/sys/fs/cgroup/memory.peak').read_text()),
            memory_current=int(Path('/sys/fs/cgroup/memory.current').read_text()),memory_events=memory_events,process_rss_bytes=process_rss,
            memory_stat={name:memory_stat[name] for name in ('anon','file','kernel','shmem') if name in memory_stat},
            elapsed_seconds=round(time.monotonic()-started,4)),sort_keys=True))
    finally:
        if held_reader is not None:await held_reader.close()
        # Close the controlling HTTP pool before stopping its peer server, so
        # no fixture-owned keepalive connection delays graceful shutdown.
        await transport.close()
        shutdown_error=None
        for process in (source_process,child_process):
            if process is None or process.returncode is not None:continue
            process.terminate()
            try:await asyncio.wait_for(process.communicate(),8)
            except TimeoutError as exc:
                process.kill();out,err=await process.communicate()
                print(err.decode(errors='replace')[-4000:],file=sys.stderr)
                shutdown_error=exc
        for server in servers:server.should_exit=True
        await asyncio.wait_for(asyncio.gather(*jobs,return_exceptions=True),8)
        for item in reversed(patches):item.stop()
        await fixture.asyncTearDown()
        if shutdown_error:raise shutdown_error


if __name__=='__main__':asyncio.run(child(sys.argv[2]) if len(sys.argv)>1 and sys.argv[1] in {'--target-child','--peer-child'} else main())
