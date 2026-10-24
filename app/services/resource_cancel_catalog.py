"""Bounded Master/Source current-catalog agreement, without release authority."""
import json

from sqlalchemy import func,select,update

from app.services import karaoke_schema as ks,media_crypto,media_crypto_schema as cs,resource_migration_schema as s
from app.services.federation import schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_cancel_census_client import CensusClient
from app.services.resource_fences import lock_node
from app.services.resource_observation import digest
from app.services.resource_encryption_binding import bind_source,source_digest
from app.services.resource_pool import validate_media_path
from app.services.resource_transfer_protocol import TransferError,identifier

MARKER='cancel_current_census'


class CurrentCatalog:
    def __init__(self,store,transport,**options):
        self.client=CensusClient(store,transport,**options);self.t=self.client.t;self.store=store

    async def _task(self,conn,lease):
        await lock_node(conn)
        task=await self.t.journal._fenced(conn,lease)
        counts=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.units.c.object_count),0),
            func.coalesce(func.sum(s.units.c.total_bytes),0)).where(s.units.c.task_id==lease.task_id,
            s.units.c.state.in_(['cleaned','cancelled']),s.units.c.native_acknowledged==1))).one()
        if task['state']!='cancelling' or tuple(counts)!=(task['plan']['unit_count'],task['object_count'],task['total_bytes']):
            raise TransferError('migration_cancel_catalog_units_incomplete')
        await self.t.relations(task,conn=conn)
        if not await conn.scalar(select(s.drains.c.owner_id).where(s.drains.c.member_id==task['source_id'],
                s.drains.c.owner_id==lease.task_id,s.drains.c.reason=='migration')):
            raise TransferError('migration_cancel_catalog_drain_missing')
        if await conn.scalar(select(fs.upload_sessions.c.upload_id).where(fs.upload_sessions.c.storage_member_id==task['source_id'],
                fs.upload_sessions.c.state=='reserved').limit(1)):
            raise TransferError('migration_cancel_catalog_writer_pending')
        return task

    def _progress(self,task,lease):
        if MARKER not in task['plan']:
            return self._new(lease)
        progress=task['plan'][MARKER]
        keys={'version','epoch','runner_id','phase','unit_after','unit_id','object_after','processed_objects',
              'processed_bytes','retained_objects','retained_bytes','catalog_sha256','source_proof'}
        if (not isinstance(progress,dict) or progress.keys()!=keys or type(progress['version']) is not int or progress['version']!=1
                or type(progress['epoch']) is not int or progress['epoch']<1 or not isinstance(progress['phase'],str)
                or progress['phase'] not in {'catalog','ready','verified'}
                or any(type(progress[name]) is not int or progress[name]<0 for name in
                    ('processed_objects','processed_bytes','retained_objects','retained_bytes'))
                or not progress['retained_objects']<=progress['processed_objects']<=task['object_count']
                or not progress['retained_bytes']<=progress['processed_bytes']<=task['total_bytes']
                or progress['source_proof'] is not None and not isinstance(progress['source_proof'],dict)):
            raise TransferError('migration_cancel_catalog_progress_changed')
        identifier(progress['runner_id']);identifier(progress['catalog_sha256'],long=True)
        for name in ('unit_after','unit_id','object_after'):
            value=progress[name]
            if value!='':identifier(value,long=not (name=='object_after' and isinstance(value,str) and len(value)==32))
        if (progress['epoch'],progress['runner_id'])!=(lease.epoch,lease.runner_id):return self._new(lease)
        return progress

    @staticmethod
    def _new(lease):
        return dict(version=1,epoch=lease.epoch,runner_id=lease.runner_id,phase='catalog',unit_after='',unit_id='',object_after='',
            processed_objects=0,processed_bytes=0,retained_objects=0,retained_bytes=0,
            catalog_sha256=digest(['cancel-current-catalog',1]),source_proof=None)

    async def _save(self,conn,task,progress,lease):
        await conn.execute(update(s.tasks).where(s.tasks.c.task_id==lease.task_id).values(
            plan={**task['plan'],MARKER:progress},updated_at=int(self.t.wall())))
        await self.store.log(conn,'migration_cancel_catalog_step',lease.runner_id,task_id=lease.task_id,
            phase=progress['phase'],processed_objects=progress['processed_objects'],retained_objects=progress['retained_objects'])

    async def _object(self,conn,task,unit,obj):
        if obj['resource_kind']!=unit['resource_kind']:raise TransferError('migration_cancel_catalog_object_changed')
        if obj['resource_kind']=='media':
            row=(await conn.execute(select(fs.global_media).where(fs.global_media.c.media_id==obj['logical_id']))).mappings().first()
            encoded=(await conn.execute(select(cs.encryption).where(cs.encryption.c.object_kind=='media',
                cs.encryption.c.object_id==obj['logical_id']))).mappings().first()
            if row is None:
                if encoded is not None and encoded['descriptor_json'] is not None:
                    raise TransferError('migration_cancel_catalog_orphan_encryption')
                return None
            if unit['state']=='cleaned':
                if row['storage_member_id']==task['source_id']:raise TransferError('migration_cancel_catalog_cleaned_owner_changed')
                return None
            path,kind=validate_media_path(row['media_path']);encryption=None
            if (row['storage_member_id']!=task['source_id'] or row['object_id']!=obj['source_object'] or row['state']!='active'
                    or row['size_bytes']!=obj['expected_bytes'] or row['object_kind']!=kind or row['path_locator']!=digest_path(path)):
                raise TransferError('migration_cancel_catalog_object_changed')
            if encoded is not None:
                try:
                    encryption=media_crypto.Metadata.from_dict(json.loads(encoded['descriptor_json'])).to_dict()
                    if encryption['file_id']!=encoded['file_id']:raise ValueError()
                except (ValueError,TypeError,KeyError):raise TransferError('migration_cancel_catalog_encryption_changed') from None
            try:original=json.loads(obj['catalog_encryption_json']) if obj['catalog_encryption_json'] else None
            except (ValueError,TypeError):raise TransferError('migration_cancel_catalog_encryption_changed') from None
            if encryption!=original:raise TransferError('migration_cancel_catalog_encryption_changed')
            binding=await conn.scalar(select(s.plan_bindings.c.object_json).where(s.plan_bindings.c.plan_id==task['task_id'],
                s.plan_bindings.c.resource_kind=='media',s.plan_bindings.c.logical_id==obj['logical_id']))
            try:
                snapshot=json.loads(binding);source=snapshot['source_proof']['result']
                if (snapshot['encryption']!=original or source['source_version']!=obj['source_version'] or
                        row['etag']!=obj['catalog_etag'] or source['encryption_sha256']!=source_digest(snapshot)):
                    raise ValueError()
                native_encryption=source_digest(bind_source(snapshot,source['encryption_sha256']))
            except (ValueError,TypeError,KeyError):
                raise TransferError('migration_cancel_catalog_encryption_changed') from None
            user=''
        else:
            row=(await conn.execute(select(ks.recordings).where(ks.recordings.c.recording_id==obj['logical_id']))).mappings().first()
            if row is None:return None
            if unit['state']=='cleaned':
                if row['storage_member_id']==task['source_id']:raise TransferError('migration_cancel_catalog_cleaned_owner_changed')
                return None
            user=row['user_id'];kind='recording';encryption=None
            if (row['storage_member_id']!=task['source_id'] or row['recording_id']!=obj['source_object'] or row['state']!='ready'
                    or row['size_bytes']!=obj['expected_bytes'] or user!=obj['catalog_user_id']):
                raise TransferError('migration_cancel_catalog_object_changed')
            path=task['source_relationship']+'/'+user+'/'+row['recording_id']+'.bin'
            native_encryption=digest(None)
        return dict(logical_id=obj['logical_id'],resource_kind=obj['resource_kind'],object_id=obj['source_object'],
            physical_path=path,bytes=row['size_bytes'],object_kind=kind,user_id=user,encryption_sha256=native_encryption)

    async def _page(self,conn,task,progress):
        unit_id=progress['unit_id'] or await conn.scalar(select(s.units.c.unit_id).where(s.units.c.task_id==task['task_id'],
            s.units.c.unit_id>progress['unit_after']).order_by(s.units.c.unit_id).limit(1))
        if unit_id is None:
            if (progress['processed_objects'],progress['processed_bytes'])!=(task['object_count'],task['total_bytes']):
                raise TransferError('migration_cancel_catalog_roster_incomplete')
            return {**progress,'phase':'ready'}
        unit=(await conn.execute(select(s.units).where(s.units.c.task_id==task['task_id'],s.units.c.unit_id==unit_id))).mappings().one()
        rows=(await conn.execute(select(s.objects).where(s.objects.c.task_id==task['task_id'],s.objects.c.unit_id==unit_id,
            s.objects.c.logical_id>progress['object_after']).order_by(s.objects.c.logical_id).limit(16))).mappings().all()
        progress={**progress,'unit_id':unit_id}
        for obj in rows:
            catalog=await self._object(conn,task,unit,obj)
            progress={**progress,'object_after':obj['logical_id'],'processed_objects':progress['processed_objects']+1,
                'processed_bytes':progress['processed_bytes']+obj['expected_bytes']}
            if catalog is not None:
                progress={**progress,'retained_objects':progress['retained_objects']+1,
                    'retained_bytes':progress['retained_bytes']+catalog['bytes'],'catalog_sha256':digest([progress['catalog_sha256'],catalog])}
        return progress if rows else {**progress,'unit_after':unit_id,'unit_id':'','object_after':''}

    async def _owned(self,conn,task,progress):
        count=total=0
        for table in (fs.global_media,ks.recordings):
            result=(await conn.execute(select(func.count(),func.coalesce(func.sum(table.c.size_bytes),0)).where(
                table.c.storage_member_id==task['source_id']))).one()
            count+=result[0];total+=result[1]
        if (count,total)!=(progress['retained_objects'],progress['retained_bytes']):
            raise TransferError('migration_cancel_catalog_current_owner_mismatch')

    async def advance(self,lease):
        await self.t.journal.renew(lease,ttl=60)
        if self.t.journal.traffic.snapshot()['can_run'] is not True:raise TransferError('migration_cancel_catalog_waiting_low_traffic')
        async with media_mutation_lock,self.store.database.begin() as conn:
            task=await self._task(conn,lease);progress=self._progress(task,lease)
            if progress['phase']=='catalog':
                progress=await self._page(conn,task,progress)
                await self._save(conn,task,progress,lease)
                return self._reply(task,progress,False)
            await self._owned(conn,task,progress)
        reply=await self.client.query(lease)
        if reply['phase']!='verified':return self._reply(task,progress,False,source_phase=reply['phase'])
        proof=reply['proof']['payload']
        async with media_mutation_lock,self.store.database.begin() as conn:
            current=await self._task(conn,lease)
            if self._progress(current,lease)!=progress:raise TransferError('migration_cancel_catalog_progress_changed')
            await self._owned(conn,current,progress)
            if (proof['object_count'],proof['total_bytes'],proof['catalog_sha256'])!=(
                    progress['retained_objects'],progress['retained_bytes'],progress['catalog_sha256']):
                raise TransferError('migration_cancel_catalog_source_mismatch')
            if self.t.wall()>=proof['expires_at']:raise TransferError('migration_cancel_catalog_source_proof_expired')
            progress={**progress,'phase':'verified','source_proof':reply['proof']}
            await self._save(conn,current,progress,lease)
        return self._reply(task,progress,True)

    async def authorize(self,lease):
        from app.services.resource_cancel_agreement import FORMAT,IDENTITIES,verify
        from app.services.federation import protocol as p
        from app.services.resource_cancel_census import verify_proof
        await self.t.journal.renew(lease,ttl=60)
        if self.t.journal.traffic.snapshot()['can_run'] is not True:raise TransferError('migration_cancel_catalog_waiting_low_traffic')
        async with media_mutation_lock,self.store.database.begin() as conn:
            task=await self._task(conn,lease);progress=self._progress(task,lease)
            if progress['phase']!='verified' or progress['source_proof'] is None:
                raise TransferError('migration_cancel_catalog_agreement_required')
            await self._owned(conn,task,progress)
            relations=await self.t.relations(task,conn=conn)
            grant=self.t._grant(task,relations,lease.epoch,lease.runner_id,'finalize');value=grant['payload']
            proof=verify_proof(progress['source_proof'],value,self.t.wall())
            if (proof['object_count'],proof['total_bytes'],proof['catalog_sha256'])!=(
                    progress['retained_objects'],progress['retained_bytes'],progress['catalog_sha256']):
                raise TransferError('migration_cancel_catalog_source_mismatch')
            committed=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.units.c.object_count),0),
                func.coalesce(func.sum(s.units.c.total_bytes),0)).where(s.units.c.task_id==lease.task_id,s.units.c.state=='cleaned'))).one()
            if committed[2]!=task['committed_bytes']:raise TransferError('migration_cancel_catalog_committed_bytes_changed')
            private=self.store.unseal(self.store.node['private_key']);now=int(self.t.wall())
            agreement=p.sign(private,dict(format=FORMAT,version=1,purpose='release-retained-source',
                **{name:value[name] for name in IDENTITIES},epoch=lease.epoch,runner_id=lease.runner_id,
                unit_count=task['plan']['unit_count'],object_count=task['object_count'],total_bytes=task['total_bytes'],
                committed_units=int(committed[0]),committed_objects=int(committed[1]),committed_bytes=int(committed[2]),
                retained_objects=progress['retained_objects'],retained_bytes=progress['retained_bytes'],
                census_sha256=digest(progress['source_proof']),catalog_sha256=proof['catalog_sha256'],roots_sha256=proof['roots_sha256'],
                master_state_sha256=digest(progress),issued_at=now,expires_at=min(proof['expires_at'],value['expires_at']),
                source_files_removed=False,ownership_changed=False,task_completed=False))
            verify(agreement,p.public_key(private),value,(task['plan']['unit_count'],task['object_count'],task['total_bytes']),
                self.t.wall(),source_census=progress['source_proof'])
            await self.store.log(conn,'migration_cancel_catalog_authorized',lease.runner_id,task_id=lease.task_id,
                agreement_sha256=digest(agreement),census_sha256=digest(progress['source_proof']))
        return dict(grant=grant,agreement=agreement,source_census=progress['source_proof'])

    @staticmethod
    def _reply(task,progress,verified,**extra):
        return dict(task_id=task['task_id'],phase=progress['phase'],processed_objects=progress['processed_objects'],
            retained_objects=progress['retained_objects'],retained_bytes=progress['retained_bytes'],master_catalog_verified=verified,
            native_usage_reconciled=False,source_drain_released=False,task_completed=False,**extra)


def digest_path(path):
    import hashlib
    return hashlib.sha256(path.encode()).hexdigest()
