"""Native retained-source release and read-only replay preserve business data."""
import os
import unittest
import uuid
from unittest.mock import patch

import httpx
from sqlalchemy import func,insert,select,update

from app.services import media_crypto,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_cancel_source_release import SourceRelease
from app.services.resource_cancel_source_receipts import verify
from app.services.resource_transfer_protocol import TransferError
from tests import test_resource_cancel_catalog as fixtures


@unittest.skipUnless(os.name=='posix','native release requires real Linux source census')
class SourceReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.CurrentCancellationCatalogTests.asyncSetUp(self)
        await self.finish();self.authority=await self.catalog.authorize(self.lease)
        self.release=SourceRelease(self.f.source.transfers)

    asyncTearDown=fixtures.CurrentCancellationCatalogTests.asyncTearDown
    new_catalog=fixtures.CurrentCancellationCatalogTests.new_catalog
    finish=fixtures.CurrentCancellationCatalogTests.finish
    step=fixtures.CurrentCancellationCatalogTests.step

    async def apply(self,*,authority=None,query=False,grant=None):
        body=authority or self.authority
        return await self.release.advance(self.f.source.store.node,self.relation,uuid.uuid4().hex,grant or body['grant'],
            None if query else body['agreement'],None if query else body['source_census'])

    async def retained(self):
        await self.case.retained()
        async with self.f.master.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.tasks.c.state)),'cancelling')
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.drains)),1)

    async def active(self):
        async with self.f.source.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.peer_reservations.c.state)),'active')
            self.assertEqual(await conn.scalar(select(fs.storage_members.c.used_bytes)),self.used)
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.drains)),1)

    async def test_signed_master_agreement_reconciles_actual_retained_bytes_without_deleting(self):
        async with self.f.source.database.connect() as conn:before=(await conn.execute(select(fs.identity))).mappings().one()
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted media')),
                patch('app.services.resource_observation.os.pread',side_effect=AssertionError('read media'))):
            result=await self.apply()
        self.assertEqual(result['phase'],'released');self.assertFalse(result['task_completed']);self.assertFalse(result['source_files_removed'])
        value=verify(result['receipt'],self.authority['grant']['payload'],(2,3,sum(len(data) for _,_,data in self.case.resources)),self.f.now,
            master_key=self.relation['peer_key'],agreement=self.authority['agreement'],source_census=self.authority['source_census'])
        actual=sum(len(data) for _,_,data in self.case.resources[1:])
        self.assertEqual((value['native_used_before'],value['native_used_after']),(self.used,actual))
        async with self.f.source.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(fs.storage_members.c.used_bytes)),actual)
            self.assertEqual(await conn.scalar(select(s.peer_reservations.c.state)),'released')
            for table in (s.scopes,s.capacity,s.drains):self.assertEqual(await conn.scalar(select(func.count()).select_from(table)),0)
            self.assertEqual(dict((await conn.execute(select(fs.identity))).mappings().one()),dict(before))
        await self.retained()

    async def test_query_cannot_release_active_source_without_an_agreement(self):
        self.assertEqual((await self.apply(query=True))['phase'],'pending');await self.active();await self.retained()

    async def test_changed_master_authority_is_refused_without_releasing_source(self):
        permission=self.authority['agreement']
        changed={**permission,'payload':{**permission['payload'],'retained_bytes':0}}
        with self.assertRaisesRegex(TransferError,'agreement_invalid'):await self.apply(authority={**self.authority,'agreement':changed})
        await self.active();await self.retained()

    async def test_missing_volatile_physical_scan_requires_new_census_before_release(self):
        self.f.source.transfers.source_cancel_census.close();self.f.source.transfers.source_cancel_census=None
        with self.assertRaisesRegex(TransferError,'current_census_required'):await self.apply()
        await self.active();await self.retained()

    async def test_audit_failure_rolls_back_usage_scopes_capacity_and_drain(self):
        with patch.object(self.f.source.store,'log',side_effect=RuntimeError('fixture audit unavailable')):
            with self.assertRaisesRegex(RuntimeError,'audit unavailable'):await self.apply()
        await self.active();await self.retained()
        self.assertEqual((await self.apply())['phase'],'released')

    async def test_authority_expiring_during_audit_rolls_back_every_release_change(self):
        original=self.f.source.store.log
        async def slow(*args,**kwargs):await original(*args,**kwargs);self.f.now+=46
        with patch.object(self.f.source.store,'log',side_effect=slow):
            with self.assertRaisesRegex(TransferError,'authority_expired'):await self.apply()
        await self.active();await self.retained()

    async def test_playback_and_administrator_drain_survive_own_task_capability_release(self):
        node_id=self.f.source.store.node['node_id'];now=int(self.f.now)
        async with self.f.source.database.begin() as conn:
            await conn.execute(insert(s.drains).values(member_id=node_id,owner_id='f'*32,reason='administrator',created_at=now))
            for lease_id,purpose,task_id in (('a'*32,'playback',''),('b'*32,'migration',self.lease.task_id)):
                await conn.execute(insert(s.read_leases).values(lease_id=lease_id,member_id=node_id,logical_id=self.case.resources[1][0],
                    object_id='c'*64,source_version='d'*64,expires_at=now+3600,task_id=task_id,purpose=purpose))
        await self.apply()
        async with self.f.source.database.connect() as conn:
            self.assertEqual(list((await conn.execute(select(s.drains.c.reason))).scalars()),['administrator'])
            self.assertEqual(list((await conn.execute(select(s.read_leases.c.lease_id))).scalars()),['a'*32])
        await self.retained()

    async def test_foreign_capacity_is_preserved_and_blocks_release(self):
        async with self.f.source.database.begin() as conn:
            await conn.execute(insert(s.capacity).values(task_id='f'*32,member_id=self.f.source.store.node['node_id'],reserved_bytes=0,updated_at=int(self.f.now)))
        with self.assertRaisesRegex(TransferError,'foreign_work'):await self.apply()
        async with self.f.source.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.capacity)),2)
        await self.active();await self.retained()

    async def test_lost_reply_new_epoch_and_later_usage_replay_only_original_receipt(self):
        first=await self.apply();path=self.f.source.roots['media']/'fixture-later-owner-data';path.write_bytes(b'Preserve later bytes')
        later_used=first['receipt']['payload']['native_used_after']+12345
        async with self.f.source.database.begin() as conn:await conn.execute(update(fs.storage_members).values(used_bytes=later_used))
        self.f.now+=100
        async with self.f.master.database.begin() as conn:await conn.execute(update(fs.relationships).values(last_heartbeat=int(self.f.now)))
        self.lease=await self.catalog.t.journal.claim(self.lease.task_id,uuid.uuid4().hex,ttl=60)
        async with self.f.master.database.begin() as conn:task=await self.catalog.t.journal._fenced(conn,self.lease)
        relations=await self.catalog.t.relations(task)
        grant=self.catalog.t._grant(task,relations,self.lease.epoch,self.lease.runner_id,'finalize')
        self.release=SourceRelease(self.f.source.transfers)
        with patch.object(self.f.source.store,'log',side_effect=AssertionError('Repeated release audit')):second=await self.apply(query=True,grant=grant)
        self.assertEqual(second['receipt'],first['receipt']);self.assertEqual(path.read_bytes(),b'Preserve later bytes')
        async with self.f.source.database.connect() as conn:self.assertEqual(await conn.scalar(select(fs.storage_members.c.used_bytes)),later_used)
        await self.retained()

    async def test_actual_asgi_hmac_route_refuses_nonce_replay_and_returns_signed_receipt(self):
        from fastapi import FastAPI
        from app.api import internal_nodes,internal_resource_inventory,internal_resource_transfer as api
        app=FastAPI();app.include_router(api.router);path='/internal/v1/migration/transfer/cancel-source-release'
        raw=p.canonical(dict(request_id=uuid.uuid4().hex,**self.authority))
        headers=p.auth_headers(self.f.source.credential,self.f.source.relationship,'POST',path,raw)
        with (patch.object(internal_nodes,'state',self.f.source.store),patch.object(internal_resource_inventory,'state',self.f.source.store),
                patch.object(api,'transfers',self.f.source.transfers)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=self.f.source.store.node['endpoint']) as client:
                response=await client.post(path,content=raw,headers=headers);self.assertEqual(response.status_code,200,response.text)
                result=p.verify(self.authority['grant']['payload']['source_key'],response.json());self.assertEqual(result['phase'],'released')
                self.assertEqual((await client.post(path,content=raw,headers=headers)).status_code,401)
        await self.retained()


if __name__=='__main__':unittest.main()
