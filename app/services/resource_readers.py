"""Durable ordinary-read leases; no media bytes or decryption keys are read.

Nginx may start sendfile after ASGI returns, so leases are not closed by that
return. They cover the signed capability deadline; live HTTP counters separately
protect transfers which outlast it. Cleanup intent and new reads serialize on
the media fence and node identity row.
"""
import os
import json
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import Text,cast,delete,func,insert,select,text

from app.core.joined_io import joined_io
from app.services import resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_observation import digest,native


def _version(root,path):
    from app.middleware.native_storage import _open_beneath
    descriptor,info=_open_beneath(root,path.split('/'))
    try:
        if info.st_nlink!=1:raise ValueError('Invalid reader resource')
        return digest(native(info))
    finally:os.close(descriptor)


async def _begin(store,relation,logical,object_id,kind,path,root,expires_at):
    now=int(time.time())
    if type(expires_at) is not int or not now<expires_at<=now+p.TOKEN_SECONDS+60:
        raise HTTPException(401,'Media capability invalid or expired')
    async with media_mutation_lock.shared(),store.database.begin() as conn:
        node=await store.lock(conn)
        active=(await conn.execute(select(fs.relationships).where(fs.relationships.c.relationship_id==relation['relationship_id']))).mappings().first()
        if (node['role']!='Follower' or node['node_id']!=store.node['node_id'] or not active or
                active['state']!='active' or active['direction']!='upstream' or
                any(active[name]!=relation[name] for name in ('peer_id','peer_key','peer_endpoint','credential','protocol'))):
            raise HTTPException(401,'Media relationship changed')
        if kind=='media':
            current=await conn.scalar(text("SELECT media_path FROM media_objects WHERE media_id=:id AND object_kind IN ('audio','video')"),dict(id=object_id))
            if current!=path:raise HTTPException(409,'Media resource changed; refresh the request')
        blocked=await conn.scalar(select(s.units.c.unit_id).join(s.peer_reservations,s.units.c.task_id==s.peer_reservations.c.task_id).where(
            s.peer_reservations.c.member_id==node['node_id'],s.peer_reservations.c.side=='source',s.peer_reservations.c.state=='active',
            s.units.c.resource_kind==kind,s.units.c.parent_path==path.rsplit('/',1)[0],s.units.c.state.in_(['cleanup_pending','cleaned'])).limit(1))
        if blocked:raise HTTPException(409,'Media placement changed; refresh the request')
        # A published, uncommitted Target shadow may still have a registry row.
        # Cancellation intent and new read leases serialize on this same fence.
        targets=list((await conn.execute(select(func.substr(cast(s.units.c.publication,Text),1,65537)).join(
            s.peer_reservations,s.units.c.task_id==s.peer_reservations.c.task_id).where(
            s.peer_reservations.c.member_id==node['node_id'],s.peer_reservations.c.side=='target',s.peer_reservations.c.state=='active',
            s.units.c.resource_kind==kind,s.units.c.parent_path==path.rsplit('/',1)[0],
            ~((s.units.c.state=='cancelled')&(s.units.c.native_acknowledged==1))).limit(2))).scalars())
        try:
            if len(targets)>1:raise ValueError()
            for raw in targets:
                if len(raw.encode())>65536:raise ValueError()
                publication=json.loads(raw)
                if not isinstance(publication,dict) or publication.get('target_cancel'):raise ValueError()
        except (ValueError,TypeError,AttributeError,RecursionError):raise HTTPException(409,'Media placement changed; refresh the request') from None
        expired=list((await conn.execute(select(s.read_leases.c.lease_id).where(s.read_leases.c.member_id==node['node_id'],
            s.read_leases.c.expires_at<=now).limit(32))).scalars())
        if expired:await conn.execute(delete(s.read_leases).where(s.read_leases.c.lease_id.in_(expired),s.read_leases.c.member_id==node['node_id']))
        try:version=await joined_io(_version,root,path)
        except (OSError,ValueError):raise HTTPException(404,'Media resource unavailable') from None
        if expires_at<=int(time.time()):raise HTTPException(401,'Media capability expired while opening the resource')
        lease=uuid.uuid4().hex
        await conn.execute(insert(s.read_leases).values(lease_id=lease,member_id=node['node_id'],logical_id=logical,
            object_id=object_id,source_version=version,expires_at=expires_at+5,task_id='',purpose='playback'))
        return lease


async def media(store,relation,object_id,logical,path,expires_at):
    from app.services.media_manager import MEDIA_ROOT
    return await _begin(store,relation,logical,object_id,'media',path,MEDIA_ROOT,expires_at)


async def recording(store,relation,recording_id,user_id,expires_at):
    from app.services.karaoke_storage import ROOT
    path=relation['relationship_id']+'/'+user_id+'/'+recording_id+'.bin'
    return await _begin(store,relation,recording_id,recording_id,'recording',path,ROOT,expires_at)
