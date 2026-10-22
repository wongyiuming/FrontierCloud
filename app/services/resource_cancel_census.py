"""Fenced current Source census; no payload hashing, quota change or task release."""
import json
import os
import stat
import uuid

from sqlalchemy import func,insert,select,text,update

from app.core.joined_io import joined_io
from app.middleware.native_storage import _open_beneath
from app.services import media_crypto_schema as cs,resource_cancel_census_schema as c,resource_final_roots as roots,resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_cancel_unit_peers import UnitSettlement
from app.services.resource_inventory import Walker
from app.services.resource_observation import digest,native
from app.services.resource_source_cleanup import SourceCleanup
from app.services.resource_source_verify import SourceVerification
from app.services.resource_transfer_protocol import TransferError,identifier


def verify_proof(envelope,value,now):
    try:
        if not isinstance(envelope,dict) or envelope.keys()!={'payload','signature'}:raise ValueError()
        proof=p.verify(value['source_key'],envelope)
        expected=dict(format='frontiercloud-python-source-cancel-census',version=1,
            **{name:value[name] for name in ('task_id','master_id','source_id','target_id','source_relationship','target_relationship',
                'plan_sha256','units_sha256','source_binding','runner_id')},node_id=value['source_id'],epoch=value['epoch'],
            native_usage_reconciled=False,source_files_removed=False,source_drain_released=False,task_completed=False)
        if (not isinstance(proof,dict) or proof.keys()!={*expected,'census_id','object_count','total_bytes','catalog_sha256',
                'physical_sha256','roots_sha256','observed_at','expires_at'} or any(proof.get(name)!=item for name,item in expected.items()) or
                any(type(proof[name]) is not int or proof[name]<0 for name in ('version','epoch','object_count','total_bytes','observed_at','expires_at')) or
                any(type(proof[name]) is not bool for name in ('native_usage_reconciled','source_files_removed','source_drain_released','task_completed')) or
                not 0<proof['observed_at']<=now+5 or not proof['observed_at']<proof['expires_at']<=proof['observed_at']+45 or now>=proof['expires_at']):raise ValueError()
        identifier(proof['census_id'])
        for name in ('catalog_sha256','physical_sha256','roots_sha256'):identifier(proof[name],long=True)
        return proof
    except (p.ProtocolError,ValueError,KeyError,TypeError,RecursionError):raise TransferError('migration_cancel_census_proof_invalid') from None


def current_file(root,path):
    try:descriptor,info=_open_beneath(root,path.split('/'))
    except FileNotFoundError:return None
    try:
        if info.st_uid!=os.geteuid() or info.st_nlink!=1 or info.st_size<=0:
            raise TransferError('migration_cancel_census_file_invalid')
        return native(info)
    finally:os.close(descriptor)


class Scan:
    def __init__(self,key,now):self.key=key;self.walker=None;self.index=0;self.deadline=now+300;self.completed=''
    def close(self):
        if self.walker is not None:self.walker.close();self.walker=None
    def page(self,paths):
        kind=('media','recording')[self.index]
        if self.walker is None:self.walker=Walker(paths['media' if kind=='media' else 'recordings'])
        entries,finished=self.walker.page()
        if finished:self.close();self.index+=1
        return kind,entries,self.index==2


