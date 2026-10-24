"""Fresh post-commit Target proofs. No payload rehash, decryption or deletion."""
import hashlib
import os

from sqlalchemy import select,text,update

from app.core.joined_io import joined_io
from app.services import media_crypto,resource_migration_schema as s,resource_publish_files as files
from app.services.federation import protocol as p
from app.services.resource_commit_peers import CommitPeers
from app.services.resource_migration_planning import extend
from app.services.resource_observation import digest
from app.services.resource_pool import validate_media_path
from app.services.resource_publish_peers import PublishPeers
from app.services.resource_transfer_protocol import WINDOW_SECONDS,TransferError,identifier


def live_proof(envelope,grant,commit,now):
    if not isinstance(envelope,dict) or envelope.keys()!={'payload','signature'}:
        raise TransferError('migration_target_verify_proof_invalid')
    try:value=p.verify(grant['target_key'],envelope)
    except p.ProtocolError:raise TransferError('migration_target_verify_proof_invalid') from None
    expected=dict(format='frontiercloud-python-target-live-proof',version=1,node_id=grant['target_id'],task_id=grant['task_id'],
        unit_id=commit['unit_id'],unit_version=commit['unit_version'],commit_sha256=digest(commit),
        object_count=commit['object_count'],total_bytes=commit['total_bytes'],epoch=grant['epoch'])
    if (value.keys()!={*expected,'verified_at','expires_at','directory_version'} or
            any(value.get(name)!=item for name,item in expected.items()) or
            any(type(value[name]) is not int for name in ('version','object_count','total_bytes','epoch','verified_at','expires_at')) or
            not value['verified_at']<=now+5 or not now<value['expires_at'] or
            not 0<value['expires_at']-value['verified_at']<=WINDOW_SECONDS):
        raise TransferError('migration_target_verify_proof_invalid')
    identifier(value['directory_version'],long=True)
    return value


def open_scan(root,task_id,unit,expected_root,key,now):
    descriptor=files.open_directory(root,task_id,unit,expected_root,unit['publication'],published=True)
    try:return TargetScan(key,descriptor,now)
    except BaseException:os.close(descriptor);raise


class TargetScan(files.Scan):
    def __init__(self,key,descriptor,now):
        super().__init__(key,descriptor)
        self.deadline=now+300;self.phase='objects';self.after=''
        self.roster=hashlib.sha256();self.objects=0;self.object_bytes=0


