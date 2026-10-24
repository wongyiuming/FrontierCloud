"""Three real stores/files, durable coordinator recovery; public gate stays closed."""
import copy
import os
import unittest
import uuid
from unittest.mock import patch

import httpx
from sqlalchemy import func,select,update

from app.services import media_crypto,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_cancel_master import PublicationCancellation
from app.services.resource_cancel_source_peers import SourceCancellation
from app.services.resource_cancel_target import TargetCancellation
from app.services.resource_cancel_unit_peers import UnitSettlement
from app.services.resource_transfer_peers import TransferPeers
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_cancel_authority as fixtures


def native_transport(f,assert_equal):
    original=f.transport.request;calls=[]
    async def request(origin,path,**kwargs):
        actions={'cancel-source-stop':('source',SourceCancellation,'stop'),'cancel-stop':('target',TargetCancellation,'stop'),
            'cancel-target':('target',TargetCancellation,'advance'),'cancel-source-retain':('source',SourceCancellation,'advance'),
            'cancel-unit-settle':(None,UnitSettlement,'advance')}
        action=actions.get(path.rsplit('/',1)[-1])
        if action is None:return await original(origin,path,**kwargs)
        peer=next(peer for peer in (f.source,f.target) if peer.store.node['endpoint']==origin)
        if action[0] is not None:assert_equal(peer.name,action[0])
        assert_equal((kwargs['credential'],kwargs['relation']),(peer.credential,peer.relationship))
        calls.append((peer.name,path,copy.deepcopy(kwargs['value'])))
        async with peer.database.connect() as conn:relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
        result=await getattr(action[1](peer.transfers),action[2])(peer.store.node,relation,**kwargs['value'])
        return p.sign(peer.store.unseal(peer.store.node['private_key']),result)
    f.transport.request=request
    return calls


