"""Identity-bound retirement preparation; physical completion is a separate gate.

No HTTP execution is exported here. These journals fence ordinary work and survive
lost acknowledgements; a stored observation never authorizes terminal completion.
"""
import hashlib
import hmac
import json
import time
import uuid

from sqlalchemy import String,Text,cast,func,insert,literal,select,text,update

from app.services import node_retirement_schema as s,resource_fences
from app.services.federation import protocol as p,schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.node_retirement_fences import RetirementError
from app.services.node_retirement_gate import Gate


def _hash(value):return hashlib.sha256(value).hexdigest()


def _authenticated(current,expected):
    if expected is None:return
    fields=('relationship_id','peer_id','peer_endpoint','peer_key','credential','direction','mode','protocol')
    if (not isinstance(expected,dict) or any(current.get(name)!=expected.get(name) for name in fields if name!='credential') or
            not isinstance(expected.get('credential'),str) or not hmac.compare_digest(current['credential'],expected['credential'])):
        raise RetirementError('retirement_authenticated_relation_changed')


def _columns(table,*,names=None,limits=None):
    limits={} if limits is None else limits
    result=[]
    for column in table.c:
        if names is not None and column.name not in names:continue
        limit=limits.get(column.name)
        if limit is None and isinstance(column.type,String):limit=column.type.length or 65536
        result.append(column if limit is None else func.substr(cast(column,Text),1,limit+1).label(column.name))
    return result


def binding(node,relation,store,identifier):
    if (not isinstance(identifier,str) or not p.IDENTIFIER.fullmatch(identifier) or not p.IDENTIFIER.fullmatch(relation['relationship_id'])
            or not p.IDENTIFIER.fullmatch(node['node_id']) or not p.IDENTIFIER.fullmatch(relation['peer_id'])
            or node['node_id']==relation['peer_id'] or relation['protocol']!=p.PROTOCOL_VERSION
            or relation['mode'] not in {'Direct','Relay'}):raise RetirementError('retirement_binding_invalid')
    own_key=p.public_key(store.unseal(node['private_key']))
    if len(p.decode(relation['peer_key']))!=32:raise RetirementError('retirement_peer_key_invalid')
    if node['role']=='Master' and relation['direction']=='downstream':
        master,member=node['node_id'],relation['peer_id'];master_key,member_key=own_key,relation['peer_key']
        master_endpoint,member_endpoint=node['endpoint'],relation['peer_endpoint']
    elif node['role']=='Follower' and relation['direction']=='upstream':
        master,member=relation['peer_id'],node['node_id'];master_key,member_key=relation['peer_key'],own_key
        master_endpoint,member_endpoint=relation['peer_endpoint'],node['endpoint']
    else:raise RetirementError('retirement_role_invalid')
    credential=p.decode(store.unseal(relation['credential']))
    if len(credential)!=48:raise RetirementError('retirement_credential_invalid')
    value=dict(version=1,retirement_id=identifier,relationship_id=relation['relationship_id'],master_id=master,member_id=member,
        master_key_sha256=_hash(p.decode(master_key)),member_key_sha256=_hash(p.decode(member_key)),
        master_endpoint=p.endpoint(master_endpoint),member_endpoint=p.endpoint(member_endpoint),protocol=p.PROTOCOL_VERSION,mode=relation['mode'])
    return value,_hash(credential)


def _decode(row):
    try:
        result=dict(row)
        for field,limit in (('binding_json',8192),('roots_json',8192),('receipt_json',16384)):
            raw=result[field]
            if not isinstance(raw,str) or len(raw.encode())>limit:raise ValueError()
            value=json.loads(raw)
            if not isinstance(value,dict) or p.canonical(value).decode()!=raw:raise ValueError()
            result[field[:-5]]=value
        for name in ('relationship_id','retirement_id','node_id','master_id','member_id'):
            if not isinstance(result[name],str) or not p.IDENTIFIER.fullmatch(result[name]):raise ValueError()
        for name in ('binding_sha256','authorization_sha256'):
            if not isinstance(result[name],str) or not p.OBJECT_ID.fullmatch(result[name]):raise ValueError()
        if (result['side'] not in {'master','peer'} or result['phase'] not in {'retiring','completed'} or
                any(type(result[name]) is not int for name in ('revision','started_at','completed_at')) or
                result['revision']<=0 or result['started_at']<=0 or result['completed_at']<0 or
                result['phase']=='retiring' and (result['completed_at']!=0 or result['receipt']) or
                result['phase']=='completed' and (result['completed_at']<result['started_at'] or not result['receipt'])):raise ValueError()
        value=result['binding']
        if (_hash(p.canonical(value))!=result['binding_sha256'] or value['retirement_id']!=result['retirement_id']
                or value['relationship_id']!=result['relationship_id'] or value['master_id']!=result['master_id']
                or value['member_id']!=result['member_id']):raise ValueError()
        return result
    except (ValueError,TypeError,KeyError):raise RetirementError('retirement_journal_invalid') from None


