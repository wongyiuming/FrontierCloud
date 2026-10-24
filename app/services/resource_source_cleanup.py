"""Journaled removal of verified Source replicas after the real cleanup grace.

This is per-unit cleanup, not task completion or node retirement. Native usage
is reconciled only by the eventual complete Source inventory, never clamped
from a possibly stale cache while individual replicas are being removed.
"""
from sqlalchemy import func,insert,select,text,update

from app.core.joined_io import joined_io
from app.services import media_crypto,media_crypto_schema as cs,resource_cleanup_files as files,resource_migration_schema as s
from app.services.federation import schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_commit_peers import CommitPeers
from app.services.resource_observation import digest
from app.services.resource_source_verify import SourceScan,SourceVerification
from app.services.resource_target_verify import live_proof
from app.services.resource_transfer_protocol import TransferError,identifier


class CleanupScan(SourceScan):
    def __init__(self,root,parent,key,now,proof):
        super().__init__(root,parent,key,now)
        try:
            if any(self.root[name]!=proof['root'][name] or self.directory[name]!=proof['directory'][name] for name in ('device','inode','mode')):
                raise TransferError('migration_cleanup_source_directory_changed')
        except BaseException:self.close();raise
        self.live_count=0;self.live_bytes=0;self.deleted_count=0;self.deleted_bytes=0;self.missing=''