@unittest.skipUnless(os.name=='posix','cancellation coordinator requires native Linux inodes')
class MasterCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base=fixtures.CancellationAuthorityTests('runTest');await self.base.asyncSetUp()
        await self.base.prepare();await self.base.cancelling()
        self.f,self.lease=self.base.f,self.base.lease;self.resources=self.base.fixture.resources
        self.calls=native_transport(self.f,self.assertEqual);self.extra=[];self.service=self.new_service()

    async def asyncTearDown(self):
        for service in self.extra:await service.close()
        await self.base.asyncTearDown()

    def new_service(self):return PublicationCancellation(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)

    async def step(self):
        self.lease,result=await self.service.advance(self.lease)
        return result

    async def finish(self):
        phases=[]
        for _ in range(140):
            result=await self.step();phases.append(result['phase'])
            if result['units_settled']:return result,phases
        self.fail('coordinator did not settle every copied parent')

    async def task(self):
        async with self.f.master.database.connect() as conn:return dict((await conn.execute(select(s.tasks))).mappings().one())

    async def originals(self):
        for _,path,data in self.resources:self.assertEqual(path.read_bytes(),data)

    async def reserved(self,peer):
        async with peer.database.connect() as conn:return await conn.scalar(select(s.capacity.c.reserved_bytes))

    async def test_plain_and_cipher_parents_settle_without_source_business_or_task_completion(self):
        async with self.f.master.database.connect() as conn:before=[dict(row) for row in (await conn.execute(select(fs.global_media))).mappings()]
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('media decrypted')),
                patch.object(media_crypto,'open_premaster',side_effect=AssertionError('premaster opened'))):result,phases=await self.finish()
        self.assertFalse(result['task_completed']);self.assertIn('source_proof_verified',phases);self.assertIn('source_retained',phases)
        self.assertEqual(sum(path.endswith('/cancel-source-stop') for _,path,_ in self.calls),1)
        self.assertEqual(sum(path.endswith('/cancel-stop') for _,path,_ in self.calls),1)
        async with self.f.master.database.connect() as conn:
            self.assertEqual([dict(row) for row in (await conn.execute(select(fs.global_media))).mappings()],before)
            self.assertEqual(await conn.scalar(select(s.tasks.c.state)),'cancelling');self.assertEqual(await conn.scalar(select(s.tasks.c.committed_bytes)),0)
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),0)
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.drains)),1)
            unit=(await conn.execute(select(s.units))).mappings().one()
            self.assertEqual(unit['state'],'cancelled');self.assertEqual(set(unit['publication']['cancel_native_work']['progress']),{'source_proof','target','source'})
        for peer in (self.f.master,self.f.target):self.assertEqual(await self.reserved(peer),0)
        for _,path,_ in self.resources:self.assertFalse((self.f.target.roots['media']/path.relative_to(self.f.source.roots['media'])).exists())
        await self.originals()

    async def test_coordinator_and_native_source_restart_during_both_metadata_phases(self):
        restarted=set()
        for _ in range(180):
            result=await self.step();phase=result['phase'];self.service=self.new_service()
            if phase in {'source_proof_objects','source_objects'} and phase not in restarted:
                restarted.add(phase);await self.f.source.transfers.close()
                self.f.source.transfers=TransferPeers(self.f.source.store,root_provider=lambda:self.f.source.roots,traffic=self.f.traffic,wall=lambda:self.f.now)
                self.extra.append(self.f.source.transfers)
            if result['units_settled']:break
        else:self.fail('native metadata process recovery did not converge')
        self.assertEqual(restarted,{'source_proof_objects','source_objects'})
        self.assertEqual(sum(path.endswith('/cancel-source-stop') for _,path,_ in self.calls),1);await self.originals()

    async def test_lost_stop_removal_retention_and_settlement_replies_recover_without_double_refund(self):
        original=self.f.transport.request;lost=set()
        async def lose(origin,path,**kwargs):
            result=await original(origin,path,**kwargs);phase=result.get('payload',{}).get('phase')
            key=(origin,path)
            if path.rsplit('/',1)[-1] in {'cancel-source-stop','cancel-stop','cancel-target','cancel-source-retain','cancel-unit-settle'} and phase in {'stopped','cancelled','retained','settled'} and key not in lost:
                lost.add(key);raise httpx.ReadTimeout('Controlled received reply discarded')
            return result
        self.f.transport.request=lose
        for _ in range(180):
            try:result=await self.step()
            except httpx.ReadTimeout:self.service=self.new_service();continue
            if result['units_settled']:break
        else:self.fail('lost reply recovery did not converge')
        self.assertEqual(len(lost),6)
        for peer in (self.f.master,self.f.target):self.assertEqual(await self.reserved(peer),0)
        self.assertEqual((await self.task())['state'],'cancelling');await self.originals()

    async def test_stop_acknowledgements_are_light_at_peak_and_fresh_verification_waits(self):
        self.f.traffic.record(egress=100*1024**2)
        self.assertEqual((await self.step())['phase'],'source_stopped');self.assertEqual((await self.step())['phase'],'target_stopped')
        amount=sum(len(data) for _,_,data in self.resources)
        with self.assertRaisesRegex(TransferError,'waiting_low_traffic'):await self.step()
        for peer in (self.f.master,self.f.target):self.assertEqual(await self.reserved(peer),amount)
        await self.originals();self.f.traffic._clock=lambda:1100;self.f.traffic._quiet_since=1000
        self.assertTrue((await self.finish())[0]['units_settled'])

    async def test_pending_source_retention_query_has_no_settlement_authority(self):
        await self.step();await self.step()
        async with self.f.master.database.begin() as conn:task,relations=await self.service._context(conn,self.lease)
        async with self.f.master.database.connect() as conn:unit=dict((await conn.execute(select(s.units))).mappings().one())
        selected=self.service.units._selected(task,unit);before=await self.reserved(self.f.master)
        result,_=await self.service.client.source(task,relations,self.lease,selected,None,None,query=True)
        self.assertEqual((result['phase'],result['receipt']),('pending',None));self.assertEqual(await self.reserved(self.f.master),before)
        await self.originals()

    async def test_source_writer_wait_does_not_reset_a_later_verification_scan(self):
        async with self.f.source.transfers.source_lock:self.assertEqual((await self.step())['phase'],'source_waiting_writer')
        _,phases=await self.finish();self.assertIn('source_proof_verified',phases)
        self.assertEqual(sum(path.endswith('/cancel-source-stop') for _,path,_ in self.calls),2);await self.originals()

    async def test_damaged_signed_or_null_stop_journal_refuses_without_clearing_fences(self):
        await self.step();await self.step();task=await self.task();original=task['plan'];journal=original['cancel_native_stops']
        for changed in (None,dict(journal,version=True),dict(journal,epoch=True),dict(journal,runner_id='0'*32),dict(journal,receipts={})):
            async with self.f.master.database.begin() as conn:await conn.execute(update(s.tasks).values(plan={**original,'cancel_native_stops':changed}))
            with self.assertRaisesRegex(TransferError,'stop_journal_changed'):await self.step()
            self.assertEqual(await self.reserved(self.f.master),sum(len(data) for _,_,data in self.resources));await self.originals()
        async with self.f.master.database.begin() as conn:await conn.execute(update(s.tasks).values(plan=original))
        self.assertTrue((await self.finish())[0]['units_settled'])

    async def test_durable_copy_proof_avoids_scanning_the_whole_task_on_every_parent_page(self):
        from sqlalchemy import event
        await self.step();await self.step();scans=[]
        def observed(conn,cursor,statement,parameters,context,executemany):
            if 'FROM migration_objects' in statement and 'migration_objects.unit_id' not in statement:scans.append(statement)
        event.listen(self.f.master.database.sync_engine,'before_cursor_execute',observed)
        try:self.assertTrue((await self.finish())[0]['units_settled'])
        finally:event.remove(self.f.master.database.sync_engine,'before_cursor_execute',observed)
        self.assertEqual(scans,[]);await self.originals()

    async def test_failed_master_stop_audit_keeps_native_fence_and_retries_same_task(self):
        log=self.f.master.store.log
        async def fail(conn,action,*args,**kwargs):
            if action=='migration_cancel_native_stop_observed':raise RuntimeError('Controlled stop audit rollback')
            return await log(conn,action,*args,**kwargs)
        with patch.object(self.f.master.store,'log',side_effect=fail):
            with self.assertRaises(RuntimeError):await self.step()
        self.assertNotIn('cancel_native_stops',(await self.task())['plan'])
        async with self.f.source.database.connect() as conn:
            header=await conn.scalar(select(s.peer_reservations.c.payload));self.assertTrue(header['transfer']['cancel_started'])
        self.assertTrue((await self.finish())[0]['units_settled']);await self.originals()

    async def test_inconsistent_verified_byte_count_is_refused_before_any_native_stop(self):
        reserved=[await self.reserved(peer) for peer in (self.f.master,self.f.target)]
        async with self.f.master.database.begin() as conn:
            row=(await conn.execute(select(s.objects).order_by(s.objects.c.logical_id).limit(1))).mappings().one()
            await conn.execute(update(s.objects).where(s.objects.c.logical_id==row['logical_id']).values(verified_bytes=row['verified_bytes']-1))
        with self.assertRaisesRegex(TransferError,'migration_cancel_copy_journal_changed'):await self.step()
        self.assertEqual(self.calls,[])
        await self.originals()
        self.assertEqual([await self.reserved(peer) for peer in (self.f.master,self.f.target)],reserved)
        async with self.f.master.database.begin() as conn:await conn.execute(update(s.objects).where(s.objects.c.logical_id==row['logical_id']).values(verified_bytes=row['verified_bytes']))
        self.assertTrue((await self.finish())[0]['units_settled']);await self.originals()

    async def test_changed_source_original_is_preserved_and_keeps_master_quota_and_scopes(self):
        for _ in range(80):
            if (await self.step())['phase']=='target_objects':break
        else:self.fail('Target unlink did not start')
        _,path,_=self.resources[0];path.write_bytes(b'preserve outside change to Source')
        with self.assertRaisesRegex(TransferError,'source_changed'):await self.finish()
        self.assertEqual(path.read_bytes(),b'preserve outside change to Source')
        self.assertEqual(await self.reserved(self.f.master),sum(len(data) for _,_,data in self.resources))
        async with self.f.master.database.connect() as conn:self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),2)

    async def test_boolean_native_reply_is_refused_before_persisting_a_target_receipt(self):
        original=self.f.transport.request;tamper=True
        async def request(origin,path,**kwargs):
            envelope=await original(origin,path,**kwargs)
            if tamper and path.endswith('/cancel-target'):
                return p.sign(self.f.target.store.unseal(self.f.target.store.node['private_key']),{**envelope['payload'],'deleted_count':True})
            return envelope
        self.f.transport.request=request
        with self.assertRaisesRegex(TransferError,'target_reply_invalid'):await self.finish()
        self.assertEqual(await self.reserved(self.f.master),sum(len(data) for _,_,data in self.resources))
        tamper=False;self.assertTrue((await self.finish())[0]['units_settled']);await self.originals()

    async def test_actual_asgi_hmac_all_native_controls_and_nonce_replay(self):
        from fastapi import FastAPI
        from app.api import internal_nodes,internal_resource_inventory,internal_resource_transfer as api
        original=self.f.transport.request;seen=[]
        async def request(origin,path,**kwargs):
            if path.rsplit('/',1)[-1] not in {'cancel-source-stop','cancel-stop','cancel-target','cancel-source-retain','cancel-unit-settle','verify-source'}:
                return await original(origin,path,**kwargs)
            peer=next(peer for peer in (self.f.source,self.f.target) if peer.store.node['endpoint']==origin)
            application=FastAPI();application.include_router(api.router);raw=p.canonical(kwargs['value'])
            headers=p.auth_headers(peer.credential,peer.relationship,'POST',path,raw)
            with (patch.object(internal_nodes,'state',peer.store),patch.object(internal_resource_inventory,'state',peer.store),patch.object(api,'transfers',peer.transfers)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url=origin) as client:
                    response=await client.post(path,content=raw,headers=headers);self.assertEqual(response.status_code,200,response.text)
                    if path not in seen:self.assertEqual((await client.post(path,content=raw,headers=headers)).status_code,401)
            seen.append(path);return response.json()
        self.f.transport.request=request
        self.assertTrue((await self.finish())[0]['units_settled'])
        self.assertEqual({path.rsplit('/',1)[-1] for path in seen},{'cancel-source-stop','cancel-stop','cancel-target','cancel-source-retain','cancel-unit-settle','verify-source'})
        await self.originals()


