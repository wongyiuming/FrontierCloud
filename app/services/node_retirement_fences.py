"""Durable exclusions for retirement; no age-based release or cleanup authority."""
from sqlalchemy import and_,or_,select

from app.services import node_retirement_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_fences import FenceConflict


class RetirementError(FenceConflict):
    def __init__(self,code):self.code=code;p.ProtocolError.__init__(self,code)


async def _own(conn):
    node=await conn.scalar(select(fs.identity.c.node_id).where(fs.identity.c.singleton==1).with_for_update())
    if not node:raise RetirementError('retirement_identity_unavailable')
    return node


async def clear(conn,*,member_id=None,reset=False,relationship_id=None):
    node=await _own(conn)
    conditions=[and_(s.retirements.c.node_id==node,s.retirements.c.phase=='retiring')] if reset else [
        and_(s.retirements.c.node_id==node,s.retirements.c.side=='peer')]
    if member_id is not None:
        conditions.append(and_(s.retirements.c.node_id==node,s.retirements.c.member_id==member_id))
    if relationship_id is not None:conditions.append(s.retirements.c.relationship_id==relationship_id)
    if await conn.scalar(select(s.retirements.c.relationship_id).where(or_(*conditions)).limit(1).with_for_update()):
        raise RetirementError('retirement_in_progress' if reset else 'retirement_identity_fenced')