async def _present(conn,table,condition=True):
    return await conn.scalar(select(literal(1)).select_from(table).where(condition).limit(1).with_for_update()) is not None


async def ledger_clear(conn,node,relation):
    """Fresh logical blockers only; this is never an empty-filesystem proof."""
    member=relation['peer_id'] if node['role']=='Master' else node['node_id']
    try:await resource_fences.member_clear(conn,member,readers=True)
    except resource_fences.FenceConflict as exc:raise RetirementError(exc.code) from None
    await _logical_clear(conn,node,relation)


async def _logical_clear(conn,node,relation):
    # Only after ordinary fence checking or verified terminal ownership.
    from app.services import karaoke_schema as ks
    member=relation['peer_id'] if node['role']=='Master' else node['node_id']
    if await _present(conn,fs.global_media,fs.global_media.c.storage_member_id==member):
        raise RetirementError('retirement_owned_media')
    if await _present(conn,ks.recordings,ks.recordings.c.storage_member_id==member):
        raise RetirementError('retirement_owned_recordings')
    if await _present(conn,fs.upload_sessions,(fs.upload_sessions.c.storage_member_id==member)&fs.upload_sessions.c.state.not_in(['complete','aborted'])):
        raise RetirementError('retirement_unfinished_upload')
    member_row=(await conn.execute(select(*_columns(fs.storage_members)).where(fs.storage_members.c.member_id==member).with_for_update())).mappings().first()
    if member_row:
        expected_relationship=relation['relationship_id'] if node['role']=='Master' else None
        expected_transport=relation['mode'] if node['role']=='Master' else 'Local'
        if (member_row['member_kind']!='Follower' or member_row['relationship_id']!=expected_relationship or
                member_row['transport']!=expected_transport):
            raise RetirementError('retirement_member_binding_changed')
        if member_row['used_bytes']!=0 or member_row['reserved_bytes']!=0:raise RetirementError('retirement_member_usage')
    elif node['role']=='Master':raise RetirementError('retirement_member_unavailable')
    backup=(await conn.execute(select(*_columns(fs.backup_members)).where(fs.backup_members.c.member_id==member).with_for_update())).mappings().first()
    if backup and (backup['enabled'] or backup['generation'] or backup['checksum'] or backup['state']!='disabled'):
        raise RetirementError('retirement_backup_retained')
    if node['role']=='Follower':
        if await conn.scalar(text("SELECT 1 FROM media_objects WHERE object_kind IN ('audio','video') LIMIT 1")) is not None:
            raise RetirementError('retirement_native_media')
        if await _present(conn,fs.business_backups) or await _present(conn,fs.business_backup_chunks):
            raise RetirementError('retirement_backup_retained')
        if await conn.scalar(text("SELECT 1 FROM media_delete_operations WHERE state NOT IN ('committed','rolled_back') LIMIT 1")) is not None:
            raise RetirementError('retirement_unfinished_delete')


