"""Admin orchestration over the same durable, identity-bound retirement journal."""
from sqlalchemy import select

from app.services import node_retirement_schema as s
from app.services.federation import protocol as p
from app.services.node_retirement_client import Coordinator
from app.services.node_retirement_fences import RetirementError
from app.services.node_retirement_preflight import Preflight
from app.services.node_retirement_state import Journal
from app.services.resource_observation import digest


def view(row):
    return {**{name:row[name] for name in ('relationship_id','retirement_id','member_id','phase','revision','started_at','completed_at')},
        'receipt_sha256':digest(row['receipt']) if row['receipt'] else '', 'receipt':row['receipt'] or None}


class Admin:
    def __init__(self,store,transport):self.store,self.transport,self.journal=store,transport,Journal(store)

    async def status(self,relationship_id):
        async with self.store.database.begin() as conn:
            node,relation=await self.journal._context(conn,relationship_id)
            if node['role']!='Master' or relation['direction']!='downstream':raise RetirementError('retirement_role_invalid')
            row=await self.journal._existing(conn,node,relation)
            if row is not None:return view(row)
            if relation['state']!='active':raise RetirementError('retirement_relationship_not_active')
            return dict(relationship_id=relationship_id,member_id=relation['peer_id'],phase='not_started')

    async def history(self,after=''):
        if after and not p.IDENTIFIER.fullmatch(after):raise RetirementError('retirement_cursor_invalid')
        async with self.store.database.begin() as conn:
            node=await self.store.lock(conn)
            if node['role']!='Master':raise RetirementError('retirement_role_invalid')
            identifiers=list((await conn.execute(select(s.retirements.c.relationship_id).where(s.retirements.c.node_id==node['node_id'],
                s.retirements.c.side=='master',s.retirements.c.relationship_id>after).order_by(s.retirements.c.relationship_id).limit(21))).scalars())
            items=[]
            for identifier in identifiers[:20]:
                _,relation=await self.journal._context(conn,identifier)
                row=await self.journal._existing(conn,node,relation)
                if row is None:raise RetirementError('retirement_journal_changed')
                items.append(view(row))
        return dict(items=items,next_cursor=identifiers[19] if len(identifiers)>20 else '')

    async def check(self,relationship_id):return await Preflight(self.journal).master(relationship_id,self.transport)

    async def apply(self,relationship_id,confirm_member_id,actor):
        status=await self.status(relationship_id)
        if confirm_member_id!=status['member_id']:raise RetirementError('retirement_confirmation_mismatch')
        if status['phase']=='not_started':
            report=await self.check(relationship_id)
            if not report['ready']:return dict(state='blocked',report=report)
            await self.journal.begin_master(relationship_id,confirm_member_id,actor)
        row=await Coordinator(self.journal,self.transport).advance(relationship_id,confirm_member_id,actor)
        return dict(state='completed',operation=view(row))
