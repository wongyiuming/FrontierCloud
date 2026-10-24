"""Atomic immediate-parent ownership cutover; no filesystem deletion.

Native publication and Source verification are separate prerequisites. The
transaction changes only placement and exact Master accounting, preserving live
business rows. Native accounting acknowledgement and source cleanup follow it.
"""
import uuid

from sqlalchemy import and_,func,or_,select,update

from app.services import karaoke_schema as ks,media_crypto_schema as cs,resource_migration_schema as s
from app.services.federation import schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_migration_journal import Lease
from app.services.resource_observation import digest
from app.services.resource_publish_master import Publication
from app.services.resource_control_client import signed_request
from app.services.resource_transfer_protocol import CONTROL_BYTES,TransferError,identifier

COMMITTED=('committed','cleanup_pending','cleaned')
GRACE_SECONDS=3600


class Cutover:
    def __init__(self,store,transport,**options):
        self.publication=Publication(store,transport,**options)
        self.transfer=self.publication.transfer;self.store,self.transport=store,transport

    async def _unit(self,conn,task,unit_id):
        row=(await conn.execute(select(s.units).where(s.units.c.task_id==task['task_id'],s.units.c.unit_id==unit_id).with_for_update())).mappings().first()
        if row is None:raise TransferError('migration_commit_unit_unavailable')
        return dict(row)

    async def _accounting(self,conn,task):
        await self.transfer.relations(task,conn=conn)
        committed=int(await conn.scalar(select(func.coalesce(func.sum(s.units.c.total_bytes),0)).where(
            s.units.c.task_id==task['task_id'],s.units.c.state.in_(COMMITTED))))
        cancelled=int(await conn.scalar(select(func.coalesce(func.sum(s.units.c.total_bytes),0)).where(
            s.units.c.task_id==task['task_id'],s.units.c.state=='cancelled',s.units.c.native_acknowledged==1)))
        own=(await conn.execute(select(s.capacity).where(s.capacity.c.task_id==task['task_id']).with_for_update())).mappings().first()
        expected=task['total_bytes']-committed-cancelled
        if (committed!=task['committed_bytes'] or expected<0 or not own or own['member_id']!=task['target_id'] or
                own['reserved_bytes']!=expected):raise TransferError('migration_commit_capacity_inconsistent')
        members={}
        for side in ('source','target'):
            row=(await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id==task[side+'_id']).with_for_update())).mappings().first()
            if (not row or row['member_kind']!='Follower' or row['relationship_id']!=task[side+'_relationship'] or
                    not row['storage_enabled'] or not row['writable'] or row['health']!='online'):
                raise TransferError('migration_commit_member_unavailable')
            members[side]=dict(row)
        target=members['target']
        if (target['reserved_bytes']<expected or target['used_bytes']+target['reserved_bytes']>target['allocated_bytes'] or
                members['source']['used_bytes']<expected):
            raise TransferError('migration_commit_capacity_inconsistent')
        return committed,expected,members

    def _proofs(self,unit,lease,now):
        target=unit['publication'].get('target_reply',{});source=unit['publication'].get('source_reply',{})
        for side,proof in (('target',target),('source',source)):
            if (proof.get('epoch')!=lease.epoch or proof.get('unit_version')!=unit['source_version'] or
                    proof.get('object_count')!=unit['object_count'] or proof.get('total_bytes')!=unit['total_bytes'] or
                    type(proof.get('expires_at')) is not int or proof['expires_at']<=now):
                raise TransferError('migration_commit_'+side+'_proof_stale')
            identifier(proof.get('receipt_sha256'),long=True)
        if target.get('state')!='published' or target.get('phase')!='published' or source.get('phase')!='verified':
            raise TransferError('migration_commit_proofs_incomplete')
        return source,target

    async def _scopes(self,conn,task,unit):
        source_parent=unit['parent_path']
        target_parent=source_parent if unit['resource_kind']=='media' else task['target_relationship']+'/'+source_parent.rsplit('/',1)[-1]
        for side,parent in (('source',source_parent),('target',target_parent)):
            if not await conn.scalar(select(s.scopes.c.scope_id).where(s.scopes.c.task_id==task['task_id'],
                    s.scopes.c.member_id==task[side+'_id'],s.scopes.c.resource_kind==unit['resource_kind'],s.scopes.c.parent_path==parent)):
                raise TransferError('migration_commit_scope_missing')
        if not await conn.scalar(select(s.drains.c.owner_id).where(s.drains.c.member_id==task['source_id'],
                s.drains.c.owner_id==task['task_id'],s.drains.c.reason=='migration')):
            raise TransferError('migration_commit_source_drain_missing')
        return target_parent

    async def _source_catalog(self,conn,task,unit,*,require_copy=True):
        owned=and_(s.objects.c.task_id==task['task_id'],s.objects.c.unit_id==unit['unit_id'])
        complete=and_(s.objects.c.state=='verified',s.objects.c.verified_bytes==s.objects.c.expected_bytes,
            s.objects.c.source_sha256!='',s.objects.c.target_sha256==s.objects.c.source_sha256)
        valid=complete if require_copy else or_(complete,and_(s.objects.c.state.in_(['pending','copying']),
            s.objects.c.verified_bytes==0,s.objects.c.source_sha256!='',s.objects.c.target_sha256==''))
        counts=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.objects.c.expected_bytes),0)).where(owned,
            s.objects.c.resource_kind==unit['resource_kind'],valid))).one()
        total=int(await conn.scalar(select(func.count()).select_from(s.objects).where(owned)))
        if tuple(counts)!=(unit['object_count'],unit['total_bytes']) or total!=unit['object_count']:
            raise TransferError('migration_commit_objects_changed')
        if unit['resource_kind']=='media':
            table=fs.global_media;key=table.c.media_id
            present=select(cs.encryption.c.object_id).where(cs.encryption.c.object_kind=='media',
                cs.encryption.c.object_id==s.objects.c.logical_id).correlate(s.objects)
            # HEX equality keeps byte comparison independent of a legacy
            # encryption column's MySQL collation. Only bounded metadata enters it.
            matching=present.where(func.hex(cs.encryption.c.descriptor_json)==func.hex(s.objects.c.catalog_encryption_json),
                cs.encryption.c.file_id==s.objects.c.catalog_encryption_file_id)
            match=and_(table.c.storage_member_id==task['source_id'],table.c.object_id==s.objects.c.source_object,
                table.c.media_path==s.objects.c.physical_path,table.c.path_locator==s.objects.c.catalog_path_locator,
                table.c.object_kind==s.objects.c.catalog_media_kind,table.c.size_bytes==s.objects.c.expected_bytes,
                table.c.etag==s.objects.c.catalog_etag,table.c.state=='active',
                or_(and_(s.objects.c.catalog_encryption_json=='',~present.exists()),
                    and_(s.objects.c.catalog_encryption_json!='',matching.exists())))
            # Descendant objects would split the selected parent. Ordinary
            # writers cannot add them while these durable scopes are held.
            live=select(func.count()).select_from(table).where(table.c.media_path.startswith(unit['parent_path']+'/',autoescape=True))
        else:
            table=ks.recordings;key=table.c.recording_id
            user=unit['parent_path'].rsplit('/',1)[-1]
            match=and_(table.c.storage_member_id==task['source_id'],table.c.user_id==s.objects.c.catalog_user_id,
                table.c.user_id==user,table.c.recording_id==s.objects.c.source_object,table.c.state=='ready',
                table.c.size_bytes==s.objects.c.expected_bytes,table.c.sha256==s.objects.c.source_sha256)
            live=select(func.count()).select_from(table).where(table.c.storage_member_id==task['source_id'],table.c.user_id==user)
        matched=int(await conn.scalar(select(func.count()).select_from(table.join(s.objects,key==s.objects.c.logical_id)).where(owned,match)))
        if matched!=unit['object_count'] or int(await conn.scalar(live))!=unit['object_count']:
            raise TransferError('migration_commit_catalog_changed')
        return owned,table,key

    async def _catalog(self,conn,task,unit):
        owned,table,key=await self._source_catalog(conn,task,unit)
        identifiers=select(s.objects.c.logical_id).where(owned)
        result=await conn.execute(update(table).where(key.in_(identifiers),table.c.storage_member_id==task['source_id'])
            .values(storage_member_id=task['target_id']))
        if result.rowcount!=unit['object_count']:raise TransferError('migration_commit_catalog_changed')
        await conn.execute(update(s.objects).where(owned).values(state='published',updated_at=int(self.transfer.wall())))

    async def commit(self,lease,unit_id):
        identifier(unit_id,long=True)
        await self.transfer.journal.renew(lease,ttl=60)
        async with media_mutation_lock,self.store.database.begin() as conn:
            task=await self.transfer.journal._fenced(conn,lease)
            if task['state']!='committing':raise TransferError('migration_commit_requires_committing')
            unit=await self._unit(conn,task,unit_id)
            committed,remaining,members=await self._accounting(conn,task)
            if unit['state'] in COMMITTED:
                record=unit['publication'].get('commit')
                if not record or digest(record)!=unit['publication'].get('commit_sha256'):
                    raise TransferError('migration_commit_record_missing')
                return lease,dict(task_id=task['task_id'],unit_id=unit_id,commit=record,
                    commit_sha256=digest(record),cutover=True,native_acknowledged=False,source_files_removed=False)
            if unit['state']!='publishing':raise TransferError('migration_commit_unit_unavailable')
            now=int(self.transfer.wall());source,target=self._proofs(unit,lease,now)
            target_parent=await self._scopes(conn,task,unit)
            await self._catalog(conn,task,unit)
            amount=unit['total_bytes']
            if amount>remaining:raise TransferError('migration_commit_capacity_inconsistent')
            await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==task['source_id'])
                .values(used_bytes=members['source']['used_bytes']-amount,updated_at=now))
            await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==task['target_id'])
                .values(used_bytes=members['target']['used_bytes']+amount,
                    reserved_bytes=members['target']['reserved_bytes']-amount,updated_at=now))
            await conn.execute(update(s.capacity).where(s.capacity.c.task_id==task['task_id']).values(reserved_bytes=remaining-amount,updated_at=now))
            record=dict(format='frontiercloud-python-resource-unit-commit',version=1,task_id=task['task_id'],
                master_id=task['master_id'],source_id=task['source_id'],target_id=task['target_id'],
                source_relationship=task['source_relationship'],target_relationship=task['target_relationship'],
                plan_sha256=task['plan_sha256'],units_sha256=task['plan']['units_sha256'],epoch=lease.epoch,
                unit_id=unit_id,resource_kind=unit['resource_kind'],source_parent=unit['parent_path'],target_parent=target_parent,
                unit_version=unit['source_version'],object_count=unit['object_count'],total_bytes=amount,
                committed_at=now,cleanup_after=now+GRACE_SECONDS,source_receipt_sha256=source['receipt_sha256'],
                target_receipt_sha256=target['receipt_sha256'])
            publication={**unit['publication'],'commit':record,'commit_sha256':digest(record),'native_acknowledgements':{}}
            await conn.execute(update(s.units).where(s.units.c.task_id==task['task_id'],s.units.c.unit_id==unit_id)
                .values(state='committed',committed_at=now,cleanup_after=now+GRACE_SECONDS,publication=publication))
            await conn.execute(update(s.tasks).where(s.tasks.c.task_id==task['task_id'])
                .values(committed_bytes=committed+amount,revision=task['revision']+1,updated_at=now))
            await self.store.log(conn,'migration_parent_committed',lease.runner_id,task_id=task['task_id'],unit_id=unit_id,bytes=amount)
            renewed=Lease(lease.task_id,lease.runner_id,lease.epoch,task['revision']+1)
        return renewed,dict(task_id=task['task_id'],unit_id=unit_id,commit=record,commit_sha256=digest(record),
            cutover=True,native_acknowledged=False,source_files_removed=False)

    async def acknowledge_one(self,lease,unit_id):
        identifier(unit_id,long=True)
        await self.transfer.journal.renew(lease,ttl=60)
        async with self.store.database.begin() as conn:
            task=await self.transfer.journal._fenced(conn,lease)
            unit=await self._unit(conn,task,unit_id)
            if task['state'] not in {'committing','cleanup_pending','cancelling'} or unit['state'] not in COMMITTED:
                raise TransferError('migration_commit_not_committed')
            await self._accounting(conn,task)
            record=unit['publication'].get('commit');checksum=unit['publication'].get('commit_sha256')
            if not record or digest(record)!=checksum:raise TransferError('migration_commit_record_missing')
            acknowledgements=unit['publication'].get('native_acknowledgements',{})
            side=next((name for name in ('target','source') if name not in acknowledgements),None)
        if side is None:return dict(unit_id=unit_id,native_acknowledged=True,source_files_removed=False)
        relations=await self.transfer.relations(task);request_id=uuid.uuid4().hex
        grant=self.transfer._grant(task,relations,lease.epoch,lease.runner_id,'commit')
        response=await signed_request(self.store,relations[task[side+'_id']],self.transport,
            '/internal/v1/migration/transfer/commit-ack',dict(request_id=request_id,grant=grant,commit=record),
            limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_commit')
        expected=dict(format='frontiercloud-python-resource-commit-ack',version=1,request_id=request_id,node_id=task[side+'_id'],
            task_id=task['task_id'],unit_id=unit_id,unit_version=unit['source_version'],side=side,epoch=lease.epoch,
            commit_sha256=checksum,acknowledged=True,source_files_removed=False)
        if (not isinstance(response,dict) or response.keys()!={*expected,'capacity_reserved','committed_bytes'} or
                any(response.get(name)!=value for name,value in expected.items()) or
                any(type(response[name]) is not int for name in ('version','epoch','capacity_reserved','committed_bytes')) or
                response['acknowledged'] is not True or response['source_files_removed'] is not False or
                not unit['total_bytes']<=response['committed_bytes']<=task['committed_bytes'] or
                response['capacity_reserved']!=(task['total_bytes']-response['committed_bytes'] if side=='target' else 0)):
            raise TransferError('migration_commit_native_reply_invalid')
        async with self.store.database.begin() as conn:
            await self.transfer.journal._fenced(conn,lease)
            current=await self._unit(conn,task,unit_id)
            if current['state'] not in COMMITTED or current['publication']!=unit['publication']:
                raise TransferError('migration_commit_unit_changed')
            acknowledgements={**acknowledgements,side:response}
            await conn.execute(update(s.units).where(s.units.c.task_id==task['task_id'],s.units.c.unit_id==unit_id)
                .values(publication={**current['publication'],'native_acknowledgements':acknowledgements},
                    native_acknowledged=int(set(acknowledgements)=={'source','target'})))
            await self.store.log(conn,'migration_parent_commit_acknowledged',lease.runner_id,
                task_id=task['task_id'],unit_id=unit_id,side=side)
        return dict(unit_id=unit_id,side=side,native_acknowledged=len(acknowledgements)==2,source_files_removed=False)

    async def verify_target(self,lease,unit_id):
        from app.services.resource_target_verify import live_proof
        identifier(unit_id,long=True)
        await self.transfer.journal.renew(lease,ttl=60)
        async with self.store.database.begin() as conn:
            task=await self.transfer.journal._fenced(conn,lease)
            unit=await self._unit(conn,task,unit_id)
            if (task['state'] not in {'committing','cleanup_pending','cancelling'} or unit['state'] not in COMMITTED or
                    set(unit['publication'].get('native_acknowledgements',{}))!={'source','target'}):
                raise TransferError('migration_target_verify_requires_native_commit')
            await self._accounting(conn,task)
            commit=unit['publication'].get('commit')
            if not commit or digest(commit)!=unit['publication'].get('commit_sha256'):
                raise TransferError('migration_commit_record_missing')
        relations=await self.transfer.relations(task);request_id=uuid.uuid4().hex
        grant=self.transfer._grant(task,relations,lease.epoch,lease.runner_id,'inspect')
        response=await signed_request(self.store,relations[task['target_id']],self.transport,
            '/internal/v1/migration/transfer/verify-target',dict(request_id=request_id,grant=grant,unit_id=unit_id),
            limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_target_verify')
        expected=dict(format='frontiercloud-python-target-verification',version=1,request_id=request_id,node_id=task['target_id'],
            task_id=task['task_id'],unit_id=unit_id,unit_version=unit['source_version'],object_count=unit['object_count'],
            total_bytes=unit['total_bytes'],epoch=lease.epoch,expires_at=grant['payload']['expires_at'],source_files_removed=False)
        if (not isinstance(response,dict) or response.keys()!={*expected,'phase','processed_objects','proof'} or
                any(response.get(name)!=item for name,item in expected.items()) or
                any(type(response[name]) is not int for name in ('version','object_count','total_bytes','epoch','expires_at','processed_objects')) or
                response['source_files_removed'] is not False or self.transfer.wall()>=response['expires_at'] or
                not isinstance(response['phase'],str) or response['phase'] not in {'objects','membership','verified'} or
                not 0<=response['processed_objects']<=unit['object_count']):
            raise TransferError('migration_target_verify_reply_invalid')
        if response['phase']=='verified':
            if response['processed_objects']!=unit['object_count']:raise TransferError('migration_target_verify_reply_invalid')
            live_proof(response['proof'],grant['payload'],commit,self.transfer.wall())
        elif response['proof'] is not None:raise TransferError('migration_target_verify_reply_invalid')
        async with self.store.database.begin() as conn:
            await self.transfer.journal._fenced(conn,lease)
            current=await self._unit(conn,task,unit_id)
            if current['state']!=unit['state'] or current['publication']!=unit['publication']:
                raise TransferError('migration_target_verify_unit_changed')
            await conn.execute(update(s.units).where(s.units.c.task_id==task['task_id'],s.units.c.unit_id==unit_id)
                .values(publication={**current['publication'],'target_live_reply':response}))
            await self.store.log(conn,'migration_target_verification_step',lease.runner_id,
                task_id=task['task_id'],unit_id=unit_id,phase=response['phase'])
        return dict(unit_id=unit_id,phase=response['phase'],target_verified=response['phase']=='verified',source_files_removed=False)
