"""Fresh, bounded Source identity/metadata verification without rehashing payloads.

The original content hash remains valid only while the exact Source version
(including inode, ctime, size and metadata) is unchanged. Managed write exclusions
remain held. This supplies a unit proof, never ownership cutover or deletion.
"""
import hashlib
import json
import os
import stat
from types import SimpleNamespace

from sqlalchemy import select,update

from app.core.joined_io import joined_io
from app.services import resource_migration_schema as s
from app.services.federation import protocol as p
from app.services.resource_inventory import _root
from app.services.resource_migration_planning import extend
from app.services.resource_observation import digest,native
from app.services.resource_transfer_protocol import TransferError,identifier,immutable


class SourceScan:
    def __init__(self,root,parent,key,now):
        descriptor,exists=_root(root)
        try:
            if not exists:raise TransferError('migration_verify_root_missing')
            self.root=native(os.fstat(descriptor))
            for name in parent.split('/'):
                child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
                os.close(descriptor);descriptor=child
            if os.fstat(descriptor).st_uid!=os.geteuid():raise TransferError('migration_verify_directory_owner')
            self.directory=native(os.fstat(descriptor));self.iterator=os.scandir(descriptor)
            self.descriptor=descriptor;descriptor=None
        finally:
            if descriptor is not None:os.close(descriptor)
        self.key=key;self.deadline=now+300;self.phase='objects';self.after=''
        self.checksum=hashlib.sha256();self.count=0;self.bytes=0
        self.scanned_count=0;self.scanned_bytes=0;self.finished=False

    def close(self):
        if self.descriptor is not None:
            self.iterator.close();os.close(self.descriptor);self.descriptor=None

    def unchanged(self):
        if native(os.fstat(self.descriptor))!=self.directory:raise TransferError('migration_verify_directory_changed')

    def page(self):
        self.unchanged();result=[]
        for _ in range(32):
            item=next(self.iterator,None)
            if item is None:self.finished=True;break
            info=item.stat(follow_symlinks=False)
            # Nested directories are separate immediate-parent ownership units.
            # Their files are not counted as direct children of this unit.
            if stat.S_ISDIR(info.st_mode):continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid():
                raise TransferError('migration_verify_unknown_entry')
            result.append(dict(name=item.name,native=native(info)))
            self.scanned_count+=1;self.scanned_bytes+=info.st_size
        self.unchanged();return result


