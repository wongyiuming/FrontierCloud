"""Native whole-parent publication. Reservations remain until Master cutover."""
from datetime import datetime,timezone
from functools import partial
import hashlib
import json
import os

from sqlalchemy import func,select,text,update

from app.core.joined_io import joined_io
from app.services import media_crypto,resource_migration_schema as s,resource_publish_files as files
from app.services.federation import protocol as p
from app.services.media_manager import media_mutation_lock
from app.services.resource_migration_planning import extend
from app.services.resource_observation import digest
from app.services.resource_pool import validate_media_path
from app.services.resource_transfer_protocol import CONTROL_BYTES,TransferError,identifier,target_encryption_digest


class PublishPeers:
    def __init__(self,transfers):self.t,self.store=transfers,transfers.store

    async def _begin(self,node,relation,request_id,grant):
        identifier(request_id)
        value,_=await self.t._bind(grant,'target',expected='publish',authenticated_relation=relation)
        if value['target_id']!=node['node_id']:raise TransferError('migration_publish_target_changed')
        return value

    async def _unit(self,conn,grant,unit_id):
        identifier(unit_id,long=True)
        header=await self.t._control_row(conn,grant)
        row=(await conn.execute(select(s.units).where(s.units.c.task_id==grant['task_id'],s.units.c.unit_id==unit_id).with_for_update())).mappings().first()
        if not row or header['state']!='active':raise TransferError('migration_publish_unit_unavailable')
        row=dict(row);kind=row['resource_kind'];parent=row['parent_path']
        source_parent=parent if kind=='media' else grant['source_relationship']+'/'+parent.split('/')[-1]
        value=dict(unit_id=unit_id,resource_kind=kind,source_parent=source_parent,target_parent=parent,
            source_version=row['source_version'],object_count=row['object_count'],total_bytes=row['total_bytes'])
        self.t.native._unit(header,value)
        if not await conn.scalar(select(s.scopes.c.scope_id).where(s.scopes.c.task_id==grant['task_id'],
                s.scopes.c.member_id==grant['target_id'],s.scopes.c.resource_kind==kind,s.scopes.c.parent_path==parent)):
            raise TransferError('migration_publish_scope_missing')
        expected_root=header['payload']['transfer'].get('staging_roots',{}).get(kind)
        if not expected_root:raise TransferError('migration_publish_root_unproven')
        return row,expected_root

    async def _object(self,conn,grant,logical_id):
        binding=(await conn.execute(select(s.peer_bindings).where(s.peer_bindings.c.task_id==grant['task_id'],
            s.peer_bindings.c.logical_id==logical_id))).mappings().first()
        entry=(await conn.execute(select(s.storage_journal).where(s.storage_journal.c.task_id==grant['task_id'],
            s.storage_journal.c.logical_id==logical_id).with_for_update())).mappings().first()
        if not binding or not entry:raise TransferError('migration_publish_object_unavailable')
        raw=json.loads(binding['binding_json']);obj=raw['object']
        if (digest(raw)!=binding['binding_sha256'] or any(raw[name]!=grant[name] for name in (
                'task_id','master_id','source_id','target_id','source_relationship','target_relationship','plan_sha256','units_sha256',
                'source_endpoint','target_endpoint','source_key','target_key','source_binding','target_binding')) or
            binding['unit_id']!=obj['unit_id'] or binding['source_path']!=obj['source_path'] or
            entry['unit_id']!=obj['unit_id'] or entry['resource_kind']!=obj['resource_kind'] or
            entry['physical_path']!=obj['target_path'] or entry['object_id']!=obj['source_object'] or
            entry['source_version']!=obj['source_version'] or entry['expected_bytes']!=obj['expected_bytes'] or
            entry['sha256']!=obj['sha256'] or entry['state'] not in {'verified','publishing','published'}):
            raise TransferError('migration_publish_object_changed')
        return dict(entry),obj

    def _reply(self,request_id,grant,unit,*,processed=0):
        return dict(format='frontiercloud-python-target-publication',version=1,request_id=request_id,
            node_id=grant['target_id'],task_id=grant['task_id'],unit_id=unit['unit_id'],unit_version=unit['source_version'],
            object_count=unit['object_count'],total_bytes=unit['total_bytes'],state=unit['state'],
            phase=unit['publication'].get('step','metadata'),processed=processed,epoch=grant['epoch'],
            receipt_sha256=unit['publication'].get('receipt_sha256',''),expires_at=grant['expires_at'],capacity_settled=False)

    async def describe(self,node,relation,request_id,grant,entries):
        if not isinstance(entries,list) or not 1<=len(entries)<=8 or len(p.canonical(entries))>CONTROL_BYTES//2:
            raise TransferError('migration_publish_metadata_invalid')
        if self.t.target_lock.locked():raise TransferError('migration_transfer_target_busy')
        async with self.t.target_lock:
            value=await self._begin(node,relation,request_id,grant)
            async with self.store.database.begin() as conn:
                last=None
                for supplied in entries:
                    if not isinstance(supplied,dict) or supplied.keys()!={'logical_id','encryption'}:
                        raise TransferError('migration_publish_metadata_invalid')
                    entry,obj=await self._object(conn,value,supplied['logical_id'])
                    unit,_=await self._unit(conn,value,entry['unit_id'])
                    if last is not None and unit['unit_id']!=last['unit_id']:raise TransferError('migration_publish_mixed_units')
                    descriptor=supplied['encryption']
                    if descriptor is not None:
                        descriptor=media_crypto.Metadata.from_dict(descriptor).to_dict()
                    if (digest(descriptor)!=target_encryption_digest(obj) or
                            descriptor is not None and descriptor['ciphertext_size']!=obj['expected_bytes'] or
                            obj['resource_kind']=='recording' and descriptor is not None):
                        raise TransferError('migration_publish_encryption_changed')
                    if entry['publication_phase']:
                        if entry['publication'].get('encryption')!=descriptor:raise TransferError('migration_publish_metadata_changed')
                    elif entry['state']!='verified' or unit['state'] not in {'pending','copying','verified'}:
                        raise TransferError('migration_publish_state_changed')
                    else:
                        verified=entry['publication'].get('verified_native')
                        if not isinstance(verified,dict) or verified.get('bytes')!=obj['expected_bytes']:
                            raise TransferError('migration_publish_stat_proof_missing')
                        await conn.execute(update(s.storage_journal).where(s.storage_journal.c.task_id==value['task_id'],
                            s.storage_journal.c.logical_id==obj['logical_id']).values(publication_phase=1,
                            publication={**entry['publication'],'encryption':descriptor}))
                    last=unit
                return self._reply(request_id,value,last,processed=len(entries))

    async def _save(self,grant,unit,**publication):
        async with self.store.database.begin() as conn:
            current,_=await self._unit(conn,grant,unit['unit_id'])
            if current['state']!=unit['state'] or current['publication']!=unit['publication']:
                raise TransferError('migration_publish_state_changed')
            result={**current['publication'],**publication}
            await conn.execute(update(s.units).where(s.units.c.task_id==grant['task_id'],s.units.c.unit_id==unit['unit_id']).values(publication=result))
            return {**current,'publication':result}

    async def _roster(self,grant,unit):
        key=(grant['task_id'],unit['unit_id'])
        record=self.t.publication_roster
        if record is None or record['key']!=key:
            record=dict(key=key,checksum=hashlib.sha256(),after='',count=0,bytes=0)
        async with self.store.database.begin() as conn:
            await self._unit(conn,grant,unit['unit_id'])
            rows=(await conn.execute(select(s.storage_journal.c.logical_id).where(s.storage_journal.c.task_id==grant['task_id'],
                s.storage_journal.c.unit_id==unit['unit_id'],s.storage_journal.c.logical_id>record['after'])
                .order_by(s.storage_journal.c.logical_id).limit(32))).scalars().all()
            for logical_id in rows:
                entry,obj=await self._object(conn,grant,logical_id)
                if entry['publication_phase']!=1 or entry['state']!='verified':raise TransferError('migration_publish_roster_changed')
                extend(record['checksum'],dict(logical_id=obj['logical_id'],source_object=obj['source_object'],
                    source_sha256=obj['sha256'],source_version=obj['source_version']))
                record['after']=logical_id;record['count']+=1;record['bytes']+=obj['expected_bytes']
        self.t.publication_roster=record
        if rows:return unit,record['count']
        if (record['count'],record['bytes'],record['checksum'].hexdigest())!=(unit['object_count'],unit['total_bytes'],unit['source_version']):
            raise TransferError('migration_publish_roster_changed')
        self.t.publication_roster=None
        return await self._save(grant,unit,step='assembling'),record['count']

    async def _register(self,conn,entry,obj,unit):
        if obj['resource_kind']=='media':
            from app.services.media_crypto_schema import encryption
            if await conn.scalar(text('SELECT media_id FROM media_objects WHERE media_id=:id OR path_locator=:locator'),
                    dict(id=obj['source_object'],locator=hashlib.sha256(obj['target_path'].encode()).hexdigest())):
                raise TransferError('migration_publish_registry_conflict')
            descriptor=entry['publication']['encryption']
            if descriptor is None and await conn.scalar(select(encryption.c.file_id).where(
                    encryption.c.object_kind=='media',encryption.c.object_id==obj['source_object'])):
                raise TransferError('migration_publish_encryption_conflict')
            path,kind=validate_media_path(obj['target_path'])
            stamp=datetime.fromtimestamp(unit['publication']['started_at'],timezone.utc).replace(tzinfo=None)
            await conn.execute(text('INSERT INTO media_objects (media_id,object_kind,media_path,path_locator,created_at,updated_at) '
                'VALUES (:id,:kind,:path,:locator,:stamp,:stamp)'),dict(id=obj['source_object'],kind=kind,path=path,
                locator=hashlib.sha256(path.encode()).hexdigest(),stamp=stamp))
            await media_crypto.put_encryption(conn,obj['source_object'],media_crypto.Metadata.from_dict(descriptor) if descriptor else None,restore=True)

    async def _scan(self,grant,unit,root,expected_root,*,published):
        key=(grant['task_id'],unit['unit_id'],published)
        scan=self.t.publication_scan
        if scan is not None and scan.key!=key:
            await joined_io(scan.close);scan=None;self.t.publication_scan=None
        if scan is None:
            descriptor=await joined_io(partial(files.open_directory,root,grant['task_id'],unit,expected_root,unit['publication'],
                published=published),on_cancel=os.close)
            scan=await joined_io(files.Scan,key,descriptor,on_cancel=lambda value:value.close())
            self.t.publication_scan=scan
        entries=await joined_io(scan.page)
        async with self.store.database.begin() as conn:
            await self._unit(conn,grant,unit['unit_id'])
            for item in entries:
                path=unit['parent_path']+'/'+item['name']
                logical_id=await conn.scalar(select(s.storage_journal.c.logical_id).where(s.storage_journal.c.task_id==grant['task_id'],
                    s.storage_journal.c.unit_id==unit['unit_id'],s.storage_journal.c.physical_path==path))
                if not logical_id:raise TransferError('migration_publish_unknown_entry')
                entry,obj=await self._object(conn,grant,logical_id)
                if entry['publication_phase']!=(3 if published else 2) or item['native']!=entry['publication']['assembled_native']:
                    raise TransferError('migration_publish_file_changed')
                if published and obj['resource_kind']=='media':
                    actual=(await conn.execute(text('SELECT object_kind,media_path FROM media_objects WHERE media_id=:id'),dict(id=obj['source_object']))).mappings().first()
                    if not actual or (actual['media_path'],actual['object_kind'])!=validate_media_path(path):
                        raise TransferError('migration_publish_registry_changed')
                    metadata=await media_crypto.encryption_for_id(conn,obj['source_object'])
                    if (metadata.to_dict() if metadata else None)!=entry['publication']['encryption']:
                        raise TransferError('migration_publish_encryption_changed')
        if scan.count>unit['object_count'] or scan.bytes>unit['total_bytes']:raise TransferError('migration_publish_unknown_entry')
        if not scan.finished:return unit,scan.count
        if (scan.count,scan.bytes)!=(unit['object_count'],unit['total_bytes']):raise TransferError('migration_publish_roster_changed')
        version=scan.version;count=scan.count
        await joined_io(scan.close);self.t.publication_scan=None
        if not published:return await self._save(grant,unit,step='move',scan_version=version),count
        from app.services.resource_transfer_files import remove_unit
        await joined_io(remove_unit,root,grant['task_id'],unit['unit_id'],expected_root)
        receipt=digest(dict(task_id=grant['task_id'],node_id=grant['target_id'],unit_id=unit['unit_id'],
            source_version=unit['source_version'],count=count,total_bytes=unit['total_bytes'],directory=unit['publication']['ready']))
        async with self.store.database.begin() as conn:
            current,_=await self._unit(conn,grant,unit['unit_id'])
            publication={**current['publication'],'step':'published','published_scan_version':version,'receipt_sha256':receipt}
            await conn.execute(update(s.units).where(s.units.c.task_id==grant['task_id'],s.units.c.unit_id==unit['unit_id'])
                .values(state='published',publication=publication))
            await self.store.log(conn,'migration_target_unit_published',grant['master_id'],task_id=grant['task_id'],unit_id=unit['unit_id'],bytes=unit['total_bytes'])
            return {**current,'state':'published','publication':publication},count

    async def advance(self,node,relation,request_id,grant,unit_id):
        if self.t.target_lock.locked():raise TransferError('migration_transfer_target_busy')
        async with self.t.target_lock:
            try:
                value=await self._begin(node,relation,request_id,grant)
                async with self.store.database.begin() as conn:unit,expected_root=await self._unit(conn,value,unit_id)
                root=self.t.root_provider()['media' if unit['resource_kind']=='media' else 'recordings']
                if unit['state']=='published':
                    if await joined_io(files.published_version,root,value['task_id'],unit,expected_root,unit['publication'])!=unit['publication']['published_scan_version']:
                        raise TransferError('migration_publish_directory_changed')
                    # Renewed receipts require another bounded inspection, not
                    # a fresh signature on historical file observations.
                    async with self.store.database.begin() as conn:
                        current,_=await self._unit(conn,value,unit_id)
                        publication={**current['publication'],'step':'scan_public','receipt_sha256':''}
                        await conn.execute(update(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit_id)
                            .values(state='publishing',publication=publication))
                        unit={**current,'state':'publishing','publication':publication}
                    return self._reply(request_id,value,unit)
                if unit['state'] in {'pending','copying','verified'}:
                    async with self.store.database.begin() as conn:
                        counts=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.storage_journal.c.expected_bytes),0),
                            func.min(s.storage_journal.c.publication_phase)).where(s.storage_journal.c.task_id==value['task_id'],
                            s.storage_journal.c.unit_id==unit_id,s.storage_journal.c.state=='verified'))).one()
                    if tuple(counts)!=(unit['object_count'],unit['total_bytes'],1):raise TransferError('migration_publish_metadata_incomplete')
                    proof=await joined_io(files.prepare,root,value['task_id'],unit,expected_root)
                    async with self.store.database.begin() as conn:
                        current,_=await self._unit(conn,value,unit_id)
                        if current!=unit:raise TransferError('migration_publish_state_changed')
                        publication={**proof,'step':'roster','started_at':int(self.t.wall())}
                        await conn.execute(update(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit_id)
                            .values(state='publishing',publication=publication))
                        unit={**current,'state':'publishing','publication':publication}
                    return self._reply(request_id,value,unit)
                if unit['state']!='publishing':raise TransferError('migration_publish_state_changed')
                step=unit['publication']['step'];processed=0
                if step=='roster':unit,processed=await self._roster(value,unit)
                elif step in ('assembling','register'):
                    phase=1 if step=='assembling' else 2
                    async with self.store.database.begin() as conn:
                        logical_id=await conn.scalar(select(s.storage_journal.c.logical_id).where(s.storage_journal.c.task_id==value['task_id'],
                            s.storage_journal.c.unit_id==unit_id,s.storage_journal.c.publication_phase==phase).order_by(s.storage_journal.c.logical_id).limit(1))
                        if logical_id:
                            entry,obj=await self._object(conn,value,logical_id)
                            if step=='assembling':await conn.execute(update(s.storage_journal).where(s.storage_journal.c.task_id==value['task_id'],
                                s.storage_journal.c.logical_id==logical_id).values(state='publishing'))
                    if logical_id:
                        if step=='assembling':
                            current=await joined_io(files.assemble,root,value['task_id'],unit_id,obj,expected_root,unit['publication'],entry['publication']['verified_native'])
                        else:
                            current=await joined_io(files.inspect_published,root,value['task_id'],unit,obj,expected_root,unit['publication'],entry['publication']['assembled_native'])
                        async with media_mutation_lock,self.store.database.begin() as conn:
                            await self._unit(conn,value,unit_id)
                            if step=='register':await self._register(conn,entry,obj,unit)
                            await conn.execute(update(s.storage_journal).where(s.storage_journal.c.task_id==value['task_id'],s.storage_journal.c.logical_id==logical_id)
                                .values(state='publishing' if step=='assembling' else 'published',publication_phase=phase+1,
                                publication={**entry['publication'],'assembled_native':current},updated_at=int(self.t.wall())))
                        processed=1
                    else:unit=await self._save(value,unit,step='scan_private' if step=='assembling' else 'scan_public')
                elif step in ('scan_private','scan_public'):unit,processed=await self._scan(value,unit,root,expected_root,published=step=='scan_public')
                elif step=='move':
                    await joined_io(files.move_directory,root,value['task_id'],unit,expected_root,unit['publication'])
                    unit=await self._save(value,unit,step='register')
                else:raise TransferError('migration_publish_state_changed')
                return self._reply(request_id,value,unit,processed=processed)
            except BaseException:
                self.t.publication_roster=None
                if self.t.publication_scan is not None:
                    await joined_io(self.t.publication_scan.close);self.t.publication_scan=None
                raise