class SourceCensus:
    def __init__(self,transfers):self.t,self.store=transfers,transfers.store

    async def _header(self,conn,value):
        header=await self.t._control_row(conn,value)
        control=header['payload'].get('transfer',{})
        if (header['side']!='source' or header['state']!='active' or control.get('cancel_started') is not True or
                control.get('source_cancel_stopped') is not True or not isinstance(control.get('source_roots'),dict)):
            raise TransferError('migration_cancel_census_source_stop_required')
        if not await conn.scalar(select(s.drains.c.owner_id).where(s.drains.c.member_id==value['source_id'],
                s.drains.c.owner_id==value['task_id'],s.drains.c.reason=='migration')):
            raise TransferError('migration_cancel_census_source_drain_missing')
        return header

    async def _ready(self,conn,value,header):
        counts=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.units.c.object_count),0),
            func.coalesce(func.sum(s.units.c.total_bytes),0)).where(s.units.c.task_id==value['task_id'],
            (s.units.c.state=='cleaned')|((s.units.c.state=='cancelled')&(s.units.c.native_acknowledged==1))))).one()
        bindings=await conn.scalar(select(func.count()).select_from(s.peer_bindings).where(s.peer_bindings.c.task_id==value['task_id']))
        if tuple(counts)!=(header['expected_units'],header['expected_objects'],header['expected_bytes']) or bindings!=header['expected_objects']:
            raise TransferError('migration_cancel_census_terminal_units_required')
        member=(await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id==value['source_id']).with_for_update())).mappings().one()
        if member['reserved_bytes'] or await conn.scalar(select(fs.upload_sessions.c.upload_id).where(fs.upload_sessions.c.state=='reserved').limit(1)):
            raise TransferError('migration_cancel_census_source_writer_pending')
        if await conn.scalar(text('SELECT operation_id FROM media_delete_operations LIMIT 1')):
            raise TransferError('migration_cancel_census_source_writer_pending')

    async def _save(self,conn,value,header,progress,*,action='migration_cancel_census_step'):
        self.t._alive(value)
        await conn.execute(update(s.peer_reservations).where(s.peer_reservations.c.task_id==value['task_id']).values(
            payload={**header['payload'],'transfer':{**header['payload']['transfer'],'cancel_census':progress}},updated_at=int(self.t.wall())))
        await self.store.log(conn,action,value['master_id'],task_id=value['task_id'],census_id=progress['census_id'],
            phase=progress['phase'],retained_objects=progress['retained_objects'],retained_bytes=progress['retained_bytes'])

    async def _start(self,value):
        if self.t.source_cancel_census is not None:await joined_io(self.t.source_cancel_census.close)
        progress=dict(version=1,census_id=uuid.uuid4().hex,epoch=value['epoch'],runner_id=value['runner_id'],phase='roster',
            unit_after='',unit_id='',object_after='',processed_objects=0,processed_bytes=0,retained_objects=0,retained_bytes=0,
            media_objects=0,physical_objects=0,physical_bytes=0,catalog_sha256=digest(['cancel-current-catalog',1]),
            physical_sha256=digest(['cancel-current-physical',1]),proof=None)
        async with self.store.database.begin() as conn:header=await self._header(conn,value)
        await roots_check(self.t,header)
        async with media_mutation_lock,self.store.database.begin() as conn:
            header=await self._header(conn,value);await self._ready(conn,value,header)
            await self._save(conn,value,header,progress,action='migration_cancel_census_fenced')
        self.t.source_cancel_census=Scan((value['task_id'],progress['census_id'],value['epoch'],value['runner_id']),self.t.wall())
        return progress

    async def _unit(self,conn,value,unit_id):
        unit=dict((await conn.execute(select(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit_id))).mappings().one())
        if unit['state']=='cancelled':
            header=await self._header(conn,value)
            master_key=await conn.scalar(select(fs.relationships.c.peer_key).where(fs.relationships.c.relationship_id==value['source_relationship']))
            await UnitSettlement(self.t)._recorded(unit,value,'source',master_key,
                header['payload']['transfer']['source_roots'])
        elif unit['state']=='cleaned':
            await SourceCleanup(self.t)._unit(conn,value,unit_id)
            counts=await SourceCleanup(self.t)._counts(conn,value,unit)
            if counts!=(unit['object_count'],unit['total_bytes']):raise TransferError('migration_cancel_census_cleanup_incomplete')
        else:raise TransferError('migration_cancel_census_terminal_units_required')
        return unit

    async def _snapshot(self,value,unit,binding):
        obj=SourceVerification(self.t)._object(binding,value,unit)
        kind=obj['resource_kind'];path=obj['source_path'];metadata=None;encryption=None
        if kind=='media':
            async with self.store.database.connect() as conn:
                exists=await conn.scalar(text('SELECT media_id FROM media_objects WHERE media_id=:id'),dict(id=obj['source_object']))
                if exists is not None:path,metadata,encryption=await self.t.observer.media_snapshot(obj['source_object'],conn=conn)
                elif await conn.scalar(select(cs.encryption.c.object_id).where(cs.encryption.c.object_kind=='media',
                        cs.encryption.c.object_id==obj['source_object'],cs.encryption.c.descriptor_json.is_not(None))):
                    raise TransferError('migration_cancel_census_orphan_encryption')
            if unit['state']=='cleaned':
                if exists is not None:raise TransferError('migration_cancel_census_cleaned_registry_reappeared')
                return obj,None
            if exists is None:return obj,None
        elif unit['state']=='cleaned':return obj,None
        root=self.t.root_provider()['media' if kind=='media' else 'recordings']
        info=await joined_io(current_file,root,path)
        if info is None:
            if kind=='media':raise TransferError('migration_cancel_census_registered_file_missing')
            return obj,None
        if info['bytes']!=obj['expected_bytes'] or kind=='media' and digest(encryption)!=obj['encryption_sha256']:
            raise TransferError('migration_cancel_census_resource_changed')
        catalog=dict(logical_id=obj['logical_id'],resource_kind=kind,object_id=obj['source_object'],physical_path=path,
            bytes=info['bytes'],object_kind=metadata['object_kind'] if metadata else 'recording',user_id=obj['user_id'],
            encryption_sha256=digest(encryption))
        snapshot=dict(stat=info,metadata_sha256=digest(metadata),encryption_sha256=digest(encryption))
        return obj,dict(catalog=catalog,native=snapshot)

    async def _roster(self,value,progress):
        original=progress
        async with self.store.database.begin() as conn:
            header=await self._header(conn,value)
            if header['payload']['transfer'].get('cancel_census')!=progress:raise TransferError('migration_cancel_census_progress_changed')
            unit_id=progress['unit_id'] or await conn.scalar(select(s.units.c.unit_id).where(s.units.c.task_id==value['task_id'],
                s.units.c.unit_id>progress['unit_after']).order_by(s.units.c.unit_id).limit(1))
            if unit_id is None:
                if (progress['processed_objects'],progress['processed_bytes'])!=(header['expected_objects'],header['expected_bytes']):
                    raise TransferError('migration_cancel_census_roster_incomplete')
                progress={**progress,'phase':'physical'};await self._save(conn,value,header,progress);return progress
            unit=await self._unit(conn,value,unit_id)
            rows=(await conn.execute(select(s.peer_bindings).where(s.peer_bindings.c.task_id==value['task_id'],
                s.peer_bindings.c.unit_id==unit_id,s.peer_bindings.c.logical_id>progress['object_after']).order_by(s.peer_bindings.c.logical_id).limit(16))).mappings().all()
        progress={**progress,'unit_id':unit_id};records=[]
        for binding in rows:
            self.t._alive(value)
            obj,snapshot=await self._snapshot(value,unit,binding)
            progress={**progress,'object_after':obj['logical_id'],'processed_objects':progress['processed_objects']+1,
                'processed_bytes':progress['processed_bytes']+obj['expected_bytes']}
            if snapshot is None:continue
            catalog=snapshot['catalog'];records.append(dict(task_id=value['task_id'],logical_id=obj['logical_id'],census_id=progress['census_id'],
                resource_kind=obj['resource_kind'],object_id=obj['source_object'],physical_path=catalog['physical_path'],
                path_locator=digest(catalog['physical_path']),expected_bytes=catalog['bytes'],native_json=p.canonical(snapshot['native']).decode(),
                catalog_json=p.canonical(catalog).decode(),seen=0))
            progress={**progress,'retained_objects':progress['retained_objects']+1,'retained_bytes':progress['retained_bytes']+catalog['bytes'],
                'media_objects':progress['media_objects']+(obj['resource_kind']=='media'),
                'catalog_sha256':digest([progress['catalog_sha256'],catalog])}
        if not rows:progress={**progress,'unit_after':unit_id,'unit_id':'','object_after':''}
        async with self.store.database.begin() as conn:
            header=await self._header(conn,value)
            current=(await conn.execute(select(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit_id))).mappings().one()
            if header['payload']['transfer'].get('cancel_census')!=original or current['state']!=unit['state'] or current['publication']!=unit['publication']:
                raise TransferError('migration_cancel_census_progress_changed')
            for record in records:
                existing=await conn.scalar(select(c.snapshots.c.logical_id).where(c.snapshots.c.task_id==value['task_id'],c.snapshots.c.logical_id==record['logical_id']))
                if existing is None:await conn.execute(insert(c.snapshots).values(**record))
                else:await conn.execute(update(c.snapshots).where(c.snapshots.c.task_id==value['task_id'],c.snapshots.c.logical_id==record['logical_id']).values(**record))
            await self._save(conn,value,header,progress)
        return progress

    async def _physical(self,value,progress):
        scan=self.t.source_cancel_census
        kind,entries,finished=await joined_io(scan.page,self.t.root_provider())
        async with self.store.database.begin() as conn:
            header=await self._header(conn,value)
            if header['payload']['transfer'].get('cancel_census')!=progress:raise TransferError('migration_cancel_census_progress_changed')
            for entry in entries:
                self.t._alive(value)
                if entry['kind']=='directory':continue
                if entry['kind']!='file' or entry['links']!=1:raise TransferError('migration_cancel_census_unknown_file')
                row=(await conn.execute(select(c.snapshots).where(c.snapshots.c.task_id==value['task_id'],
                    c.snapshots.c.resource_kind==kind,c.snapshots.c.path_locator==digest(entry['path'])))).mappings().first()
                if not row or row['physical_path']!=entry['path'] or row['census_id']!=progress['census_id'] or row['seen']:
                    raise TransferError('migration_cancel_census_unknown_file')
                snapshot=json.loads(row['native_json']);info=snapshot['stat']
                if (any(info[name]!=entry[name] for name in ('bytes','device','inode','links','mtime_ns','ctime_ns')) or
                        stat.S_IMODE(info['mode'])!=entry['mode']):raise TransferError('migration_cancel_census_file_changed')
                if kind=='media':
                    path,metadata,encryption=await self.t.observer.media_snapshot(row['object_id'],conn=conn)
                    if path!=entry['path'] or digest(metadata)!=snapshot['metadata_sha256'] or digest(encryption)!=snapshot['encryption_sha256']:
                        raise TransferError('migration_cancel_census_metadata_changed')
                    object_kind=metadata['object_kind'];user_id=''
                else:
                    parts=entry['path'].split('/')
                    if len(parts)!=3 or parts[0]!=value['source_relationship'] or parts[2]!=row['object_id']+'.bin' or row['logical_id']!=row['object_id']:
                        raise TransferError('migration_cancel_census_metadata_changed')
                    object_kind='recording';user_id=parts[1]
                catalog=dict(logical_id=row['logical_id'],resource_kind=kind,object_id=row['object_id'],physical_path=entry['path'],
                    bytes=entry['bytes'],object_kind=object_kind,user_id=user_id,encryption_sha256=snapshot['encryption_sha256'])
                if row['expected_bytes']!=entry['bytes'] or p.canonical(catalog).decode()!=row['catalog_json']:
                    raise TransferError('migration_cancel_census_metadata_changed')
                await conn.execute(update(c.snapshots).where(c.snapshots.c.task_id==value['task_id'],c.snapshots.c.logical_id==row['logical_id']).values(seen=1))
                progress={**progress,'physical_objects':progress['physical_objects']+1,'physical_bytes':progress['physical_bytes']+entry['bytes'],
                    'physical_sha256':digest([progress['physical_sha256'],kind,entry['path'],snapshot])}
            if finished:
                if ((progress['physical_objects'],progress['physical_bytes'])!=(progress['retained_objects'],progress['retained_bytes']) or
                        await conn.scalar(text('SELECT COUNT(*) FROM media_objects'))!=progress['media_objects'] or
                        await conn.scalar(select(c.snapshots.c.logical_id).where(c.snapshots.c.task_id==value['task_id'],
                            c.snapshots.c.census_id==progress['census_id'],c.snapshots.c.seen==0).limit(1))):
                    raise TransferError('migration_cancel_census_current_roster_mismatch')
                progress={**progress,'phase':'verified'}
            await self._save(conn,value,header,progress)
        if finished:scan.completed=digest(progress)
        return progress

    def _progress(self,header,grant):
        control=header['payload']['transfer']
        if 'cancel_census' not in control:return None
        value=control['cancel_census']
        fields={'version','census_id','epoch','runner_id','phase','unit_after','unit_id','object_after','processed_objects','processed_bytes',
            'retained_objects','retained_bytes','media_objects','physical_objects','physical_bytes','catalog_sha256','physical_sha256','proof'}
        if (not isinstance(value,dict) or value.keys()!=fields or type(value['version']) is not int or value['version']!=1 or
                value['phase'] not in {'roster','physical','verified'} or
                any(type(value[name]) is not int or value[name]<0 for name in ('epoch','processed_objects','processed_bytes','retained_objects',
                    'retained_bytes','media_objects','physical_objects','physical_bytes')) or value['epoch']<1 or
                not value['physical_objects']<=value['retained_objects']<=value['processed_objects']<=header['expected_objects'] or
                not value['physical_bytes']<=value['retained_bytes']<=value['processed_bytes']<=header['expected_bytes'] or
                value['media_objects']>value['retained_objects'] or value['proof'] is not None and not isinstance(value['proof'],dict)):
            raise TransferError('migration_cancel_census_progress_changed')
        for name in ('census_id','runner_id'):identifier(value[name])
        for name in ('catalog_sha256','physical_sha256'):identifier(value[name],long=True)
        for name in ('unit_after','unit_id'):
            if value[name]!='':identifier(value[name],long=True)
        if value['object_after']!='' and (not isinstance(value['object_after'],str) or
                not (p.IDENTIFIER.fullmatch(value['object_after']) or p.OBJECT_ID.fullmatch(value['object_after']))):
            raise TransferError('migration_cancel_census_progress_changed')
        if value['proof'] is not None:
            historical=dict(grant,epoch=value['epoch'],runner_id=value['runner_id'])
            observed=value['proof'].get('payload',{}).get('observed_at')
            if type(observed) is not int:raise TransferError('migration_cancel_census_progress_changed')
            proof=verify_proof(value['proof'],historical,observed)
            if (value['phase']!='verified' or proof['census_id']!=value['census_id'] or
                    (proof['object_count'],proof['total_bytes'],proof['catalog_sha256'],proof['physical_sha256'])!=
                    (value['retained_objects'],value['retained_bytes'],value['catalog_sha256'],value['physical_sha256'])):
                raise TransferError('migration_cancel_census_progress_changed')
        return value

    async def advance(self,node,relation,request_id,grant):
        identifier(request_id)
        if self.t.source_lock.locked():raise TransferError('migration_cancel_census_source_busy')
        async with self.t.source_lock:
            try:
                value,_=await self.t._bind(grant,'source',expected='finalize',authenticated_relation=relation)
                async with self.store.database.begin() as conn:
                    header=await self._header(conn,value);progress=self._progress(header,value)
                scan=self.t.source_cancel_census
                if (scan is None or progress is None or self.t.wall()>=scan.deadline or
                        scan.key!=(value['task_id'],progress['census_id'],value['epoch'],value['runner_id']) or
                        progress['phase']=='verified' and progress['proof'] is not None and self.t.wall()>=progress['proof'].get('payload',{}).get('expires_at',0)):
                    progress=await self._start(value)
                else:
                    scan.deadline=self.t.wall()+300
                    await roots_check(self.t,header)
                    if progress['phase']=='roster':progress=await self._roster(value,progress)
                    elif progress['phase']=='physical':progress=await self._physical(value,progress)
                    elif progress['phase']!='verified':raise TransferError('migration_cancel_census_progress_changed')
                proof=progress['proof']
                if progress['phase']=='verified' and proof is None:
                    if self.t.source_cancel_census.completed!=digest(progress):raise TransferError('migration_cancel_census_physical_proof_required')
                    await roots_check(self.t,header)
                    proof=p.sign(self.store.unseal(node['private_key']),dict(format='frontiercloud-python-source-cancel-census',version=1,
                        **{name:value[name] for name in ('task_id','master_id','source_id','target_id','source_relationship','target_relationship',
                            'plan_sha256','units_sha256','source_binding','runner_id')},node_id=value['source_id'],epoch=value['epoch'],
                        census_id=progress['census_id'],object_count=progress['retained_objects'],total_bytes=progress['retained_bytes'],
                        catalog_sha256=progress['catalog_sha256'],physical_sha256=progress['physical_sha256'],
                        roots_sha256=digest(header['payload']['transfer']['source_roots']),observed_at=int(self.t.wall()),expires_at=value['expires_at'],
                        native_usage_reconciled=False,source_files_removed=False,source_drain_released=False,task_completed=False))
                    async with self.store.database.begin() as conn:
                        current=await self._header(conn,value)
                        if self._progress(current,value)!=progress:raise TransferError('migration_cancel_census_progress_changed')
                        progress={**progress,'proof':proof};await self._save(conn,value,current,progress)
                return dict(format='frontiercloud-python-source-cancel-census-progress',version=1,request_id=request_id,
                    node_id=value['source_id'],task_id=value['task_id'],epoch=value['epoch'],expires_at=value['expires_at'],
                    phase=progress['phase'],processed_objects=progress['processed_objects'],retained_objects=progress['retained_objects'],
                    retained_bytes=progress['retained_bytes'],proof=proof,native_usage_reconciled=False,source_drain_released=False,task_completed=False)
            except BaseException:
                if self.t.source_cancel_census is not None:
                    await joined_io(self.t.source_cancel_census.close);self.t.source_cancel_census=None
                raise


async def roots_check(transfers,header):
    await joined_io(roots.check,transfers.root_provider(),header['payload']['transfer']['source_roots'])