class SourceVerification:
    def __init__(self,transfers):self.t,self.store=transfers,transfers.store

    async def _unit(self,conn,grant,unit_id):
        header=await self.t._control_row(conn,grant)
        row=(await conn.execute(select(s.units).where(s.units.c.task_id==grant['task_id'],s.units.c.unit_id==unit_id))).mappings().first()
        if not row or header['state']!='active' or row['state'] not in {'pending','copying','verified'}:
            raise TransferError('migration_verify_unit_unavailable')
        if not await conn.scalar(select(s.scopes.c.scope_id).where(s.scopes.c.task_id==grant['task_id'],
                s.scopes.c.member_id==grant['source_id'],s.scopes.c.resource_kind==row['resource_kind'],s.scopes.c.parent_path==row['parent_path'])):
            raise TransferError('migration_verify_scope_missing')
        return dict(row)

    def _object(self,row,grant,unit):
        raw=json.loads(row['binding_json']);obj=raw['object']
        expected={name:value for name,value in immutable(grant).items() if name!='object'}
        if (digest(raw)!=row['binding_sha256'] or any(raw[name]!=value for name,value in expected.items()) or
            obj['logical_id']!=row['logical_id'] or obj['unit_id']!=unit['unit_id'] or row['unit_id']!=obj['unit_id'] or
            row['source_path']!=obj['source_path'] or obj['unit_version']!=unit['source_version'] or
            obj['source_path'].rsplit('/',1)[0]!=unit['parent_path'] or obj['resource_kind']!=unit['resource_kind']):
            raise TransferError('migration_verify_binding_changed')
        return obj

    async def _identity(self,grant,relation,obj):
        record=SimpleNamespace(header=dict(node_id=grant['source_id'],master_id=grant['master_id'],
            relationship_id=grant['source_relationship'],resource_kind=obj['resource_kind'],
            object_id=obj['source_object'],user_id=obj['user_id']),relation=relation)
        descriptor,current=await self.t.observer._open(record)
        try:
            if any(current[name]!=obj[key] for name,key in (('source_version','source_version'),('bytes','expected_bytes'),
                    ('physical_path','source_path'),('metadata_sha256','metadata_sha256'),('encryption_sha256','encryption_sha256'))):
                raise TransferError('migration_verify_source_changed')
            return current['native']
        finally:os.close(descriptor)

    def _alive(self,value):self.t._alive(value)

    async def _persist(self,conn,value,current,proof):
        await conn.execute(update(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==current['unit_id'])
            .values(state='verified',publication={**current['publication'],'source_verification':proof}))

    async def _step(self,node,active,request_id,value,unit_id):
        self._alive(value)
        async with self.store.database.begin() as conn:unit=await self._unit(conn,value,unit_id)
        key=(value['task_id'],unit_id,value['epoch'],value['runner_id'])
        scan=self.t.source_verification
        if scan is not None and (scan.key!=key or self.t.wall()>=scan.deadline):
            await joined_io(scan.close);scan=None;self.t.source_verification=None
        if scan is None:
            root=self.t.root_provider()['media' if unit['resource_kind']=='media' else 'recordings']
            scan=await joined_io(SourceScan,root,unit['parent_path'],key,self.t.wall(),on_cancel=lambda item:item.close())
            self.t.source_verification=scan
        await joined_io(scan.unchanged)
        if scan.phase=='objects':
            async with self.store.database.begin() as conn:
                await self._unit(conn,value,unit_id)
                rows=(await conn.execute(select(s.peer_bindings).where(s.peer_bindings.c.task_id==value['task_id'],
                    s.peer_bindings.c.unit_id==unit_id,s.peer_bindings.c.logical_id>scan.after)
                    .order_by(s.peer_bindings.c.logical_id).limit(8))).mappings().all()
            for row in rows:
                self._alive(value);obj=self._object(row,value,unit)
                await self._identity(value,active,obj)
                extend(scan.checksum,dict(logical_id=obj['logical_id'],source_object=obj['source_object'],
                    source_sha256=obj['sha256'],source_version=obj['source_version']))
                scan.after=obj['logical_id'];scan.count+=1;scan.bytes+=obj['expected_bytes']
            if not rows:
                if (scan.count,scan.bytes,scan.checksum.hexdigest())!=(unit['object_count'],unit['total_bytes'],unit['source_version']):
                    raise TransferError('migration_verify_roster_changed')
                scan.phase='membership'
        else:
            entries=await joined_io(scan.page)
            for item in entries:
                self._alive(value)
                async with self.store.database.begin() as conn:
                    await self._unit(conn,value,unit_id)
                    row=(await conn.execute(select(s.peer_bindings).where(s.peer_bindings.c.task_id==value['task_id'],
                        s.peer_bindings.c.unit_id==unit_id,s.peer_bindings.c.source_path==unit['parent_path']+'/'+item['name']))).mappings().first()
                if not row:raise TransferError('migration_verify_unknown_entry')
                obj=self._object(row,value,unit)
                if await self._identity(value,active,obj)!=item['native']:raise TransferError('migration_verify_source_changed')
            if scan.scanned_count>unit['object_count'] or scan.scanned_bytes>unit['total_bytes']:
                raise TransferError('migration_verify_roster_changed')
            if scan.finished:
                if (scan.scanned_count,scan.scanned_bytes)!=(unit['object_count'],unit['total_bytes']):
                    raise TransferError('migration_verify_roster_changed')
                await joined_io(scan.unchanged);self._alive(value)
                proof=dict(task_id=value['task_id'],node_id=value['source_id'],unit_id=unit_id,unit_version=unit['source_version'],
                    object_count=scan.count,total_bytes=scan.bytes,root=scan.root,directory=scan.directory,
                    verified_at=int(self.t.wall()),expires_at=value['expires_at'])
                async with self.store.database.begin() as conn:
                    current=await self._unit(conn,value,unit_id)
                    await self._persist(conn,value,current,proof)
                    await self.store.log(conn,'migration_source_unit_verified',value['master_id'],task_id=value['task_id'],unit_id=unit_id)
                scan.phase='verified'
        self._alive(value)
        result=dict(format='frontiercloud-python-source-verification',version=1,request_id=request_id,
            node_id=node['node_id'],task_id=value['task_id'],unit_id=unit_id,unit_version=unit['source_version'],
            object_count=unit['object_count'],total_bytes=unit['total_bytes'],phase=scan.phase,
            processed_objects=scan.count,epoch=value['epoch'],expires_at=value['expires_at'],
            receipt_sha256=digest(proof) if scan.phase=='verified' else '',ownership_changed=False)
        if scan.phase=='verified':await joined_io(scan.close);self.t.source_verification=None
        return result

    async def advance(self,node,relation,request_id,grant,unit_id):
        identifier(request_id);identifier(unit_id,long=True)
        if self.t.source_lock.locked():raise TransferError('migration_transfer_source_busy')
        async with self.t.source_lock:
            try:
                value,active=await self.t._bind(grant,'source',expected='verify',authenticated_relation=relation)
                return await self._step(node,active,request_id,value,unit_id)
            except BaseException:
                if self.t.source_verification is not None:
                    await joined_io(self.t.source_verification.close);self.t.source_verification=None
                raise
