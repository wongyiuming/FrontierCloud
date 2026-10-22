"""Master-only metadata coordination; Target pulls Source bytes directly."""
import json
import time
import uuid

import httpx

from sqlalchemy import func, insert, select, update

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_control_client import signed_request
from app.services.resource_migration_admission import Admission
from app.services.resource_migration_journal import Journal
from app.services.resource_observation import digest
from app.services.resource_encryption_binding import source_digest
from app.services.resource_plan_store import relation_binding
from app.services.resource_transfer_protocol import CONTROL_BYTES,WINDOW_SECONDS,TransferError,identifier


class TargetClient:
    def __init__(self,store,relation,transport):self.store,self.relation,self.transport=store,relation,transport

    async def call(self,action,grant):
        request_id=uuid.uuid4().hex
        value=await signed_request(self.store,self.relation,self.transport,'/internal/v1/migration/transfer/'+action,
            dict(request_id=request_id,grant=grant),limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_transfer')
        raw=grant['payload'];obj=raw['object']
        expected=dict(format='frontiercloud-python-target-transfer',version=1,request_id=request_id,task_id=raw['task_id'],
            node_id=raw['target_id'],epoch=raw['epoch'],grant_sha256=digest(grant),logical_id=obj['logical_id'] if obj else '',
            expires_at=raw['expires_at'],writable_verified=True,peer_verified=True)
        if (not isinstance(value,dict) or value.keys()!={*expected,'state','received_bytes','hashed_bytes','sha256'} or
                any(value.get(name)!=item for name,item in expected.items()) or type(value.get('version')) is not int or
                type(value.get('epoch')) is not int or value['writable_verified'] is not True or value['peer_verified'] is not True or
                any(type(value[name]) is not int for name in ('received_bytes','hashed_bytes','expires_at'))):
            raise TransferError('migration_transfer_target_reply_invalid')
        if action=='probe':
            if value['state']!='probed' or value['received_bytes'] or value['hashed_bytes'] or value['sha256']:
                raise TransferError('migration_transfer_target_reply_invalid')
        elif (value['state'] not in {'receiving','reconciling','verified'} or
                not 0<=value['hashed_bytes']<=value['received_bytes']<=obj['expected_bytes'] or
                (value['state']=='verified' and (value['received_bytes']!=obj['expected_bytes'] or
                    value['hashed_bytes']!=obj['expected_bytes'] or value['sha256']!=obj['sha256'])) or
                (value['state']!='verified' and value['sha256']!='')):
            raise TransferError('migration_transfer_target_reply_invalid')
        return value


class TransferMaster:
    def __init__(self,store,transport,*,traffic=None,wall=time.time):
        self.store,self.transport,self.wall=store,transport,wall
        self.preparation=Admission(store,transport,traffic=traffic,wall=wall)
        self.journal=Journal(store,clock=wall,traffic=traffic)

    async def relations(self,task,*,conn=None):
        if conn is None:
            async with self.store.database.connect() as opened:return await self.relations(task,conn=opened)
        node=(await conn.execute(select(fs.identity.c.node_id,fs.identity.c.role).where(fs.identity.c.singleton==1))).mappings().one()
        if node['role']!='Master' or node['node_id']!=task['master_id']:raise TransferError('migration_transfer_master_changed')
        relations={}
        for side in ('source','target'):
            member_id=task[side+'_id']
            relation=(await conn.execute(select(fs.relationships).where(fs.relationships.c.relationship_id==task[side+'_relationship']))).mappings().first()
            member=(await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id==member_id))).mappings().first()
            original=task['plan']['facts']['storage'][member_id]
            if (not relation or not member or relation['peer_id']!=member_id or relation['direction']!='downstream' or
                    relation_binding(relation)!=task['plan']['facts']['relationships'][member_id] or
                    member['relationship_id']!=relation['relationship_id'] or
                    any(member[name]!=original[name] for name in ('member_id','member_kind','transport','storage_enabled','allocated_bytes','writable'))):
                raise TransferError('migration_transfer_members_changed')
            if (relation['status']!='online' or self.wall()-relation['last_heartbeat']>p.OFFLINE_SECONDS
                    or member['health']!='online'):
                raise TransferError('migration_transfer_peer_offline')
            relations[member_id]=dict(relation)
        return relations

    def _grant(self,task,relations,epoch,runner_id,purpose,obj=None):
        grant=dict(format='frontiercloud-python-transfer-grant',version=1,
            **{name:task[name] for name in ('task_id','master_id','source_id','target_id','source_relationship','target_relationship','plan_sha256')},
            units_sha256=task['plan']['units_sha256'],epoch=epoch,runner_id=runner_id,purpose=purpose,
            created_at=int(self.wall()),expires_at=int(self.wall())+WINDOW_SECONDS,nonce=uuid.uuid4().hex,object=obj)
        for side in ('source','target'):
            relation=relations[task[side+'_id']]
            grant.update({side+'_endpoint':relation['peer_endpoint'],side+'_key':relation['peer_key'],
                side+'_binding':digest(self.preparation._request(task,side))})
        return p.sign(self.store.unseal(self.store.node['private_key']),grant)

    async def preflight(self,task_id,actor):
        identifier(task_id)
        async with self.store.database.connect() as conn:task=await self.preparation._task(conn,task_id)
        if task['state']!='prepared':raise TransferError('migration_transfer_requires_prepared_task')
        await self.preparation.prepare(task_id,task['plan_sha256'],actor,revalidate=True)
        async with self.preparation.lock,self.preparation._running(task_id):
            try:
                async with self.store.database.connect() as conn:task=await self.preparation._task(conn,task_id)
                relations=await self.preparation._relations(task)
                grant=self._grant(task,relations,self.preparation.lease[1],self.preparation.runner_id,'probe')
                result=await TargetClient(self.store,relations[task['target_id']],self.transport).call('probe',grant)
                async with media_mutation_lock,self.store.database.begin() as conn:
                    current=await self.preparation._locked(conn,task_id)
                    facts,_,_=await self.preparation.spool.facts(task['source_id'],task['target_id'],conn=conn,task_id=task_id)
                    if current['state']!='prepared' or facts!=task['plan']['facts'] or self.wall()>=grant['payload']['expires_at']:
                        raise TransferError('migration_transfer_preflight_stale')
                    plan={**current['plan'],'phase':'queued','execution':dict(writable_verified=True,peer_verified=True,
                        proof_sha256=digest(result),verified_at=int(self.wall()))}
                    await conn.execute(update(s.tasks).where(s.tasks.c.task_id==task_id).values(state='queued',plan=plan,
                        revision=current['revision']+1,error_code='',updated_at=int(self.wall())))
                    await self.store.log(conn,'migration_direct_preflight_verified',actor,task_id=task_id)
                return dict(task_id=task_id,state='queued',writable_verified=True,peer_verified=True)
            except (p.ProtocolError,OSError,TimeoutError,httpx.HTTPError) as exc:
                await self.preparation._error(task_id,exc)
                raise

    async def start(self,task_id,runner_id):
        lease=await self.journal.claim(task_id,runner_id,ttl=60)
        async with self.store.database.begin() as conn:task=await self.journal._fenced(conn,lease)
        if task['state'] not in {'queued','copying'}:raise TransferError('migration_transfer_state_invalid')
        if task['state']=='queued':lease=await self.journal.checkpoint(lease,'copying',committed_bytes=task['committed_bytes'])
        return lease

    async def _object(self,conn,task,logical_id):
        row=(await conn.execute(select(s.plan_bindings).where(s.plan_bindings.c.plan_id==task['task_id'],
            s.plan_bindings.c.logical_id==logical_id))).mappings().first()
        if not row:raise TransferError('migration_transfer_object_unplanned')
        snapshot=json.loads(row['object_json']);content=snapshot['source_proof']['result']
        unit=(await conn.execute(select(s.units).where(s.units.c.task_id==task['task_id'],s.units.c.unit_id==row['unit_id']))).mappings().one()
        obj=dict(logical_id=logical_id,unit_id=row['unit_id'],resource_kind=row['resource_kind'],source_object=row['source_object'],
            source_path=snapshot['physical_path'],target_path=snapshot['target_path'],source_version=content['source_version'],
            unit_version=unit['source_version'],expected_bytes=row['expected_bytes'],sha256=row['source_sha256'],
            metadata_sha256=content['metadata_sha256'],encryption_sha256=content['encryption_sha256'],user_id=snapshot['master'].get('user_id',''))
        if source_digest(snapshot)!=content['encryption_sha256']:
            raise TransferError('migration_transfer_source_encryption_changed')
        # Existing task journals retain their exact immutable object shape.
        # Only plans with an explicit native binding use the extended form.
        if 'source_encryption_sha256' in snapshot:obj['master_encryption_sha256']=digest(snapshot['encryption'])
        expected=dict(unit_id=obj['unit_id'],resource_kind=obj['resource_kind'],source_object=obj['source_object'],
            target_object=obj['source_object'],source_version=obj['source_version'],physical_path=obj['source_path'],
            expected_bytes=obj['expected_bytes'],source_sha256=obj['sha256'])
        from app.services import media_crypto as crypto,media_crypto_schema as cs
        encoded='';file_id=''
        if obj['resource_kind']=='media':
            encryption=(await conn.execute(select(cs.encryption).where(cs.encryption.c.object_kind=='media',
                cs.encryption.c.object_id==logical_id))).mappings().first()
            if encryption is not None:
                if encryption['descriptor_json'] is None:raise TransferError('migration_transfer_master_encryption_changed')
                metadata=crypto.Metadata.from_dict(json.loads(encryption['descriptor_json']))
                if metadata.to_dict()!=snapshot['encryption'] or metadata.file_id!=encryption['file_id']:
                    raise TransferError('migration_transfer_master_encryption_changed')
                encoded=encryption['descriptor_json'];file_id=encryption['file_id']
            elif snapshot['encryption'] is not None:raise TransferError('migration_transfer_master_encryption_changed')
        expected.update(catalog_etag=snapshot['master'].get('etag',''),
            catalog_path_locator=snapshot['master'].get('path_locator',''),
            catalog_media_kind=snapshot['master'].get('object_kind',''),catalog_user_id=obj['user_id'],
            catalog_encryption_json=encoded,catalog_encryption_file_id=file_id)
        old=(await conn.execute(select(s.objects).where(s.objects.c.task_id==task['task_id'],s.objects.c.logical_id==logical_id))).mappings().first()
        if old:
            if any(old[name]!=value for name,value in expected.items()) or old['state'] not in {'pending','copying','verified'}:
                raise TransferError('migration_transfer_object_changed')
        else:await conn.execute(insert(s.objects).values(task_id=task['task_id'],logical_id=logical_id,**expected,state='pending',updated_at=int(self.wall())))
        return obj

    async def copy_one(self,lease,logical_id):
        if not isinstance(logical_id,str) or not (p.OBJECT_ID.fullmatch(logical_id) or p.IDENTIFIER.fullmatch(logical_id)):
            raise TransferError('migration_transfer_identity_invalid')
        if not self.journal.traffic.snapshot()['can_run']:raise TransferError('migration_transfer_waiting_low_traffic')
        await self.journal.renew(lease,ttl=60)
        async with self.store.database.begin() as conn:
            task=await self.journal._fenced(conn,lease)
            proof=task['plan'].get('execution',{})
            if task['state']!='copying' or proof.get('writable_verified') is not True or proof.get('peer_verified') is not True:
                raise TransferError('migration_transfer_preflight_required')
        async with self.store.database.begin() as conn:
            task=await self.journal._fenced(conn,lease)
            obj=await self._object(conn,task,logical_id)
            relations=await self.relations(task,conn=conn)
            grant=self._grant(task,relations,lease.epoch,lease.runner_id,'copy',obj)
        result=await TargetClient(self.store,relations[task['target_id']],self.transport).call('copy',grant)
        async with self.store.database.begin() as conn:
            task=await self.journal._fenced(conn,lease)
            verified=result['state']=='verified'
            await conn.execute(update(s.objects).where(s.objects.c.task_id==task['task_id'],s.objects.c.logical_id==logical_id).values(
                state='verified' if verified else 'copying',target_sha256=result['sha256'],
                verified_bytes=result['received_bytes'] if verified else 0,updated_at=int(self.wall())))
            await conn.execute(update(s.tasks).where(s.tasks.c.task_id==task['task_id']).values(
                plan={**task['plan'],'phase':'copying','current_object':dict(logical_id=logical_id,state=result['state'],received_bytes=result['received_bytes'],
                    hashed_bytes=result['hashed_bytes'])},updated_at=int(self.wall())))
        return lease,result