class TargetVerification:
    def __init__(self,transfers,*,purpose='inspect'):
        self.t,self.store=transfers,transfers.store
        self.publisher=PublishPeers(transfers)
        self.purpose=purpose

    async def _unit(self,conn,value,unit_id):
        unit,root=await self.publisher._unit(conn,value,unit_id)
        commit=unit['publication'].get('commit')
        if (unit['state'] not in {'committed','cleanup_pending'} or not commit or
                CommitPeers(self.t)._record(commit,value)!=unit['publication'].get('commit_sha256') or
                any(commit[name]!=unit[field] for name,field in (
                    ('unit_id','unit_id'),('resource_kind','resource_kind'),('unit_version','source_version'),
                    ('target_parent','parent_path'),('object_count','object_count'),('total_bytes','total_bytes')))):
            raise TransferError('migration_target_verify_commit_missing_or_changed')
        return unit,root

    async def _object(self,conn,value,unit,logical):
        entry,obj=await self.publisher._object(conn,value,logical)
        if (entry['state']!='published' or entry['publication_phase']!=3 or entry['unit_id']!=unit['unit_id'] or
                obj['unit_version']!=unit['source_version'] or obj['resource_kind']!=unit['resource_kind'] or
                obj['target_path'].rsplit('/',1)[0]!=unit['parent_path'] or
                not isinstance(entry['publication'].get('assembled_native'),dict)):
            raise TransferError('migration_target_verify_object_changed')
        return entry,obj

    async def _metadata(self,conn,entry,obj):
        if obj['resource_kind']!='media':return
        actual=(await conn.execute(text('SELECT object_kind,media_path,path_locator FROM media_objects WHERE media_id=:id'),
            dict(id=obj['source_object']))).mappings().first()
        if (not actual or (actual['media_path'],actual['object_kind'])!=validate_media_path(obj['target_path']) or
                actual['path_locator']!=hashlib.sha256(obj['target_path'].encode()).hexdigest()):
            raise TransferError('migration_target_verify_registry_changed')
        metadata=await media_crypto.encryption_for_id(conn,obj['source_object'])
        if (metadata.to_dict() if metadata else None)!=entry['publication']['encryption']:
            raise TransferError('migration_target_verify_encryption_changed')

    async def advance(self,node,relation,request_id,grant,unit_id):
        identifier(request_id);identifier(unit_id,long=True)
        if self.t.target_lock.locked():raise TransferError('migration_transfer_target_busy')
        async with self.t.target_lock:
            try:
                value,_=await self.t._bind(grant,'target',expected=self.purpose,authenticated_relation=relation)
                async with self.store.database.begin() as conn:unit,expected_root=await self._unit(conn,value,unit_id)
                root=self.t.root_provider()['media' if unit['resource_kind']=='media' else 'recordings']
                version=await joined_io(files.published_version,root,value['task_id'],unit,expected_root,unit['publication'])
                if version!=unit['publication'].get('published_scan_version'):
                    raise TransferError('migration_target_verify_directory_changed')
                key=(value['task_id'],unit_id,value['epoch'],value['runner_id'],unit['publication']['commit_sha256'],self.purpose)
                scan=self.t.target_verification
                if scan is not None and (scan.key!=key or self.t.wall()>=scan.deadline):
                    await joined_io(scan.close);self.t.target_verification=None;scan=None
                if scan is None:
                    scan=await joined_io(open_scan,root,value['task_id'],unit,expected_root,key,self.t.wall(),on_cancel=lambda item:item.close())
                    self.t.target_verification=scan
                if scan.version!=version:raise TransferError('migration_target_verify_directory_changed')
                if scan.phase=='objects':
                    async with self.store.database.begin() as conn:
                        await self._unit(conn,value,unit_id)
                        rows=(await conn.execute(select(s.storage_journal.c.logical_id).where(s.storage_journal.c.task_id==value['task_id'],
                            s.storage_journal.c.unit_id==unit_id,s.storage_journal.c.logical_id>scan.after)
                            .order_by(s.storage_journal.c.logical_id).limit(8))).scalars().all()
                        for logical in rows:
                            self.t._alive(value);entry,obj=await self._object(conn,value,unit,logical)
                            await self._metadata(conn,entry,obj)
                            await joined_io(files.inspect_published,root,value['task_id'],unit,obj,expected_root,
                                unit['publication'],entry['publication']['assembled_native'])
                            extend(scan.roster,dict(logical_id=obj['logical_id'],source_object=obj['source_object'],
                                source_sha256=obj['sha256'],source_version=obj['source_version']))
                            scan.after=logical;scan.objects+=1;scan.object_bytes+=obj['expected_bytes']
                    if not rows:
                        if (scan.objects,scan.object_bytes,scan.roster.hexdigest())!=(unit['object_count'],unit['total_bytes'],unit['source_version']):
                            raise TransferError('migration_target_verify_roster_changed')
                        scan.phase='membership'
                else:
                    entries=await joined_io(scan.page)
                    async with self.store.database.begin() as conn:
                        await self._unit(conn,value,unit_id)
                        for item in entries:
                            logical=await conn.scalar(select(s.storage_journal.c.logical_id).where(s.storage_journal.c.task_id==value['task_id'],
                                s.storage_journal.c.unit_id==unit_id,s.storage_journal.c.physical_path==unit['parent_path']+'/'+item['name']))
                            if logical is None:raise TransferError('migration_target_verify_unknown_entry')
                            entry,obj=await self._object(conn,value,unit,logical);await self._metadata(conn,entry,obj)
                            if item['native']!=entry['publication']['assembled_native']:
                                raise TransferError('migration_target_verify_file_changed')
                    if scan.count>unit['object_count'] or scan.bytes>unit['total_bytes']:
                        raise TransferError('migration_target_verify_roster_changed')
                    if scan.finished:
                        if (scan.count,scan.bytes)!=(unit['object_count'],unit['total_bytes']):
                            raise TransferError('migration_target_verify_roster_changed')
                        if await joined_io(files.published_version,root,value['task_id'],unit,expected_root,unit['publication'])!=scan.version:
                            raise TransferError('migration_target_verify_directory_changed')
                        self.t._alive(value)
                        proof=dict(format='frontiercloud-python-target-live-proof',version=1,node_id=node['node_id'],task_id=value['task_id'],
                            unit_id=unit_id,unit_version=unit['source_version'],commit_sha256=unit['publication']['commit_sha256'],
                            object_count=scan.count,total_bytes=scan.bytes,epoch=value['epoch'],verified_at=int(self.t.wall()),
                            expires_at=value['expires_at'],directory_version=scan.version)
                        signed=p.sign(self.store.unseal(node['private_key']),proof)
                        async with self.store.database.begin() as conn:
                            current,_=await self._unit(conn,value,unit_id)
                            await conn.execute(update(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit_id)
                                .values(publication={**current['publication'],'target_live_verification':signed}))
                            await self.store.log(conn,'migration_target_live_verified',value['master_id'],task_id=value['task_id'],unit_id=unit_id)
                        scan.phase='verified'
                self.t._alive(value)
                result=dict(format='frontiercloud-python-target-verification',version=1,request_id=request_id,node_id=node['node_id'],
                    task_id=value['task_id'],unit_id=unit_id,unit_version=unit['source_version'],phase=scan.phase,processed_objects=scan.objects,
                    object_count=unit['object_count'],total_bytes=unit['total_bytes'],epoch=value['epoch'],expires_at=value['expires_at'],
                    proof=signed if scan.phase=='verified' else None,source_files_removed=False)
                if scan.phase=='verified':await joined_io(scan.close);self.t.target_verification=None
                return result
            except BaseException:
                if self.t.target_verification is not None:
                    await joined_io(self.t.target_verification.close);self.t.target_verification=None
                raise