@unittest.skipUnless(os.name=='posix','mixed cancellation uses actual copied native parents')
class MasterCancellationVariantsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.test_resource_cutover import CutoverTests
        self.base=CutoverTests('runTest');await self.base.asyncSetUp();self.pub=self.base.fixture.fixture;self.f=self.base.f
        self.calls=native_transport(self.f,self.assertEqual);self.service=self.new_service()

    async def asyncTearDown(self):await self.base.asyncTearDown()
    new_service=MasterCancellationTests.new_service
    step=MasterCancellationTests.step
    finish=MasterCancellationTests.finish

    async def cancel(self,lease):
        async with self.f.master.database.begin() as conn:
            row=(await conn.execute(select(s.tasks))).mappings().one()
            await conn.execute(update(s.tasks).values(state='cancelling',revision=row['revision']+1))
            await conn.execute(update(s.runners).values(expires_at=0))
        self.lease=await self.service.t.journal.claim(lease.task_id,uuid.uuid4().hex,ttl=60)

    async def partial(self,phase):
        await self.pub.prepare(pair=True);await self.pub.reach(phase);await self.cancel(self.pub.lease)
        self.assertTrue((await self.finish())[0]['units_settled'])
        for _,path,data in self.pub.resources:
            self.assertEqual(path.read_bytes(),data)
            self.assertFalse((self.f.target.roots['media']/path.relative_to(self.f.source.roots['media'])).exists())

    async def test_private_assembly_cancels_through_the_coordinator(self):await self.partial('assembling')
    async def test_moved_parent_with_incomplete_registry_cancels_through_the_coordinator(self):await self.partial('register')

    async def mixed(self,*,committed_encrypted=False):
        transfer=self.pub.fixture
        from app.services.resource_observation import digest
        parents=sorted(('music/committed','music/retained'),key=lambda parent:digest(['media',self.f.source.store.node['node_id'],parent]))
        self.f.migration_committed_parent,self.f.migration_retained_parent=parents
        # Generated node IDs change hash order. The committed fixture parent
        # must be the actual first pipeline unit, not a hard-coded path name.
        self.resources=[await self.f.media(b'ID3 committed original'*300,path=parents[0]+'/song.mp3',encrypted=committed_encrypted),
            await self.f.media(b'ID3 retained original'*300,path=parents[1]+'/plain.mp3'),
            await self.f.media(b'opaque retained ciphertext'*300,path=parents[1]+'/cipher.mp3',encrypted=True)]
        transfer.plan=await self.f.plan();self.assertEqual(transfer.plan['state'],'ready',transfer.plan)
        await transfer.fixture.admission.prepare(transfer.plan['plan_id'],transfer.plan['review_sha256'],'admin');await transfer.queue()
        for logical,_,_ in self.resources:
            transfer.logical=logical
            for _ in range(12):
                if (await transfer.step())['state']=='verified':break
            else:self.fail('mixed native copy did not converge')
        lease=await self.pub.service.enter(transfer.lease);lease=await self.pub.service.enter(lease)
        async with self.f.master.database.connect() as conn:
            self.committed=await conn.scalar(select(s.units.c.unit_id).where(s.units.c.parent_path==parents[0]))
        for _ in range(40):
            if (await self.pub.service.target_step(lease,self.committed))['target_published']:break
        else:self.fail('committed Target parent did not publish')
        for _ in range(20):
            if (await self.pub.service.source_step(lease,self.committed))['source_verified']:break
        else:self.fail('committed Source parent did not verify')
        lease,result=await self.base.service.commit(lease,self.committed);self.record=result['commit'];await self.cancel(lease)

    async def assert_mixed(self):
        self.assertTrue((await self.finish())[0]['units_settled'])
        amount=len(self.resources[0][2]);target_id=self.f.target.store.node['node_id'];source_id=self.f.source.store.node['node_id']
        async with self.f.master.database.connect() as conn:
            task=(await conn.execute(select(s.tasks))).mappings().one();self.assertEqual((task['state'],task['committed_bytes']),('cancelling',amount))
            units=list((await conn.execute(select(s.units))).mappings());self.assertEqual({u['state'] for u in units},{'committed','cancelled'})
            self.assertEqual({u['native_acknowledged'] for u in units},{1})
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),2)
            rows={r['media_id']:r['storage_member_id'] for r in (await conn.execute(select(fs.global_media))).mappings()}
            self.assertEqual(rows,{logical:target_id if index==0 else source_id for index,(logical,_,_) in enumerate(self.resources)})
            self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes)),0)
        for index,(_,path,data) in enumerate(self.resources):
            self.assertEqual(path.read_bytes(),data);target=self.f.target.roots['media']/path.relative_to(self.f.source.roots['media'])
            if index==0:self.assertEqual(target.read_bytes(),data)
            else:self.assertFalse(target.exists())
        for peer in (self.f.source,self.f.target):
            async with peer.database.connect() as conn:self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),1)
        self.assertEqual(self.record['cleanup_after']-self.record['committed_at'],3600)

    async def test_mixed_committed_parent_is_acknowledged_not_rolled_back_or_cleaned_early(self):
        await self.mixed();await self.assert_mixed()

    async def test_lost_commit_acknowledgements_replay_without_charging_native_target_twice(self):
        await self.mixed();original=self.f.transport.request;lost=set()
        async def lose(origin,path,**kwargs):
            result=await original(origin,path,**kwargs)
            if path.endswith('/commit-ack') and origin not in lost:
                lost.add(origin);raise httpx.ReadTimeout('Controlled lost native commit acknowledgement')
            return result
        self.f.transport.request=lose
        for _ in range(12):
            try:await self.step()
            except httpx.ReadTimeout:self.service=self.new_service()
            if len(lost)==2:break
        self.assertEqual(len(lost),2);await self.assert_mixed()
        async with self.f.target.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(fs.storage_members.c.used_bytes)),len(self.resources[0][2]))

    async def test_opaque_recording_footers_and_current_master_business_are_retained(self):
        from app.services import karaoke_schema as ks
        await self.base.prepare_recordings();await self.cancel(self.base.lease)
        async with self.f.master.database.connect() as conn:before=[dict(row) for row in (await conn.execute(select(ks.recordings))).mappings()]
        with (patch.object(media_crypto,'open_premaster',side_effect=AssertionError('recording premaster')),
                patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('recording decrypted'))):await self.finish()
        async with self.f.master.database.connect() as conn:self.assertEqual([dict(row) for row in (await conn.execute(select(ks.recordings))).mappings()],before)
        for _,path,data in self.base.resources:self.assertEqual(path.read_bytes(),data)
