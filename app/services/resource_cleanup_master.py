"""Master authorization and progress for delayed, managed Source replica cleanup."""
import uuid

from sqlalchemy import and_,func,or_,select,update

from app.services import karaoke_schema as ks,media_crypto_schema as cs,resource_migration_schema as s
from app.services.federation import schema as fs
from app.services.resource_control_client import signed_request
from app.services.resource_cutover_master import COMMITTED,Cutover
from app.services.resource_migration_journal import Lease
from app.services.resource_observation import digest
from app.services.resource_target_verify import live_proof
from app.services.resource_transfer_protocol import CONTROL_BYTES,TransferError,identifier


class Cleanup:
    def __init__(self,store,transport,**options):
        self.cutover=Cutover(store,transport,**options);self.t=self.cutover.transfer
        self.store,self.transport=store,transport

    async def _task(self,conn,lease,unit_id):
        task=await self.t.journal._fenced(conn,lease)
        if task['state'] not in {'committing','cleanup_pending','cancelling'}:
            raise TransferError('migration_cleanup_task_unavailable')
        unit=await self.cutover._unit(conn,task,unit_id)
        if unit['state'] not in COMMITTED:raise TransferError('migration_cleanup_unit_not_committed')
        states=(*COMMITTED,'cancelled') if task['state']=='cancelling' else COMMITTED
        counts=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.units.c.object_count),0),
            func.coalesce(func.sum(s.units.c.total_bytes),0)).where(s.units.c.task_id==task['task_id'],
            s.units.c.state.in_(states),s.units.c.native_acknowledged==1))).one()
        if (tuple(counts)!=(task['plan']['unit_count'],task['object_count'],task['total_bytes']) or
                task['state']!='cancelling' and task['committed_bytes']!=task['total_bytes'] or
                set(unit['publication'].get('native_acknowledgements',{}))!={'source','target'}):
            raise TransferError('migration_cleanup_all_native_acknowledgements_required')
        await self.cutover._accounting(conn,task);await self.cutover._scopes(conn,task,unit)
        commit=unit['publication'].get('commit')
        if not commit or digest(commit)!=unit['publication'].get('commit_sha256'):
            raise TransferError('migration_cleanup_commit_changed')
        return task,unit

    async def _catalog(self,conn,task,unit):
        owned=and_(s.objects.c.task_id==task['task_id'],s.objects.c.unit_id==unit['unit_id'])
        counts=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.objects.c.expected_bytes),0)).where(owned,
            s.objects.c.resource_kind==unit['resource_kind'],s.objects.c.state.in_(['published','cleanup_pending','cleaned']),
            s.objects.c.verified_bytes==s.objects.c.expected_bytes,s.objects.c.source_sha256!='',
            s.objects.c.target_sha256==s.objects.c.source_sha256,s.objects.c.target_object==s.objects.c.source_object))).one()
        total=int(await conn.scalar(select(func.count()).select_from(s.objects).where(owned)))
        if tuple(counts)!=(unit['object_count'],unit['total_bytes']) or total!=unit['object_count']:
            raise TransferError('migration_cleanup_master_roster_changed')
        if unit['resource_kind']=='media':
            table=fs.global_media;key=table.c.media_id
            present=select(cs.encryption.c.object_id).where(cs.encryption.c.object_kind=='media',cs.encryption.c.object_id==s.objects.c.logical_id)
            matching=present.where(func.hex(cs.encryption.c.descriptor_json)==func.hex(s.objects.c.catalog_encryption_json),
                cs.encryption.c.file_id==s.objects.c.catalog_encryption_file_id)
            match=and_(table.c.storage_member_id==task['target_id'],table.c.object_id==s.objects.c.target_object,
                table.c.media_path==s.objects.c.physical_path,table.c.path_locator==s.objects.c.catalog_path_locator,
                table.c.object_kind==s.objects.c.catalog_media_kind,table.c.size_bytes==s.objects.c.expected_bytes,
                table.c.etag==s.objects.c.catalog_etag,table.c.state=='active',
                or_(and_(s.objects.c.catalog_encryption_json=='',~present.exists()),
                    and_(s.objects.c.catalog_encryption_json!='',matching.exists())))
            live=select(func.count()).select_from(table).where(table.c.media_path.startswith(unit['parent_path']+'/',autoescape=True))
        else:
            table=ks.recordings;key=table.c.recording_id;user=unit['parent_path'].rsplit('/',1)[-1]
            match=and_(table.c.storage_member_id==task['target_id'],table.c.user_id==s.objects.c.catalog_user_id,
                table.c.user_id==user,table.c.recording_id==s.objects.c.target_object,table.c.state=='ready',
                table.c.size_bytes==s.objects.c.expected_bytes,table.c.sha256==s.objects.c.source_sha256)
            live=select(func.count()).select_from(table).where(table.c.storage_member_id==task['target_id'],table.c.user_id==user)
        matched=int(await conn.scalar(select(func.count()).select_from(table.join(s.objects,key==s.objects.c.logical_id)).where(owned,match)))
        if matched!=unit['object_count'] or int(await conn.scalar(live))!=unit['object_count']:
            raise TransferError('migration_cleanup_master_catalog_changed')

    async def advance(self,lease,unit_id):
        identifier(unit_id,long=True);await self.t.journal.renew(lease,ttl=60)
        async with self.store.database.begin() as conn:
            task,unit=await self._task(conn,lease,unit_id);await self._catalog(conn,task,unit)
        now=self.t.wall();commit=unit['publication']['commit']
        if (type(unit['cleanup_after']) is not int or unit['cleanup_after']!=commit['cleanup_after'] or
                unit['committed_at']!=commit['committed_at'] or unit['cleanup_after']-unit['committed_at']!=3600):
            raise TransferError('migration_cleanup_grace_record_changed')
        if now<unit['cleanup_after']:
            return lease,dict(unit_id=unit_id,phase='waiting_grace',cleanup_after=unit['cleanup_after'],source_cleaned=False)
        relations=await self.t.relations(task)
        grant=self.t._grant(task,relations,lease.epoch,lease.runner_id,'cleanup')
        target=unit['publication'].get('target_live_reply',{}).get('proof')
        refresh=target is None or target.get('payload',{}).get('expires_at',0)<=now
        if not refresh:
            old_epoch=target.get('payload',{}).get('epoch')
            if type(old_epoch) is int and 0<old_epoch<lease.epoch:
                # The retained proof must be authentic before replacing it for
                # this runner. A new epoch cannot reuse the old authority.
                live_proof(target,{**grant['payload'],'epoch':old_epoch},commit,now)
                refresh=True
        if refresh:
            result=await self.cutover.verify_target(lease,unit_id)
            return lease,dict(unit_id=unit_id,phase='target_ready' if result['target_verified'] else 'verifying_target',source_cleaned=False)
        live_proof(target,grant['payload'],commit,now)
        request_id=uuid.uuid4().hex
        response=await signed_request(self.store,relations[task['source_id']],self.transport,
            '/internal/v1/migration/transfer/cleanup-source',dict(request_id=request_id,grant=grant,unit_id=unit_id,target_proof=target),
            limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_cleanup')
        expected=dict(format='frontiercloud-python-source-cleanup',version=1,request_id=request_id,node_id=task['source_id'],
            task_id=task['task_id'],unit_id=unit_id,unit_version=unit['source_version'],commit_sha256=digest(commit),
            epoch=lease.epoch,expires_at=grant['payload']['expires_at'],object_count=unit['object_count'],total_bytes=unit['total_bytes'],
            native_usage_reconciled=False)
        previous=unit['publication'].get('source_cleanup_reply',{})
        if (not isinstance(response,dict) or response.keys()!={*expected,'phase','deleted_count','deleted_bytes'} or
                any(response.get(name)!=item for name,item in expected.items()) or
                any(type(response[name]) is not int for name in ('version','epoch','expires_at','object_count','total_bytes','deleted_count','deleted_bytes')) or
                response['native_usage_reconciled'] is not False or self.t.wall()>=response['expires_at'] or
                not isinstance(response['phase'],str) or response['phase'] not in {'verifying','ready','waiting_reader','waiting_low_traffic','cleaning','cleaned'} or
                not previous.get('deleted_count',0)<=response['deleted_count']<=unit['object_count'] or
                not previous.get('deleted_bytes',0)<=response['deleted_bytes']<=unit['total_bytes'] or
                response['phase']=='cleaned' and (response['deleted_count'],response['deleted_bytes'])!=(unit['object_count'],unit['total_bytes'])):
            raise TransferError('migration_cleanup_source_reply_invalid')
        async with self.store.database.begin() as conn:
            current_task,current=await self._task(conn,lease,unit_id);await self._catalog(conn,current_task,current)
            if current['publication']!=unit['publication']:raise TransferError('migration_cleanup_unit_changed')
            complete=response['phase']=='cleaned'
            await conn.execute(update(s.units).where(s.units.c.task_id==task['task_id'],s.units.c.unit_id==unit_id)
                .values(state='cleaned' if complete else 'cleanup_pending',publication={**current['publication'],'source_cleanup_reply':response}))
            if complete:
                await conn.execute(update(s.objects).where(s.objects.c.task_id==task['task_id'],s.objects.c.unit_id==unit_id).values(state='cleaned'))
            await self.store.log(conn,'migration_source_cleanup_step',lease.runner_id,task_id=task['task_id'],unit_id=unit_id,
                phase=response['phase'],deleted_bytes=response['deleted_bytes'])
            if current_task['state']=='committing':
                revision=current_task['revision']+1
                await conn.execute(update(s.tasks).where(s.tasks.c.task_id==task['task_id'])
                    .values(state='cleanup_pending',revision=revision,updated_at=int(self.t.wall())))
                lease=Lease(lease.task_id,lease.runner_id,lease.epoch,revision)
        return lease,dict(unit_id=unit_id,phase=response['phase'],source_cleaned=complete,deleted_count=response['deleted_count'],
            deleted_bytes=response['deleted_bytes'],task_completed=False)