class SourceCleanup:
    def __init__(self,transfers):
        self.t,self.store=transfers,transfers.store;self.verifier=SourceVerification(transfers)
        self.deadline=None

    def _alive(self,value):
        self.t._alive(value)
        if self.deadline is not None and self.t.wall()>=self.deadline:
            raise TransferError('migration_cleanup_target_proof_expired')

    async def _unit(self,conn,value,unit_id):
        self._alive(value)
        header=await self.t._control_row(conn,value)
        unit=(await conn.execute(select(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit_id)
            .with_for_update())).mappings().first()
        if (not unit or header['state']!='active' or header['side']!='source' or
                unit['state'] not in {'committed','cleanup_pending','cleaned'}):
            raise TransferError('migration_cleanup_native_commit_required')
        commit=unit['publication'].get('commit')
        if (not commit or CommitPeers(self.t)._record(commit,value)!=unit['publication'].get('commit_sha256') or
                any(commit[name]!=unit[field] for name,field in (('unit_id','unit_id'),('unit_version','source_version'),
                    ('resource_kind','resource_kind'),('source_parent','parent_path'),('object_count','object_count'),
                    ('total_bytes','total_bytes'),('committed_at','committed_at'),('cleanup_after','cleanup_after')))):
            raise TransferError('migration_cleanup_native_commit_changed')
        if self.t.wall()<commit['cleanup_after']:raise TransferError('migration_cleanup_grace_pending')
        states=s.units.c.state.in_(['committed','cleanup_pending','cleaned'])
        if header['payload'].get('transfer',{}).get('cancel_started') is True:
            states=states|((s.units.c.state=='cancelled')&(s.units.c.native_acknowledged==1))
        counts=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.units.c.object_count),0),
            func.coalesce(func.sum(s.units.c.total_bytes),0)).where(s.units.c.task_id==value['task_id'],
            states))).one()
        if tuple(counts)!=(header['expected_units'],header['expected_objects'],header['expected_bytes']):
            raise TransferError('migration_cleanup_all_native_commits_required')
        if (not await conn.scalar(select(s.scopes.c.scope_id).where(s.scopes.c.task_id==value['task_id'],
                s.scopes.c.member_id==value['source_id'],s.scopes.c.resource_kind==unit['resource_kind'],s.scopes.c.parent_path==unit['parent_path'])) or
                not await conn.scalar(select(s.drains.c.owner_id).where(s.drains.c.member_id==value['source_id'],
                    s.drains.c.owner_id==value['task_id'],s.drains.c.reason=='migration'))):
            raise TransferError('migration_cleanup_source_fence_missing')
        proof=unit['publication'].get('source_verification')
        if not proof or digest(proof)!=commit['source_receipt_sha256']:
            raise TransferError('migration_cleanup_source_proof_changed')
        return dict(unit)

    async def _entry(self,conn,value,unit,obj):
        row=(await conn.execute(select(s.storage_journal).where(s.storage_journal.c.task_id==value['task_id'],
            s.storage_journal.c.logical_id==obj['logical_id']).with_for_update())).mappings().first()
        if row is None:return None
        expected=dict(unit_id=unit['unit_id'],master_id=value['master_id'],source_id=value['source_id'],target_id=value['target_id'],
            resource_kind=obj['resource_kind'],object_id=obj['source_object'],physical_path=obj['source_path'],
            source_version=obj['source_version'],expected_bytes=obj['expected_bytes'],sha256=obj['sha256'])
        if (any(row[name]!=item for name,item in expected.items()) or row['state'] not in {'deleting','deleted'} or
                row['publication'].get('action')!='source-cleanup' or
                row['publication'].get('commit_sha256')!=unit['publication']['commit_sha256'] or
                digest(row['publication'].get('native'))!=row['cleanup_version']):
            raise TransferError('migration_cleanup_journal_changed')
        return dict(row)

    async def _counts(self,conn,value,unit):
        rows=(await conn.execute(select(s.storage_journal.c.state,func.count(),func.coalesce(func.sum(s.storage_journal.c.expected_bytes),0))
            .where(s.storage_journal.c.task_id==value['task_id'],s.storage_journal.c.unit_id==unit['unit_id'])
            .group_by(s.storage_journal.c.state))).all()
        states={state:(int(count),int(size)) for state,count,size in rows}
        if set(states)-{'deleting','deleted'} or states.get('deleting',(0,0))[0]>1:
            raise TransferError('migration_cleanup_journal_inconsistent')
        return states.get('deleted',(0,0))

    async def _metadata(self,conn,obj,*,deleted=False):
        if obj['resource_kind']!='media':return
        if deleted:
            if (await conn.scalar(text('SELECT media_id FROM media_objects WHERE media_id=:id'),dict(id=obj['source_object'])) or
                    await conn.scalar(select(cs.encryption.c.object_id).where(cs.encryption.c.object_kind=='media',
                        cs.encryption.c.object_id==obj['source_object'],cs.encryption.c.descriptor_json.is_not(None)))):
                raise TransferError('migration_cleanup_deleted_registry_reappeared')
        else:
            path,metadata,encryption=await self.t.observer.media_snapshot(obj['source_object'],conn=conn)
            if (path!=obj['source_path'] or digest(metadata)!=obj['metadata_sha256'] or digest(encryption)!=obj['encryption_sha256']):
                raise TransferError('migration_cleanup_native_metadata_changed')

    async def _save(self,value,unit,cleanup,*,state=None,action='migration_source_cleanup_progress'):
        async with media_mutation_lock,self.store.database.begin() as conn:
            current=await self._unit(conn,value,unit['unit_id'])
            if current['state']!=unit['state'] or current['publication']!=unit['publication']:
                raise TransferError('migration_cleanup_unit_changed')
            if await self._counts(conn,value,current)!=(cleanup['deleted_count'],cleanup['deleted_bytes']):
                raise TransferError('migration_cleanup_journal_inconsistent')
            await conn.execute(update(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit['unit_id'])
                .values(state=state or unit['state'],publication={**current['publication'],'cleanup':cleanup}))
            await self.store.log(conn,action,value['master_id'],task_id=value['task_id'],unit_id=unit['unit_id'],
                deleted_count=cleanup['deleted_count'],deleted_bytes=cleanup['deleted_bytes'])
        return {**unit,'state':state or unit['state'],'publication':{**unit['publication'],'cleanup':cleanup}}

    def _reply(self,value,request_id,unit,phase):
        cleanup=unit['publication'].get('cleanup',{})
        return dict(format='frontiercloud-python-source-cleanup',version=1,request_id=request_id,node_id=value['source_id'],
            task_id=value['task_id'],unit_id=unit['unit_id'],unit_version=unit['source_version'],commit_sha256=unit['publication']['commit_sha256'],
            epoch=value['epoch'],expires_at=value['expires_at'],phase=phase,object_count=unit['object_count'],total_bytes=unit['total_bytes'],
            deleted_count=cleanup.get('deleted_count',0),deleted_bytes=cleanup.get('deleted_bytes',0),native_usage_reconciled=False)

    async def _scan(self,value,relation,unit):
        cleanup=unit['publication']['cleanup'];proof=unit['publication']['source_verification']
        key=(value['task_id'],unit['unit_id'],value['epoch'],value['runner_id'])
        scan=self.t.source_cleanup_scan
        if scan is not None and (scan.key!=key or self.t.wall()>=scan.deadline):
            await joined_io(scan.close);scan=None;self.t.source_cleanup_scan=None
        root=self.t.root_provider()['media' if unit['resource_kind']=='media' else 'recordings']
        if scan is None:
            # Directory ctime can legitimately change when the owner preserves
            # an unknown entry elsewhere. Verify the complete current roster
            # and each original file version, rather than trusting old ctime.
            scan=await joined_io(CleanupScan,root,unit['parent_path'],key,self.t.wall(),proof,on_cancel=lambda item:item.close())
            self.t.source_cleanup_scan=scan
        await joined_io(scan.unchanged)
        if scan.phase=='objects':
            async with self.store.database.begin() as conn:
                await self._unit(conn,value,unit['unit_id'])
                rows=(await conn.execute(select(s.peer_bindings).where(s.peer_bindings.c.task_id==value['task_id'],
                    s.peer_bindings.c.unit_id==unit['unit_id'],s.peer_bindings.c.logical_id>scan.after)
                    .order_by(s.peer_bindings.c.logical_id).limit(8))).mappings().all()
            from app.services.resource_migration_planning import extend
            for row in rows:
                obj=self.verifier._object(row,value,unit)
                async with self.store.database.begin() as conn:
                    entry=await self._entry(conn,value,unit,obj)
                    await self._metadata(conn,obj,deleted=entry is not None and entry['state']=='deleted')
                observed=await joined_io(files.inspect,root,unit['parent_path'],obj['source_path'].rsplit('/',1)[-1],proof)
                if entry and entry['state']=='deleted':
                    if observed['file'] is not None:raise TransferError('migration_cleanup_source_file_reappeared')
                    scan.deleted_count+=1;scan.deleted_bytes+=obj['expected_bytes']
                elif observed['file'] is None:
                    if not entry or entry['state']!='deleting' or scan.missing:
                        raise TransferError('migration_cleanup_source_missing_without_intent')
                    scan.missing=obj['logical_id']
                else:
                    if await self.verifier._identity(value,relation,obj)!=observed['file']:
                        raise TransferError('migration_cleanup_source_file_changed')
                    if entry and entry['publication']['native']!=observed['file']:
                        raise TransferError('migration_cleanup_source_file_changed')
                    scan.live_count+=1;scan.live_bytes+=obj['expected_bytes']
                extend(scan.checksum,dict(logical_id=obj['logical_id'],source_object=obj['source_object'],
                    source_sha256=obj['sha256'],source_version=obj['source_version']))
                scan.after=obj['logical_id'];scan.count+=1;scan.bytes+=obj['expected_bytes'];self._alive(value)
            if not rows:
                if (scan.count,scan.bytes,scan.checksum.hexdigest())!=(unit['object_count'],unit['total_bytes'],unit['source_version']):
                    raise TransferError('migration_cleanup_roster_changed')
                scan.phase='membership'
        else:
            entries=await joined_io(scan.page)
            for item in entries:
                async with self.store.database.begin() as conn:
                    await self._unit(conn,value,unit['unit_id'])
                    row=(await conn.execute(select(s.peer_bindings).where(s.peer_bindings.c.task_id==value['task_id'],
                        s.peer_bindings.c.unit_id==unit['unit_id'],s.peer_bindings.c.source_path==unit['parent_path']+'/'+item['name']))).mappings().first()
                    if not row:raise TransferError('migration_cleanup_unknown_source_file')
                    obj=self.verifier._object(row,value,unit);entry=await self._entry(conn,value,unit,obj)
                    if entry and entry['state']=='deleted':raise TransferError('migration_cleanup_source_file_reappeared')
                if await self.verifier._identity(value,relation,obj)!=item['native']:
                    raise TransferError('migration_cleanup_source_file_changed')
            if scan.scanned_count>scan.live_count or scan.scanned_bytes>scan.live_bytes:
                raise TransferError('migration_cleanup_roster_changed')
            if scan.finished:
                if (scan.scanned_count,scan.scanned_bytes)!=(scan.live_count,scan.live_bytes):
                    raise TransferError('migration_cleanup_roster_changed')
                await joined_io(scan.unchanged);self._alive(value)
                cleanup={**cleanup,'verified':True,'final_verified':scan.deleted_count==unit['object_count'],
                    'directory_version':digest(scan.directory),'reconciled_missing':scan.missing,'verified_at':int(self.t.wall())}
                unit=await self._save(value,unit,cleanup)
                await joined_io(scan.close);self.t.source_cleanup_scan=None
        return unit

    async def _clear(self,conn,value):
        traffic=self.t.traffic.snapshot()
        if traffic.get('can_run') is not True:return 'waiting_low_traffic'
        if traffic.get('readers_clear') is not True:return 'waiting_reader'
        if await conn.scalar(select(s.read_leases.c.lease_id).where(s.read_leases.c.member_id==value['source_id'],
                s.read_leases.c.expires_at>self.t.wall()).limit(1)):
            return 'waiting_reader'
        return ''

    async def _remove(self,value,relation,unit):
        cleanup=unit['publication']['cleanup'];proof=unit['publication']['source_verification']
        async with media_mutation_lock,self.store.database.begin() as conn:
            await self._unit(conn,value,unit['unit_id']);waiting=await self._clear(conn,value)
            if waiting:return unit,waiting
            row=(await conn.execute(select(s.peer_bindings).outerjoin(s.storage_journal,
                (s.peer_bindings.c.task_id==s.storage_journal.c.task_id)&(s.peer_bindings.c.logical_id==s.storage_journal.c.logical_id))
                .where(s.peer_bindings.c.task_id==value['task_id'],s.peer_bindings.c.unit_id==unit['unit_id'],
                    (s.storage_journal.c.state.is_(None))|(s.storage_journal.c.state!='deleted'))
                .order_by(s.peer_bindings.c.logical_id).limit(1))).mappings().first()
            if row is None:
                if cleanup.get('final_verified'):
                    unit=await self._save_after_transaction(conn,value,unit,cleanup,state='cleaned')
                    return unit,'cleaned'
                raise TransferError('migration_cleanup_final_verification_required')
            obj=self.verifier._object(row,value,unit);entry=await self._entry(conn,value,unit,obj)
        root=self.t.root_provider()['media' if unit['resource_kind']=='media' else 'recordings']
        if entry is None:
            observed=await joined_io(files.inspect,root,unit['parent_path'],obj['source_path'].rsplit('/',1)[-1],proof)
            if observed['file'] is None:raise TransferError('migration_cleanup_source_missing_without_intent')
            expected=await self.verifier._identity(value,relation,obj)
            if expected!=observed['file']:raise TransferError('migration_cleanup_source_file_changed')
            async with media_mutation_lock,self.store.database.begin() as conn:
                current=await self._unit(conn,value,unit['unit_id'])
                if current['publication']!=unit['publication']:raise TransferError('migration_cleanup_unit_changed')
                waiting=await self._clear(conn,value)
                if waiting:return unit,waiting
                if await self._entry(conn,value,unit,obj) is not None:raise TransferError('migration_cleanup_journal_changed')
                await self._metadata(conn,obj)
                publication=dict(action='source-cleanup',native=expected,commit_sha256=unit['publication']['commit_sha256'])
                await conn.execute(insert(s.storage_journal).values(task_id=value['task_id'],logical_id=obj['logical_id'],unit_id=unit['unit_id'],
                    master_id=value['master_id'],source_id=value['source_id'],target_id=value['target_id'],resource_kind=obj['resource_kind'],
                    object_id=obj['source_object'],physical_path=obj['source_path'],source_version=obj['source_version'],expected_bytes=obj['expected_bytes'],
                    sha256=obj['sha256'],stage_identity='',cleanup_version=digest(expected),publication=publication,state='deleting',updated_at=int(self.t.wall())))
                await self.store.log(conn,'migration_source_unlink_intent',value['master_id'],task_id=value['task_id'],logical_id=obj['logical_id'])
                entry=await self._entry(conn,value,unit,obj)
        try:
            async with media_mutation_lock:
                async with self.store.database.begin() as conn:
                    current=await self._unit(conn,value,unit['unit_id']);waiting=await self._clear(conn,value)
                    if waiting:return unit,waiting
                    await self._metadata(conn,obj)
                    if current['publication']!=unit['publication'] or await self._entry(conn,value,unit,obj)!=entry:
                        raise TransferError('migration_cleanup_unit_changed')
                # Keep the media fence, Source I/O lock and durable intent, but
                # let authenticated health/status acquire the node row while
                # the filesystem completes unlink/fsync.
                self._alive(value)
                after=await joined_io(files.unlink,root,unit['parent_path'],obj['source_path'].rsplit('/',1)[-1],proof,
                    cleanup['directory_version'],entry['publication']['native'],cleanup.get('reconciled_missing')==obj['logical_id'])
                self._alive(value)
                async with self.store.database.begin() as conn:
                    current=await self._unit(conn,value,unit['unit_id']);await self._metadata(conn,obj)
                    if current['publication']!=unit['publication'] or await self._entry(conn,value,unit,obj)!=entry:
                        raise TransferError('migration_cleanup_unit_changed')
                    if obj['resource_kind']=='media':
                        result=await conn.execute(text('DELETE FROM media_objects WHERE media_id=:id AND media_path=:path'),
                            dict(id=obj['source_object'],path=obj['source_path']))
                        if result.rowcount!=1:raise TransferError('migration_cleanup_native_metadata_changed')
                        await media_crypto.forget_encryption(conn,obj['source_object'])
                    await conn.execute(update(s.storage_journal).where(s.storage_journal.c.task_id==value['task_id'],
                        s.storage_journal.c.logical_id==obj['logical_id']).values(state='deleted',updated_at=int(self.t.wall())))
                    cleanup={**cleanup,'directory_version':digest(after),'reconciled_missing':'',
                        'deleted_count':cleanup['deleted_count']+1,'deleted_bytes':cleanup['deleted_bytes']+obj['expected_bytes'],
                        'verified':cleanup['deleted_count']+1<unit['object_count'],'final_verified':False}
                    unit=await self._save_after_transaction(conn,value,unit,cleanup)
            return unit,'cleaning'
        except files.RescanRequired:
            unit=await self._save(value,unit,{**cleanup,'verified':False,'final_verified':False})
            return unit,'verifying'

    async def _save_after_transaction(self,conn,value,unit,cleanup,*,state='cleanup_pending'):
        self._alive(value)
        if await self._counts(conn,value,unit)!=(cleanup['deleted_count'],cleanup['deleted_bytes']):
            raise TransferError('migration_cleanup_journal_inconsistent')
        await conn.execute(update(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit['unit_id'])
            .values(state=state,publication={**unit['publication'],'cleanup':cleanup}))
        await self.store.log(conn,'migration_source_replica_cleaned' if state=='cleaned' else 'migration_source_replica_removed',
            value['master_id'],task_id=value['task_id'],unit_id=unit['unit_id'],deleted_count=cleanup['deleted_count'],deleted_bytes=cleanup['deleted_bytes'])
        return {**unit,'state':state,'publication':{**unit['publication'],'cleanup':cleanup}}

    async def advance(self,node,relation,request_id,grant,unit_id,target_proof):
        identifier(request_id);identifier(unit_id,long=True)
        if self.t.source_lock.locked():raise TransferError('migration_transfer_source_busy')
        async with self.t.source_lock:
            try:
                value,active=await self.t._bind(grant,'source',expected='cleanup',authenticated_relation=relation)
                async with self.store.database.begin() as conn:unit=await self._unit(conn,value,unit_id)
                self.deadline=live_proof(target_proof,value,unit['publication']['commit'],self.t.wall())['expires_at']
                if unit['state']=='cleaned':return self._reply(value,request_id,unit,'cleaned')
                if unit['state']=='committed':
                    cleanup=dict(version=1,started_at=int(self.t.wall()),verified=False,final_verified=False,deleted_count=0,deleted_bytes=0)
                    unit=await self._save(value,unit,cleanup,state='cleanup_pending',action='migration_source_cleanup_intent')
                    return self._reply(value,request_id,unit,'verifying')
                if not unit['publication']['cleanup']['verified']:
                    unit=await self._scan(value,active,unit)
                    phase='ready' if unit['publication']['cleanup']['verified'] else 'verifying'
                else:unit,phase=await self._remove(value,active,unit)
                return self._reply(value,request_id,unit,phase)
            except BaseException:
                if self.t.source_cleanup_scan is not None:
                    await joined_io(self.t.source_cleanup_scan.close);self.t.source_cleanup_scan=None
                raise
