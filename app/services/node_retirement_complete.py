"""Fresh terminal retirement under native request, writer, SQL and file barriers.

No HTTP or Admin execution is exported here. A complete peer receipt can only
be produced by the actual pinned native runtime. No resource is removed.
"""
import asyncio
import time

from sqlalchemy import update

from app.core.config import settings
from app.core.joined_io import joined_io
from app.services import business_traffic,karaoke_storage,media_manager,node_retirement_schema as s,resource_fences
from app.services.federation import protocol as p,schema as fs
from app.services.node_retirement_fences import RetirementError
from app.services.node_retirement_files import Authority
from app.services.node_retirement_history import NativeHistory
from app.services.node_retirement_master_history import MasterHistory
from app.services import node_retirement_receipts as receipts
from app.services.node_retirement_runtime import Runtime,current
from app.services.node_retirement_state import _authenticated,_logical_clear
from app.services.resource_observation import digest


class Completion:
    def __init__(self,journal,*,runtime=None,traffic=None):
        self.journal,self.store=journal,journal.store
        self.runtime=runtime
        self.traffic=business_traffic.monitor if traffic is None else traffic

    def _native(self,lease):
        runtime=current() if self.runtime is None else self.runtime
        if type(runtime) is not Runtime:raise RetirementError('retirement_native_runtime_required')
        runtime.check();authority=runtime.authority
        if (type(authority) is not Authority or authority.runtime is not runtime or authority.store is not self.store or
                self.journal.authority is not authority or self.journal.gate is not runtime.gate):
            raise RetirementError('retirement_native_authority_required')
        runtime.gate.require(lease,exclusive=True);authority.check()
        return runtime,authority

    def _readers(self):
        if type(self.traffic) is not business_traffic.Monitor:
            raise RetirementError('retirement_reader_observation_unavailable')
        if settings.DEPLOYMENT_MODE!='only_stroge' and not self.traffic._required:
            raise RetirementError('retirement_reader_observation_unavailable')
        value=self.traffic.snapshot()
        if value.get('readers_clear') is not True:
            code='retirement_readers_active' if type(value.get('active_business_requests')) is int and value['active_business_requests']>0 else 'retirement_reader_observation_unavailable'
            raise RetirementError(code)

    async def _fresh_readers(self,lease):
        self._readers()
        if not self.traffic._required:return
        after=self.traffic._clock();wall=int(self.traffic._wall()*1000);deadline=time.monotonic()+3
        while True:
            self._native(lease);self._readers();sample=self.traffic._gateway
            if (self.traffic._gateway_at is not None and self.traffic._gateway_at>after and
                    sample is not None and sample.observed_at_ms>=wall):return
            if time.monotonic()>=deadline:raise RetirementError('retirement_reader_observation_not_fresh')
            await asyncio.sleep(.05)

    async def _ledger(self,conn,node,relation):
        member=relation['peer_id'] if node['role']=='Master' else node['node_id']
        # The exact existing retirement fence has already been authenticated by
        # Journal._existing. Do not weaken ordinary member_clear with a flag.
        try:await resource_fences._migration_member_clear(conn,member,readers=True)
        except resource_fences.FenceConflict as exc:raise RetirementError(exc.code) from None
        await _logical_clear(conn,node,relation)
        if node['role']=='Follower':return await NativeHistory(self.store,self.journal.wall()).verify(conn,node)
        return await MasterHistory(self.store,self.journal.wall()).verify(conn,node,member)

    async def _write(self,conn,node,relation,row,payload,roots,actor):
        envelope=p.sign(self.store.unseal(node['private_key']),payload)
        recorded={**row,'phase':'completed','completed_at':payload['completed_at'],'receipt':envelope,'roots':roots}
        receipts.recorded(self.store,node,relation,recorded,self.journal.wall())
        summary={**relation['summary'],'revocation_acknowledged':True,'retirement_id':row['retirement_id'],
            'retirement_receipt_sha256':digest(envelope)}
        if len(p.canonical(summary))>65536:raise RetirementError('retirement_relationship_metadata_limit')
        changed=await conn.execute(update(s.retirements).where(s.retirements.c.relationship_id==row['relationship_id'],
            s.retirements.c.phase=='retiring',s.retirements.c.revision==row['revision']).values(
                phase='completed',revision=row['revision']+1,completed_at=payload['completed_at'],
                roots_json=p.canonical(roots).decode(),receipt_json=p.canonical(envelope).decode()))
        if changed.rowcount!=1:raise RetirementError('retirement_journal_changed')
        await conn.execute(update(fs.relationships).where(fs.relationships.c.relationship_id==row['relationship_id']).values(
            state='revoked',status='offline',summary=summary))
        await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==row['member_id']).values(
            storage_enabled=0,writable=0,health='offline',updated_at=payload['completed_at']))
        await self.store.log(conn,'retirement-completed',actor,row['relationship_id'],retirement_id=row['retirement_id'],
            member_id=row['member_id'],receipt_sha256=digest(envelope))
        return await self.journal._existing(conn,node,{**relation,'state':'revoked','summary':summary})

    def _payload(self,row,side,history):
        now=int(self.journal.wall())
        if now<row['started_at'] or now<=0:raise RetirementError('retirement_clock_invalid')
        return dict(format=receipts.FORMAT,version=1,side=side,node_id=row['node_id'],
            **{name:row[name] for name in ('retirement_id','relationship_id','master_id','member_id','binding_sha256','started_at')},
            completed_at=now,history=history)

    async def peer(self,relationship_id,value,lease,*,authenticated_relation=None):
        runtime,authority=self._native(lease)
        if not isinstance(value,dict) or len(p.canonical(value))>8192:raise RetirementError('retirement_binding_invalid')
        # A fresh observation must follow exclusive request admission. Replays
        # do not re-inspect current files or reinterpret later external writes.
        async with media_manager.media_mutation_lock,karaoke_storage._write_lock,self.store.database.begin() as conn:
            node,relation=await self.journal._context(conn,relationship_id)
            _authenticated(relation,authenticated_relation)
            if node['role']!='Follower' or relation['direction']!='upstream':raise RetirementError('retirement_role_invalid')
            row=await self.journal._existing(conn,node,relation)
            if row is None:raise RetirementError('retirement_not_started')
            if value!=row['binding']:raise RetirementError('retirement_operation_conflict')
            if 'native' not in row['roots']:raise RetirementError('retirement_native_authority_required')
            if row['phase']=='completed':return row
            await self._fresh_readers(lease)
            history=await self._ledger(conn,node,relation)
            census=await authority.census(conn,lease)
            try:
                while not await joined_io(census.page):await asyncio.sleep(0)
                inspection=census.summary()
                if await self._ledger(conn,node,relation)!=history:raise RetirementError('retirement_ledger_changed')
                self._native(lease);self._readers();inspection=census.summary()
                payload=dict(**self._payload(row,'peer',history),roots_sha256=digest(row['roots']),inspection=inspection)
                result=await self._write(conn,node,relation,row,payload,row['roots'],row['master_id'])
                self._native(lease);self._readers()
            finally:census.close()
        return result

    async def master(self,relationship_id,confirm_member_id,peer_receipt,actor):
        async with media_manager.media_mutation_lock,karaoke_storage._write_lock,self.store.database.begin() as conn:
            node,relation=await self.journal._context(conn,relationship_id)
            if node['role']!='Master' or relation['direction']!='downstream':raise RetirementError('retirement_role_invalid')
            if confirm_member_id!=relation['peer_id']:raise RetirementError('retirement_confirmation_mismatch')
            row=await self.journal._existing(conn,node,relation)
            if row is None:raise RetirementError('retirement_not_started')
            if row['phase']=='completed':
                if peer_receipt is not None and row['receipt']['payload']['peer_receipt']!=peer_receipt:
                    raise RetirementError('retirement_peer_receipt_changed')
                return row
            observed=receipts.peer(peer_receipt,relation['peer_key'],row['binding'],self.journal.wall())
            if observed['started_at']<row['started_at']-p.AUTH_SKEW_SECONDS:raise RetirementError('retirement_peer_receipt_stale')
            history=await self._ledger(conn,node,relation)
            payload=dict(**self._payload(row,'master',history),peer_receipt=peer_receipt,peer_receipt_sha256=digest(peer_receipt))
            return await self._write(conn,node,relation,row,payload,dict(version=1,peer_roots_sha256=observed['roots_sha256']),actor)