class Journal:
    def __init__(self,store,*,gate=None,authority=None,wall=time.time):
        self.store,self.gate,self.authority,self.wall=store,gate,authority,wall

    async def _context(self,conn,relationship_id):
        if not isinstance(relationship_id,str) or not p.IDENTIFIER.fullmatch(relationship_id):raise RetirementError('retirement_relationship_invalid')
        node=await self.store.lock(conn)
        if self.store.node['node_id']!=node['node_id']:raise RetirementError('retirement_cached_identity_changed')
        relation=(await conn.execute(select(*_columns(fs.relationships,names={'relationship_id','peer_id','peer_endpoint','peer_key',
            'credential','direction','mode','state','protocol','summary'},limits={'summary':65536})).where(
                fs.relationships.c.relationship_id==relationship_id).with_for_update())).mappings().first()
        if relation is None:raise RetirementError('retirement_relationship_unknown')
        relation=dict(relation)
        try:
            if len(relation['summary'].encode())>65536:raise ValueError()
            relation['summary']=json.loads(relation['summary'])
            if not isinstance(relation['summary'],dict):raise ValueError()
        except (ValueError,TypeError,RecursionError):raise RetirementError('retirement_relationship_metadata_invalid') from None
        return node,relation

    async def _existing(self,conn,node,relation):
        row=(await conn.execute(select(*_columns(s.retirements,limits={'binding_json':8192,'roots_json':8192,'receipt_json':16384})).where(
            s.retirements.c.relationship_id==relation['relationship_id']).with_for_update())).mappings().first()
        if row is None:return None
        result=_decode(row);expected,authorization=binding(node,relation,self.store,result['retirement_id'])
        if (p.canonical(result['binding'])!=p.canonical(expected) or result['authorization_sha256']!=authorization or result['node_id']!=node['node_id']
                or result['side']!=('master' if node['role']=='Master' else 'peer')
                or relation['state']!=('retiring' if result['phase']=='retiring' else 'revoked')):
            raise RetirementError('retirement_identity_changed')
        if result['side']=='peer':
            if p.canonical(result['roots'])!=p.canonical(self._roots()):raise RetirementError('retirement_roots_changed')
            if self.authority is not None:await self.authority.verify_database(conn)
        if result['phase']=='completed':
            from app.services.node_retirement_receipts import recorded
            recorded(self.store,node,relation,result,self.wall())
        return result

    def _roots(self):
        if type(self.gate) is not Gate:raise RetirementError('retirement_gate_unavailable')
        self.gate.check()
        result=dict(version=1,data_root=str(self.gate.root),root_identity=list(self.gate.root_identity),request_identity=list(self.gate.identity))
        if self.authority is not None:result['native']=self.authority.binding()
        return result

    async def _begin(self,conn,node,relation,value,authorization,roots,actor):
        if relation['state']!='active':raise RetirementError('retirement_relationship_inactive')
        await ledger_clear(conn,node,relation)
        started=int(self.wall())
        if started<=0:raise RetirementError('retirement_clock_invalid')
        await conn.execute(insert(s.retirements).values(relationship_id=relation['relationship_id'],retirement_id=value['retirement_id'],
            node_id=node['node_id'],master_id=value['master_id'],member_id=value['member_id'],side='master' if node['role']=='Master' else 'peer',
            phase='retiring',binding_sha256=_hash(p.canonical(value)),authorization_sha256=authorization,
            binding_json=p.canonical(value).decode(),roots_json=p.canonical(roots).decode(),receipt_json='{}',revision=1,started_at=started,completed_at=0))
        await conn.execute(update(fs.relationships).where(fs.relationships.c.relationship_id==relation['relationship_id']).values(state='retiring'))
        await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==value['member_id']).values(storage_enabled=0,writable=0))
        await self.store.log(conn,'retirement-started',actor,relation['relationship_id'],retirement_id=value['retirement_id'],member_id=value['member_id'])
        return await self._existing(conn,node,{**relation,'state':'retiring'})

    async def begin_master(self,relationship_id,confirm_member_id,actor):
        async with media_mutation_lock,self.store.database.begin() as conn:
            node,relation=await self._context(conn,relationship_id)
            if node['role']!='Master' or relation['direction']!='downstream':raise RetirementError('retirement_role_invalid')
            if confirm_member_id!=relation['peer_id']:raise RetirementError('retirement_confirmation_mismatch')
            existing=await self._existing(conn,node,relation)
            if existing:return existing
            value,authorization=binding(node,relation,self.store,uuid.uuid4().hex)
            return await self._begin(conn,node,relation,value,authorization,{},actor)

    async def begin_peer(self,relationship_id,value,*,authenticated_relation=None):
        if not isinstance(value,dict) or len(p.canonical(value))>8192:raise RetirementError('retirement_binding_invalid')
        async with media_mutation_lock,self.store.database.begin() as conn:
            node,relation=await self._context(conn,relationship_id)
            _authenticated(relation,authenticated_relation)
            if node['role']!='Follower' or relation['direction']!='upstream':raise RetirementError('retirement_role_invalid')
            expected,authorization=binding(node,relation,self.store,value.get('retirement_id',''))
            if p.canonical(value)!=p.canonical(expected):raise RetirementError('retirement_binding_changed')
            existing=await self._existing(conn,node,relation)
            if existing:
                if existing['binding']!=value:raise RetirementError('retirement_operation_conflict')
                return existing
            return await self._begin(conn,node,relation,value,authorization,self._roots(),value['master_id'])

    async def read(self,relationship_id,*,authenticated_relation=None):
        async with self.store.database.begin() as conn:
            node,relation=await self._context(conn,relationship_id)
            _authenticated(relation,authenticated_relation)
            result=await self._existing(conn,node,relation)
            if result is None:raise RetirementError('retirement_not_started')
            return result
