"""Real native per-parent settlement, independent from whole-task cancellation."""
import os
import hashlib
import time
import unittest
import uuid
from unittest.mock import patch

import httpx
from sqlalchemy import delete,func,insert,select,text,update

from app.services import media_crypto,media_crypto_schema as cs,resource_migration_schema as s,resource_readers
from app.services.federation import protocol as p,schema as fs
from app.services.resource_cancel_unit_master import UnitCancellation
from app.services.resource_cancel_unit_peers import UnitSettlement
from app.services.resource_cancel_unit_receipts import verify
from app.services.resource_peer_reservations import ReservationError
from app.services.resource_transfer_peers import TransferPeers
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_cancel_source_peers as fixtures


def native_transport(f,assert_equal):
    original=f.transport.request
    async def request(origin,path,**kwargs):
        if not path.endswith('/cancel-unit-settle'):return await original(origin,path,**kwargs)
        peer=next(peer for peer in (f.source,f.target) if peer.store.node['endpoint']==origin)
        async with peer.database.connect() as conn:relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
        assert_equal((kwargs['credential'],kwargs['relation']),(peer.credential,peer.relationship))
        result=await UnitSettlement(peer.transfers).advance(peer.store.node,relation,**kwargs['value'])
        return p.sign(peer.store.unseal(peer.store.node['private_key']),result)
    f.transport.request=request


