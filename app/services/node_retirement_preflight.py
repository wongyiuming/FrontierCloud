"""Readonly business observations; neither a reservation nor permission to retire."""
import asyncio
import uuid

import httpx

from app.core.joined_io import joined_io
from app.services import karaoke_storage,media_manager
from app.services.federation import protocol as p
from app.services.node_retirement_complete import Completion
from app.services.node_retirement_fences import RetirementError
from app.services.node_retirement_history import NativeHistory
from app.services.node_retirement_master_history import MasterHistory
from app.services.node_retirement_state import _authenticated,binding,ledger_clear
from app.services.resource_observation import digest


class Preflight:
    def __init__(self,journal,*,runtime=None,traffic=None):
        self.journal,self.store=journal,journal.store
        self.completion=Completion(journal,runtime=runtime,traffic=traffic)

    async def _active(self,conn,relationship_id,*,authenticated_relation=None):
        node,relation=await self.journal._context(conn,relationship_id)
        _authenticated(relation,authenticated_relation)
        if await self.journal._existing(conn,node,relation) is not None:
            raise RetirementError('retirement_already_started')
        if relation['state']!='active':raise RetirementError('retirement_relationship_not_active')
        return node,relation

    async def peer(self,relationship_id,value,lease,*,authenticated_relation=None):
        _,authority=self.completion._native(lease)
        async with media_manager.media_mutation_lock,karaoke_storage._write_lock,self.store.database.begin() as conn:
            node,relation=await self._active(conn,relationship_id,authenticated_relation=authenticated_relation)
            if node['role']!='Follower' or relation['direction']!='upstream':raise RetirementError('retirement_role_invalid')
            expected,_=binding(node,relation,self.store,value['retirement_id'])
            if p.canonical(expected)!=p.canonical(value):raise RetirementError('retirement_operation_conflict')
            await authority.verify_database(conn)
            await self.completion._fresh_readers(lease)
            await ledger_clear(conn,node,relation)
            history=await NativeHistory(self.store,self.journal.wall()).verify(conn,node)
            census=await authority.census(conn,lease)
            try:
                while not await joined_io(census.page):await asyncio.sleep(0)
                inspection=census.summary()
                await ledger_clear(conn,node,relation)
                if await NativeHistory(self.store,self.journal.wall()).verify(conn,node)!=history:
                    raise RetirementError('retirement_ledger_changed')
                self.completion._native(lease);self.completion._readers()
                inspection=census.summary()
                return dict(observed_at=int(self.journal.wall()),history=history,inspection=inspection,
                    roots_sha256=digest(self.journal._roots()))
            finally:census.close()

    async def master(self,relationship_id,transport):
        # The ephemeral binding authenticates this observation only. No journal,
        # desired configuration, quota, ownership or filesystem state is changed.
        async with media_manager.media_mutation_lock,karaoke_storage._write_lock,self.store.database.begin() as conn:
            node,relation=await self._active(conn,relationship_id)
            if node['role']!='Master' or relation['direction']!='downstream':raise RetirementError('retirement_role_invalid')
            value,_=binding(node,relation,self.store,uuid.uuid4().hex)
            try:
                await ledger_clear(conn,node,relation)
                history=await MasterHistory(self.store,self.journal.wall()).verify(conn,node,relation['peer_id'])
                master=dict(state='passed',code='',history=history)
            except RetirementError as exc:master=dict(state='blocked',code=exc.code)
        from app.services.node_retirement_client import Client
        try:
            result=await Client(self.store,relation,transport,wall=self.journal.wall).call('check',value)
            peer=dict(state='passed',code='',observation=result)
        except RetirementError as exc:
            peer=dict(state='blocked',code=exc.code)
        except (httpx.HTTPError,OSError,TimeoutError):
            peer=dict(state='unverified',code='retirement_peer_unavailable')
        return dict(version=1,preflight_only=True,relationship_id=relationship_id,member_id=value['member_id'],
            observed_at=int(self.journal.wall()),ready=master['state']==peer['state']=='passed',master=master,peer=peer)
