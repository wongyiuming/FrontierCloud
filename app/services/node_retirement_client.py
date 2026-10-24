"""Verified HTTPS retirement controls and a stable Master completion step."""
import time
import uuid

from app.services.federation import protocol as p
from app.services.node_retirement_fences import RetirementError
from app.services import node_retirement_protocol as protocol,node_retirement_receipts as receipts
from app.services.node_retirement_complete import Completion
from app.services.resource_control_client import signed_request


class Client:
    def __init__(self,store,relation,transport,*,wall=time.time):
        self.store,self.relation,self.transport,self.wall=store,relation,transport,wall

    async def call(self,action,binding):
        relation=self.relation
        if (self.store.node.get('role')!='Master' or relation.get('direction')!='downstream' or relation.get('protocol')!=p.PROTOCOL_VERSION or
                relation.get('peer_id')!=binding.get('member_id') or self.store.node['node_id']!=binding.get('master_id') or
                relation.get('relationship_id')!=binding.get('relationship_id') or action not in {'check','begin','status','complete'}):
            raise RetirementError('retirement_client_binding_invalid')
        value=dict(format=protocol.FORMAT,version=1,action=action,request_id=uuid.uuid4().hex,binding=binding)
        envelope=await signed_request(self.store,relation,self.transport,protocol.PATH,value,limit=protocol.LIMIT,
            error_type=RetirementError,prefix='retirement',return_envelope=True)
        if action=='check':return protocol.verify_preflight(value,envelope,relation['peer_key'],binding,self.wall())
        result=protocol.verify(value,envelope,relation['peer_key'],binding)
        if result['phase']=='completed':
            observed=receipts.peer(result['receipt'],relation['peer_key'],binding,self.wall())
            if (observed['roots_sha256']!=result['roots_sha256'] or observed['started_at']!=result['started_at'] or
                    observed['completed_at']!=result['completed_at']):raise RetirementError('retirement_signed_response_invalid')
        return result


class Coordinator:
    def __init__(self,journal,transport):self.journal,self.store,self.transport=journal,journal.store,transport

    async def advance(self,relationship_id,confirm_member_id,actor):
        # Preparation and final Master completion independently repeat the
        # confirmation and logical checks. A lost peer reply is queried first.
        row=await self.journal.read(relationship_id)
        if confirm_member_id!=row['member_id']:raise RetirementError('retirement_confirmation_mismatch')
        if row['side']!='master':raise RetirementError('retirement_role_invalid')
        completion=Completion(self.journal)
        if row['phase']=='completed':return await completion.master(relationship_id,confirm_member_id,None,actor)
        async with self.store.database.begin() as conn:_,relation=await self.journal._context(conn,relationship_id)
        client=Client(self.store,relation,self.transport,wall=self.journal.wall)
        try:peer=await client.call('status',row['binding'])
        except RetirementError as exc:
            if exc.code!='retirement_not_started':raise
            peer=await client.call('begin',row['binding'])
        if peer['phase']=='retiring':peer=await client.call('complete',row['binding'])
        if peer['phase']!='completed':raise RetirementError('retirement_peer_not_completed')
        return await completion.master(relationship_id,confirm_member_id,peer['receipt'],actor)