@unittest.skipUnless(os.name=='posix','settlement requires actual native Linux files')
class UnitSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base=fixtures.SourceRetentionTests('runTest');await self.base.asyncSetUp()
        self.f,self.unit=self.base.f,self.base.unit
        await self.base.stop();self.source_receipt=(await self.base.finish())['receipt']
        self.target_receipt=self.base.target_receipt;self.lease=self.base.target.lease
        native_transport(self.f,self.assertEqual)
        self.service=self.new_service();self.extra=[]

    async def asyncTearDown(self):
        for service in self.extra:await service.close()
        await self.base.asyncTearDown()

    def new_service(self):return UnitCancellation(self.f.master.store,self.f.transport,traffic=self.f.traffic,wall=lambda:self.f.now)

    async def step(self):
        self.lease,result=await self.service.advance(self.lease,self.unit,self.source_receipt,self.target_receipt)
        self.base.target.lease=self.lease
        return result

    async def finish(self):
        for _ in range(40):
            result=await self.step()
            if result['unit_cancelled']:return result
        self.fail('per-parent settlement did not converge')

    async def row(self,peer):
        async with peer.database.connect() as conn:return dict((await conn.execute(select(s.units))).mappings().one())

    async def unchanged_originals(self):await self.base.target.originals()

    async def test_native_then_master_release_exact_scopes_and_reserved_bytes_only_once(self):
        before=await self.base.target.domain()
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decryption')),
                patch.object(media_crypto,'open_premaster',side_effect=AssertionError('premaster opened'))):
            result=await self.finish()
        self.assertFalse(result['task_completed']);self.assertEqual(result['phase'],'cancelled')
        after=await self.base.target.domain();amount=(await self.row(self.f.master))['total_bytes']
        for name,peer in (('master',self.f.master),('source',self.f.source),('target',self.f.target)):
            self.assertEqual(after[name][fs.identity.name],before[name][fs.identity.name])
            if name!='target':
                for table in (fs.global_media,cs.encryption):
                    self.assertEqual(after[name][table.name],before[name][table.name])
            self.assertEqual(after[name][s.drains.name],before[name][s.drains.name])
            self.assertEqual(after[name][s.scopes.name],[])
            old={r['member_id']:r for r in before[name][fs.storage_members.name]}
            for row in after[name][fs.storage_members.name]:
                expected=old[row['member_id']]
                if name in {'master','target'} and row['member_id']==self.f.target.store.node['node_id']:
                    expected={**expected,'reserved_bytes':expected['reserved_bytes']-amount,'updated_at':int(self.f.now)}
                self.assertEqual(row,expected)
            async with peer.database.connect() as conn:
                if name in {'master','target'}:self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes)),0)
                if name!='master':relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
            if name!='master':
                status=await peer.transfers.native.status(peer.store.node,relation,uuid.uuid4().hex,self.lease.task_id)
                self.assertEqual((status['committed_bytes'],status['cancelled_bytes'],status['capacity_reserved']),(0,amount,0))
        async with self.f.master.database.connect() as conn:
            task=dict((await conn.execute(select(s.tasks))).mappings().one())
            self.assertEqual((task['state'],task['committed_bytes']),('cancelling',0))
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.objects).where(s.objects.c.state=='cancelled')),2)
        async with self.f.master.database.begin() as conn:
            committed,remaining,_=await self.service.cutover._accounting(conn,task)
            self.assertEqual((committed,remaining),(0,0))
        self.assertEqual((await self.step())['receipt'],result['receipt'])
        self.assertEqual(await self.base.target.domain(),after);await self.unchanged_originals()


    async def test_unrelated_target_reservations_and_scopes_survive_all_three_settlements(self):
        foreign=uuid.uuid4().hex;scopes={}
        for name,peer in (('master',self.f.master),('target',self.f.target)):
            scope=dict(scope_id='9'*64,task_id=foreign,member_id=self.f.target.store.node['node_id'],
                resource_kind='media',parent_path='music/unrelated',created_at=int(self.f.now));scopes[name]=scope
            async with peer.database.begin() as conn:
                await conn.execute(insert(s.scopes).values(**scope))
                await conn.execute(insert(s.capacity).values(task_id=foreign,member_id=self.f.target.store.node['node_id'],reserved_bytes=137,updated_at=int(self.f.now)))
                await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==self.f.target.store.node['node_id'])
                    .values(reserved_bytes=fs.storage_members.c.reserved_bytes+137))
        await self.finish()
        for name,peer in (('master',self.f.master),('target',self.f.target)):
            async with peer.database.connect() as conn:
                self.assertEqual(list((await conn.execute(select(s.scopes))).mappings()),[scopes[name]])
                self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes).where(s.capacity.c.task_id==foreign)),137)
                self.assertEqual(await conn.scalar(select(fs.storage_members.c.reserved_bytes).where(fs.storage_members.c.member_id==self.f.target.store.node['node_id'])),137)
        await self.unchanged_originals()

    async def test_traffic_peak_does_not_release_source_scope_until_fresh_quiet_verification(self):
        self.f.traffic.record(egress=100*1024**2);self.assertFalse(self.f.traffic.snapshot()['can_run'])
        await self.step();before=await self.base.target.domain();self.assertEqual((await self.step())['phase'],'source_waiting_low_traffic')
        self.assertEqual(await self.base.target.domain(),before)
        self.f.traffic._clock=lambda:1100;self.f.traffic._quiet_since=1000
        self.assertTrue((await self.finish())['unit_cancelled']);await self.unchanged_originals()

    async def test_source_changed_after_retention_blocks_fresh_settlement_without_deleting_it(self):
        _,path,_=self.base.target.fixture.fixture.fixture.resources[0]
        path.write_bytes(b'preserve changed original after preliminary retention')
        before=await self.base.target.domain()
        with self.assertRaisesRegex(TransferError,'source_changed'):await self.finish()
        self.assertEqual(path.read_bytes(),b'preserve changed original after preliminary retention')
        self.assertEqual(await self.base.target.domain(),before)
        self.assertNotIn('cancel_settled',(await self.row(self.f.source))['publication'])

    async def test_null_native_or_master_settlement_journal_refuses_without_releasing_any_scope(self):
        before=await self.base.target.domain();grant=await self.base.target.grant()
        for peer in (self.f.source,self.f.target):
            current=await self.row(peer)
            async with peer.database.begin() as conn:await conn.execute(update(s.units).values(publication={**current['publication'],'cancel_settled':None}))
            async with peer.database.connect() as conn:relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
            with self.assertRaisesRegex(TransferError,'settlement_journal_changed'):
                await UnitSettlement(peer.transfers).advance(peer.store.node,relation,uuid.uuid4().hex,grant,self.unit,None,None,None,None)
            self.assertEqual(await self.base.target.domain(),before)
            async with peer.database.begin() as conn:await conn.execute(update(s.units).values(publication=current['publication']))
        current=await self.row(self.f.master)
        async with self.f.master.database.begin() as conn:await conn.execute(update(s.units).values(publication={**current['publication'],'cancel_unit_work':None}))
        with self.assertRaisesRegex(TransferError,'master_journal_changed'):await self.step()
        self.assertEqual(await self.base.target.domain(),before)
        async with self.f.master.database.begin() as conn:await conn.execute(update(s.units).values(publication=current['publication']))
        self.assertTrue((await self.finish())['unit_cancelled']);await self.unchanged_originals()

    async def test_target_requires_actual_source_settlement_not_just_retention(self):
        authorization,_,_=await self.service.authority.issue(self.lease,self.unit)
        grant=await self.base.target.grant();before=await self.base.target.domain()
        async with self.f.target.database.connect() as conn:relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
        with self.assertRaisesRegex(TransferError,'settlement_receipt_invalid'):
            await UnitSettlement(self.f.target.transfers).advance(self.f.target.store.node,relation,uuid.uuid4().hex,grant,
                self.unit,authorization,self.source_receipt,self.target_receipt,self.source_receipt)
        self.assertEqual(await self.base.target.domain(),before);await self.unchanged_originals()

    async def test_lost_source_or_target_settlement_reply_recovers_without_double_refund(self):
        original=self.f.transport.request;lost=set()
        async def lose(origin,path,**kwargs):
            result=await original(origin,path,**kwargs)
            if path.endswith('/cancel-unit-settle') and result['payload']['phase']=='settled' and origin not in lost:
                lost.add(origin);raise httpx.ReadTimeout('Controlled reply loss after native commit')
            return result
        self.f.transport.request=lose
        for _ in range(50):
            try:result=await self.step()
            except (httpx.ReadTimeout,TransferError):
                self.service=self.new_service();continue
            if result['unit_cancelled']:break
        else:self.fail('reply loss recovery did not converge')
        self.assertEqual(len(lost),2)
        before=await self.base.target.domain();receipt=result['receipt']
        self.service=self.new_service();self.assertEqual((await self.finish())['receipt'],receipt)
        self.assertEqual(await self.base.target.domain(),before);await self.unchanged_originals()

    async def test_lost_master_final_reply_recovers_live_revision_but_never_renews_expired_runner(self):
        original=self.service._finish
        async def lose(*args,**kwargs):
            await original(*args,**kwargs)
            raise httpx.ReadTimeout('Controlled lost final Master reply after atomic commit')
        with patch.object(self.service,'_finish',side_effect=lose):
            with self.assertRaises(httpx.ReadTimeout):await self.finish()
        old=self.lease;self.service=self.new_service();await self.finish()
        self.assertEqual((self.lease.runner_id,self.lease.epoch,self.lease.revision),(old.runner_id,old.epoch,old.revision+1))
        async with self.f.master.database.begin() as conn:await self.service.t.journal._fenced(conn,self.lease)
        self.f.now+=100;stable=self.lease;await self.finish();self.assertEqual(self.lease,stable)
        async with self.f.master.database.begin() as conn:
            from app.services.resource_migration_journal import Conflict
            with self.assertRaises(Conflict):await self.service.t.journal._fenced(conn,self.lease)
        await self.unchanged_originals()

    async def test_native_target_audit_failure_rolls_back_quota_scope_and_marker(self):
        log=self.f.target.store.log
        async def fail(conn,action,*args,**kwargs):
            if action=='migration_native_unit_cancel_settled':raise RuntimeError('Controlled native audit rollback')
            return await log(conn,action,*args,**kwargs)
        async with self.f.target.database.connect() as conn:reserved=await conn.scalar(select(s.capacity.c.reserved_bytes))
        with patch.object(self.f.target.store,'log',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'native audit rollback'):await self.finish()
        unit=await self.row(self.f.target);self.assertEqual(unit['native_acknowledged'],0);self.assertNotIn('cancel_settled',unit['publication'])
        async with self.f.target.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes)),reserved)
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),1)
        self.assertTrue((await self.finish())['unit_cancelled']);await self.unchanged_originals()

    async def test_master_audit_failure_rolls_back_only_master_then_replays_native_receipts(self):
        before=await self.base.target.domain();log=self.f.master.store.log
        async def fail(conn,action,*args,**kwargs):
            if action=='migration_master_unit_cancelled':raise RuntimeError('Controlled Master audit rollback')
            return await log(conn,action,*args,**kwargs)
        with patch.object(self.f.master.store,'log',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'Master audit rollback'):await self.finish()
        self.assertEqual((await self.base.target.domain())['master'],before['master'])
        self.assertNotEqual((await self.row(self.f.master))['state'],'cancelled')
        native=[(await self.row(peer))['publication']['cancel_settled']['receipt'] for peer in (self.f.source,self.f.target)]
        self.assertTrue((await self.finish())['unit_cancelled'])
        self.assertEqual([(await self.row(peer))['publication']['cancel_settled']['receipt'] for peer in (self.f.source,self.f.target)],native)
        await self.unchanged_originals()

    async def test_changed_native_capacity_refuses_then_explicit_fixture_repair_retries(self):
        async with self.f.target.database.begin() as conn:
            original=await conn.scalar(select(s.capacity.c.reserved_bytes))
            await conn.execute(update(s.capacity).values(reserved_bytes=original-1))
        with self.assertRaisesRegex(ReservationError,'reservation_capacity_inconsistent'):await self.finish()
        async with self.f.target.database.begin() as conn:await conn.execute(update(s.capacity).values(reserved_bytes=original))
        self.assertTrue((await self.finish())['unit_cancelled']);await self.unchanged_originals()

    async def test_changed_master_capacity_refuses_after_native_settlement_without_releasing_master(self):
        for _ in range(30):
            await self.step()
            work=(await self.row(self.f.master))['publication'].get('cancel_unit_work',{})
            if set(work.get('settlements',{}))=={'source','target'}:break
        else:self.fail('both native receipts did not converge')
        async with self.f.master.database.begin() as conn:
            original=await conn.scalar(select(s.capacity.c.reserved_bytes));await conn.execute(update(s.capacity).values(reserved_bytes=original-1))
        with self.assertRaisesRegex(TransferError,'cancel_capacity_inconsistent'):await self.finish()
        async with self.f.master.database.begin() as conn:
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),2)
            await conn.execute(update(s.capacity).values(reserved_bytes=original))
        self.assertTrue((await self.finish())['unit_cancelled']);await self.unchanged_originals()

    async def test_recreated_target_parent_is_preserved_and_blocks_first_settlement(self):
        _,path,_=self.base.target.fixture.fixture.fixture.resources[0]
        parent=self.f.target.roots['media']/path.relative_to(self.f.source.roots['media']).parent
        parent.mkdir(parents=True,exist_ok=True);unknown=parent/'later-user-file';unknown.write_bytes(b'preserve new target bytes')
        with self.assertRaises(TransferError):await self.finish()
        self.assertEqual(unknown.read_bytes(),b'preserve new target bytes')
        kept=self.f.root/'preserved-recreated-target-parent';parent.rename(kept)
        self.assertTrue((await self.finish())['unit_cancelled']);self.assertEqual((kept/unknown.name).read_bytes(),b'preserve new target bytes')
        await self.unchanged_originals()

    async def test_typed_signed_receipt_mutations_are_rejected(self):
        await self.finish();grant=await self.base.target.grant();task=grant['payload']
        master_unit=await self.row(self.f.master)
        chosen=self.service._selected({'target_relationship':task['target_relationship']},master_unit)
        for side,peer in (('source',self.f.source),('target',self.f.target)):
            record=(await self.row(peer))['publication']['cancel_settled'];private=peer.store.unseal(peer.store.node['private_key'])
            for change in ({'version':True},{'epoch':True},{'capacity_refunded':True},{'scope_released':1},{'task_completed':True},
                    {'unit_sha256':'0'*64},{'used_after':record['receipt']['payload']['used_before']+1}):
                forged=p.sign(private,{**record['receipt']['payload'],**change})
                with self.subTest(side=side,change=change),self.assertRaises(TransferError):
                    verify(forged,task,side,chosen,self.f.now)

    async def test_native_restart_and_new_epoch_replay_preserve_later_business_data(self):
        result=await self.finish();self.f.now+=100
        for peer in (self.f.source,self.f.target):
            await peer.transfers.close()
            peer.transfers=TransferPeers(peer.store,root_provider=lambda peer=peer:peer.roots,traffic=self.f.traffic,wall=lambda:self.f.now)
            self.extra.append(peer.transfers)
        async with self.f.master.database.begin() as conn:await conn.execute(update(fs.relationships).values(last_heartbeat=int(self.f.now)))
        self.lease=await self.service.t.journal.claim(self.lease.task_id,uuid.uuid4().hex,ttl=60);self.base.target.lease=self.lease
        _,original,_=self.base.target.fixture.fixture.fixture.resources[0]
        path=self.f.target.roots['media']/original.relative_to(self.f.source.roots['media'])
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'legitimate later target content')
        before=await self.base.target.domain();grant=await self.base.target.grant()
        for peer in (self.f.source,self.f.target):
            async with peer.database.connect() as conn:relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
            reply=await UnitSettlement(peer.transfers).advance(peer.store.node,relation,uuid.uuid4().hex,grant,self.unit,None,None,None,None)
            self.assertEqual(reply['phase'],'settled')
        self.assertEqual((await self.finish())['receipt'],result['receipt'])
        self.assertEqual(await self.base.target.domain(),before);self.assertEqual(path.read_bytes(),b'legitimate later target content')
        await self.unchanged_originals()

    async def test_restored_scope_or_missing_marker_cannot_replay_as_settled(self):
        async with self.f.target.database.connect() as conn:scope=dict((await conn.execute(select(s.scopes))).mappings().one())
        await self.finish();grant=await self.base.target.grant();peer=self.f.target
        async with peer.database.connect() as conn:relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
        async with peer.database.begin() as conn:await conn.execute(insert(s.scopes).values(**scope))
        with self.assertRaisesRegex(TransferError,'scope'):
            await UnitSettlement(peer.transfers).advance(peer.store.node,relation,uuid.uuid4().hex,grant,self.unit,None,None,None,None)
        async with peer.database.connect() as conn:self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),1)
        current=await self.row(peer);publication={k:v for k,v in current['publication'].items() if k!='cancel_settled'}
        async with peer.database.begin() as conn:
            await conn.execute(delete(s.scopes).where(s.scopes.c.scope_id==scope['scope_id']))
            await conn.execute(update(s.units).values(publication=publication))
        with self.assertRaisesRegex(TransferError,'settlement_journal_changed'):
            await UnitSettlement(peer.transfers).advance(peer.store.node,relation,uuid.uuid4().hex,grant,self.unit,None,None,None,None)
        await self.unchanged_originals()

    async def test_later_target_read_is_not_blocked_by_historical_cancel_intent(self):
        await self.finish();peer=self.f.target
        _,original,_=self.base.target.fixture.fixture.fixture.resources[0]
        relative=original.relative_to(self.f.source.roots['media']).as_posix();path=peer.roots['media']/relative
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'later independently owned object')
        object_id='7'*64
        async with peer.database.begin() as conn:
            await conn.execute(text("INSERT INTO media_objects(media_id,media_path,object_kind,path_locator,created_at,updated_at) "
                "VALUES (:id,:path,:kind,:locator,'2026-10-21 00:00:00','2026-10-21 00:00:00')"),
                dict(id=object_id,path=relative,kind='audio',locator=hashlib.sha256(relative.encode()).hexdigest()))
        async with peer.database.connect() as conn:relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
        lease=await resource_readers._begin(peer.store,relation,'8'*64,object_id,'media',relative,peer.roots['media'],int(time.time())+p.TOKEN_SECONDS)
        self.assertEqual(len(lease),32);self.assertEqual(path.read_bytes(),b'later independently owned object')

    async def test_actual_asgi_hmac_settlement_and_nonce_replay(self):
        from fastapi import FastAPI
        from app.api import internal_nodes,internal_resource_inventory,internal_resource_transfer as api
        application=FastAPI();application.include_router(api.router)
        authorization,_,_=await self.service.authority.issue(self.lease,self.unit)
        source_settlement=None
        for peer in (self.f.source,self.f.target):
            key=p.public_key(peer.store.unseal(peer.store.node['private_key']))
            with (patch.object(internal_nodes,'state',peer.store),patch.object(internal_resource_inventory,'state',peer.store),patch.object(api,'transfers',peer.transfers)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url=peer.store.node['endpoint']) as client:
                    for index in range(24):
                        path='/internal/v1/migration/transfer/cancel-unit-settle'
                        value=dict(request_id=uuid.uuid4().hex,grant=await self.base.target.grant(),unit_id=self.unit,
                            authorization=authorization,source_receipt=self.source_receipt,target_receipt=self.target_receipt,
                            source_settlement=source_settlement)
                        raw=p.canonical(value);headers=p.auth_headers(peer.credential,peer.relationship,'POST',path,raw)
                        response=await client.post(path,content=raw,headers=headers);self.assertEqual(response.status_code,200,response.text)
                        result=p.verify(key,response.json());self.assertEqual(result['request_id'],value['request_id'])
                        if index==0:self.assertEqual((await client.post(path,content=raw,headers=headers)).status_code,401)
                        if result['phase']=='settled':
                            if peer is self.f.source:source_settlement=result['receipt']
                            break
                    else:self.fail('authenticated native settlement did not converge')
        self.assertTrue((await self.finish())['unit_cancelled']);await self.unchanged_originals()



