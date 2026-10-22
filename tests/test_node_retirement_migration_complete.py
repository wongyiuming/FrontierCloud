"""Actual migration/cleanup histories followed by independent native retirement."""
import copy
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from sqlalchemy import select,update
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.core.sqlite_compat import create_sqlite_engine
from app.services import business_traffic,media_crypto,resource_migration_schema as s,runtime_gate
from app.services.federation import protocol as p,schema as fs
from app.services.node_retirement_complete import Completion
from app.services.node_retirement_files import Authority
from app.services.node_retirement_fences import RetirementError
from app.services.node_retirement_runtime import Runtime
from app.services.node_retirement_state import Journal
from tests import test_resource_finalization as finalization,test_resource_migration_planning as planning


def selected_database(url):
    value=make_url(url);selected=Path(value.database).parent/'data/selected.db';selected.parent.mkdir(parents=True,exist_ok=True)
    return create_sqlite_engine(value.set(database=str(selected)))


@unittest.skipUnless(os.name=='posix','native migration/retirement requires Linux')
class MigratedSourceRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mode=patch.object(settings,'DEPLOYMENT_MODE','only_stroge');self.mode.start()
        self.fixture=finalization.FinalizationTests('runTest')
        with patch.object(planning,'create_sqlite_engine',side_effect=selected_database):await self.fixture.asyncSetUp()
        self.f=self.fixture.f;self.native=None;self.runtime=None
        for peer in (self.f.source,self.f.target):
            peer.reservations.wall=lambda:self.f.now;peer.observer.wall=lambda:self.f.now
            # The older transfer fixtures use 32-byte test credentials. Set the
            # actual 48-byte pairing contract before any plan binds credentials.
            credential=p.encode(os.urandom(48));peer.credential=credential
            for local in (self.f.master,peer):
                async with local.database.begin() as conn:
                    await conn.execute(update(fs.relationships).where(fs.relationships.c.relationship_id==peer.relationship).values(credential=local.store.seal(credential)))
        await self.fixture.prepare();await self.fixture.finish()
        source=self.f.source;root=source.roots['data']
        # This synthetic fixture note is setup metadata, outside the native
        # deployment. Do not add an exception for it to the product census.
        (root/'identity-note').rename(root.parent/'fixture-identity-note')
        secrets=root.parent/'secrets';secrets.mkdir(exist_ok=True)
        vault=secrets/'node-vault.key';vault.write_bytes(source.vault_key);vault.chmod(0o600)
        self.native=runtime_gate.acquire(root);self.runtime=Runtime(root,self.native)
        self.authority=await Authority.capture(self.runtime,source.store,paths=source.roots,secrets=secrets,upload_leases=root/'upload-leases')
        self.runtime.authority=self.authority;self.relationship=source.relationship
        self.master_journal=Journal(self.f.master.store,wall=lambda:self.f.now)
        self.peer_journal=Journal(source.store,gate=self.runtime.gate,authority=self.authority,wall=lambda:self.f.now)
        self.traffic=business_traffic.Monitor()
        self.peer_complete=Completion(self.peer_journal,runtime=self.runtime,traffic=self.traffic)
        self.master_complete=Completion(self.master_journal)
        self.proposal=await self.master_journal.begin_master(self.relationship,source.store.node['node_id'],'migration-retirement-test')
        await self.peer_journal.begin_peer(self.relationship,self.proposal['binding'])

    async def asyncTearDown(self):
        if self.runtime is not None:self.runtime.close()
        if self.native is not None:self.native.close()
        await self.fixture.asyncTearDown();self.mode.stop()

    async def finish(self):
        with self.runtime.gate.lease(exclusive=True) as lease:
            peer=await self.peer_complete.peer(self.relationship,self.proposal['binding'],lease)
        master=await self.master_complete.master(self.relationship,self.f.source.store.node['node_id'],peer['receipt'],'migration-retirement-test')
        return peer,master

    async def test_completed_migration_remains_valid_history_during_independent_retirement(self):
        before={}
        for peer in (self.f.source,self.f.target,self.f.master):
            async with peer.database.connect() as conn:
                before[peer.name]={table.name:[dict(row) for row in (await conn.execute(select(table))).mappings()] for table in s.metadata.tables.values()}
        with (patch.object(media_crypto.Manager,'file_key',side_effect=AssertionError('decrypted media')),
                patch.object(media_crypto,'open_premaster',side_effect=AssertionError('opened premaster'))):
            peer,master=await self.finish()
        self.assertEqual(peer['receipt']['payload']['history'],dict(native_tasks=1))
        self.assertEqual(master['receipt']['payload']['history'],dict(master_tasks=1))
        for node in (self.f.source,self.f.target,self.f.master):
            async with node.database.connect() as conn:
                self.assertEqual(before[node.name],{table.name:[dict(row) for row in (await conn.execute(select(table))).mappings()] for table in s.metadata.tables.values()})
        await self.fixture.fixture.target_preserved()

    async def test_empty_filesystem_cannot_hide_a_forged_migration_final_receipt(self):
        async with self.f.source.database.begin() as conn:
            raw=await conn.scalar(select(s.peer_reservations.c.payload));changed=copy.deepcopy(raw)
            changed['final_receipt']['payload']['total_bytes']+=1
            await conn.execute(update(s.peer_reservations).values(payload=changed))
        with self.assertRaisesRegex(RetirementError,'retirement_migration_receipt_invalid'):await self.finish()
        async with self.f.source.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.peer_reservations.c.payload)),changed)
            self.assertEqual(await conn.scalar(select(fs.relationships.c.state)),'retiring')
        await self.fixture.fixture.target_preserved()