@unittest.skipUnless(os.name=='posix','recording settlement requires opaque native Linux files')
class UnitSettlementRecordingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_settlement_keeps_all_three_opaque_recording_footers_and_business_facts(self):
        from app.services import karaoke_schema as ks
        from app.services.resource_cancel_source_peers import SourceCancellation
        target=fixtures.fixtures.CancellationTargetRecordingsTests('runTest');await target.asyncSetUp()
        try:
            target_receipt=(await target.finish())['receipt'];f=target.f
            async with f.master.database.connect() as conn:before=[dict(row) for row in (await conn.execute(select(ks.recordings))).mappings()]
            async with f.source.database.connect() as conn:relation=dict((await conn.execute(select(fs.relationships))).mappings().one())
            authorization,raw,_=await target.source.authority.issue(target.lease,target.unit)
            source=SourceCancellation(f.source.transfers);grant=p.sign(f.master.store.unseal(f.master.store.node['private_key']),raw)
            await source.stop(f.source.store.node,relation,uuid.uuid4().hex,grant)
            with (patch.object(media_crypto,'open_premaster',side_effect=AssertionError('recording premaster')),
                    patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('recording decryption'))):
                for _ in range(24):
                    result=await source.advance(f.source.store.node,relation,uuid.uuid4().hex,grant,target.unit,authorization,target_receipt)
                    if result['phase']=='retained':source_receipt=result['receipt'];break
                else:self.fail('recording Source retention did not converge')
                native_transport(f,self.assertEqual)
                service=UnitCancellation(f.master.store,f.transport,traffic=f.traffic,wall=lambda:f.now);lease=target.lease
                for _ in range(40):
                    lease,result=await service.advance(lease,target.unit,source_receipt,target_receipt)
                    if result['unit_cancelled']:break
                else:self.fail('opaque recording unit settlement did not converge')
            async with f.master.database.connect() as conn:
                self.assertEqual([dict(row) for row in (await conn.execute(select(ks.recordings))).mappings()],before)
                self.assertEqual(await conn.scalar(select(s.capacity.c.reserved_bytes)),0)
                self.assertEqual(await conn.scalar(select(func.count()).select_from(s.scopes)),0)
                self.assertEqual(await conn.scalar(select(s.tasks.c.state)),'cancelling')
            for _,path,data in target.base.resources:self.assertEqual(path.read_bytes(),data)
            self.assertEqual(result['receipt']['payload']['object_count'],3)
        finally:await target.asyncTearDown()
