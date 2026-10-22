"""Bounded native Target rollback; Source and Master business state are untouched.

The public post-publication cancel operation remains gated until mixed-task
settlement is integrated. This protocol never refunds quota or releases scopes.
"""
import hashlib
import json
import os
from functools import partial

from sqlalchemy import JSON,String,Text,cast,func,select,text,update

from app.core.joined_io import joined_io
from app.services import media_crypto,media_crypto_schema as crypto,resource_cancel_files as files
from app.services import resource_migration_schema as s,resource_publish_files as publication,resource_transfer_files as staging
from app.services.federation import protocol as p
from app.services.media_manager import media_mutation_lock
from app.services.resource_cancel_source import reply as source_reply,verify as verify_source
from app.services.resource_observation import digest
from app.services.resource_pool import validate_media_path
from app.services.resource_transfer_protocol import TransferError,grant_shape,identifier,immutable,object_shape,target_encryption_digest

RECEIPT='frontiercloud-python-target-shadow-cancelled'
IDENTITIES=('task_id','master_id','source_id','target_id','source_relationship','target_relationship','plan_sha256','units_sha256',
    'source_binding','target_binding','source_key','target_key')


def columns(table):
    result=[]
    for column in table.c:
        limit=65536 if isinstance(column.type,JSON) else (column.type.length or 65536) if isinstance(column.type,String) else None
        result.append(column if limit is None else func.substr(cast(column,Text),1,limit+1).label(column.name))
    return result


def decoded(table,row):
    if row is None:return None
    value=dict(row)
    try:
        for column in table.c:
            item=value[column.name]
            if item is None:continue
            if isinstance(column.type,(String,JSON)):
                limit=65536 if isinstance(column.type,JSON) else column.type.length or 65536
                if not isinstance(item,str):raise ValueError()
                size=len(item.encode()) if isinstance(column.type,JSON) or getattr(column.type,'length',None) is None else len(item)
                if size>limit:raise ValueError()
                if isinstance(column.type,JSON):
                    item=json.loads(item)
                    if not isinstance(item,dict):raise ValueError()
                    value[column.name]=item
        return value
    except (ValueError,TypeError,RecursionError,UnicodeError):raise TransferError('migration_cancel_metadata_invalid') from None


def selected(unit,grant):
    parent=unit['parent_path']
    return dict(unit_id=unit['unit_id'],resource_kind=unit['resource_kind'],source_parent=parent if unit['resource_kind']=='media' else
        grant['source_relationship']+'/'+parent.split('/')[-1],target_parent=parent,unit_version=unit['source_version'],
        object_count=unit['object_count'],total_bytes=unit['total_bytes'])


def verify_receipt(envelope,key,master_key,grant,unit,now,*,proof=None):
    raw=envelope.get('payload') if isinstance(envelope,dict) else None
    if isinstance(raw,dict) and raw.get('format')=='frontiercloud-python-target-private-cancelled':
        from app.services.resource_cancel_private_receipts import verify
        return verify(envelope,key,master_key,grant,unit,now,proof=proof)
    try:
        if not isinstance(envelope,dict) or envelope.keys()!={'payload','signature'}:raise ValueError()
        value=p.verify(key,envelope)
        expected=dict(format=RECEIPT,version=1,node_id=grant['target_id'],**{name:grant[name] for name in IDENTITIES},
            **{name:unit[name] for name in ('unit_id','resource_kind','unit_version','object_count','total_bytes')},unit_sha256=digest(unit),
            source_files_removed=False,ownership_changed=False,capacity_settled=False)
        if (not isinstance(value,dict) or value.keys()!={*expected,'epoch','started_at','completed_at','deleted_count','deleted_bytes',
                'root_sha256','roster_sha256','initial_proof_sha256','final_authorization_sha256','final_source_reply'} or any(value.get(name)!=item for name,item in expected.items()) or
                any(type(value[name]) is not int for name in ('version','epoch','started_at','completed_at','deleted_count','deleted_bytes')) or
                any(type(value[name]) is not bool for name in ('source_files_removed','ownership_changed','capacity_settled')) or
                not 1<=value['epoch']<=grant['epoch'] or not 0<value['started_at']<=value['completed_at']<=now+5 or
                (value['deleted_count'],value['deleted_bytes'])!=(unit['object_count'],unit['total_bytes'])):raise ValueError()
        for name in ('root_sha256','roster_sha256','initial_proof_sha256','final_authorization_sha256'):identifier(value[name],long=True)
        observed=source_reply(value['final_source_reply'],dict(grant,epoch=value['epoch']),unit,value['completed_at'])
        if observed['phase']!='verified' or observed['processed_objects']!=unit['object_count']:raise ValueError()
        if proof is not None:
            permission=proof['authorization']['payload']
            if (permission['epoch']!=value['epoch'] or digest(proof['authorization'])!=value['final_authorization_sha256'] or
                    proof['source_reply']!=value['final_source_reply']):raise ValueError()
            verify_source(proof['source_reply'],master_key,proof['authorization'],
                dict(grant,epoch=value['epoch'],runner_id=permission['runner_id']),unit,value['completed_at'])
        return value
    except (p.ProtocolError,ValueError,TypeError,KeyError,RecursionError):raise TransferError('migration_cancel_target_receipt_invalid') from None


class TargetCancellation:
    def __init__(self,transfers):self.t,self.store=transfers,transfers.store

    def _signed(self,node,relation,envelope):
        value=grant_shape(p.verify(relation['peer_key'],envelope),self.t.wall())
        if value['purpose']!='cancel' or node['node_id']!=value['target_id']:
            raise TransferError('migration_cancel_target_binding_invalid')
        return value

    async def _unit(self,conn,node,relation,grant,unit_id,*,control=False):
        identifier(unit_id,long=True)
        header=await self.t._control_row(conn,grant) if control else await self.t.native._row(conn,node,relation,grant['task_id'])
        unit=decoded(s.units,(await conn.execute(select(*columns(s.units)).where(s.units.c.task_id==grant['task_id'],
            s.units.c.unit_id==unit_id).with_for_update())).mappings().first())
        if unit is None or header['state']!='active' or header['side']!='target':raise TransferError('migration_cancel_target_unavailable')
        if (unit['state'] in {'committed','cleanup_pending','cleaned'} or unit['committed_at'] or unit['cleanup_after'] or
                unit['publication'].get('commit') or unit['publication'].get('commit_sha256')):
            raise TransferError('migration_cancel_native_commit_is_final')
        chosen=selected(unit,grant)
        self.t.native._unit(header,{('source_version' if name=='unit_version' else name):value for name,value in chosen.items()})
        scope=await conn.scalar(select(s.scopes.c.scope_id).where(s.scopes.c.task_id==grant['task_id'],
            s.scopes.c.member_id==grant['target_id'],s.scopes.c.resource_kind==unit['resource_kind'],s.scopes.c.parent_path==unit['parent_path']))
        if 'cancel_settled' in unit['publication'] or unit['native_acknowledged']:
            from app.services.resource_cancel_unit_receipts import recorded
            recorded(unit,grant,'target',self.t.wall())
            if scope:raise TransferError('migration_cancel_settlement_scope_changed')
        elif not scope:
            raise TransferError('migration_cancel_scope_missing')
        root=header['payload'].get('transfer',{}).get('staging_roots',{}).get(unit['resource_kind'])
        if not isinstance(root,dict):raise TransferError('migration_cancel_root_unproven')
        return unit,root

    async def _object(self,conn,grant,unit,logical):
        progress=unit['publication'].get('target_cancel')
        if isinstance(progress,dict) and progress.get('version')==2:
            from app.services.resource_cancel_private_target import PrivateTargetCancellation
            return await PrivateTargetCancellation(self.t)._object(conn,grant,unit,logical)
        entry=decoded(s.storage_journal,(await conn.execute(select(*columns(s.storage_journal)).where(s.storage_journal.c.task_id==grant['task_id'],
            s.storage_journal.c.logical_id==logical).with_for_update())).mappings().first())
        binding=decoded(s.peer_bindings,(await conn.execute(select(*columns(s.peer_bindings)).where(s.peer_bindings.c.task_id==grant['task_id'],
            s.peer_bindings.c.logical_id==logical))).mappings().first())
        try:
            if entry is None or binding is None:raise ValueError()
            raw=json.loads(binding['binding_json']);obj=raw['object']
            object_shape(dict(grant,purpose='copy',object=obj))
            if (raw.keys()!=immutable(grant).keys() or digest(raw)!=binding['binding_sha256'] or
                    any(raw[name]!=value for name,value in immutable(grant).items() if name!='object') or
                    binding['unit_id']!=unit['unit_id'] or binding['source_path']!=obj['source_path'] or obj['unit_version']!=unit['source_version'] or
                    obj['unit_id']!=unit['unit_id'] or obj['target_path'].rsplit('/',1)[0]!=unit['parent_path'] or
                    any(entry[name]!=obj[field] for name,field in (('logical_id','logical_id'),('unit_id','unit_id'),('resource_kind','resource_kind'),
                        ('object_id','source_object'),('physical_path','target_path'),('source_version','source_version'),('expected_bytes','expected_bytes'),('sha256','sha256'))) or
                    entry['state'] not in {'verified','publishing','published','aborting','aborted'} or
                    type(entry['publication_phase']) is not int or not 0<=entry['publication_phase']<=3):raise ValueError()
            return entry,obj
        except (p.ProtocolError,ValueError,TypeError,KeyError,RecursionError):raise TransferError('migration_cancel_object_changed') from None

    async def _metadata(self,conn,entry,obj,*,deleted=False):
        if obj['resource_kind']!='media':return
        path,kind=validate_media_path(obj['target_path']);locator=hashlib.sha256(path.encode()).hexdigest()
        rows=(await conn.execute(text('SELECT substr(media_id,1,65) AS media_id,substr(object_kind,1,17) AS object_kind,'
            'substr(media_path,1,1025) AS media_path,substr(path_locator,1,65) AS path_locator '
            'FROM media_objects WHERE media_id=:id OR path_locator=:locator LIMIT 2'),dict(id=obj['source_object'],locator=locator))).mappings().all()
        raw=await conn.scalar(select(func.substr(crypto.encryption.c.descriptor_json,1,65537)).where(
            crypto.encryption.c.object_kind=='media',crypto.encryption.c.object_id==obj['source_object']))
        if deleted or entry['publication_phase']<3:
            if rows or raw is not None:raise TransferError('migration_cancel_registry_changed')
            return
        if len(rows)!=1 or dict(rows[0])!=dict(media_id=obj['source_object'],object_kind=kind,media_path=path,path_locator=locator):
            raise TransferError('migration_cancel_registry_changed')
        try:
            if raw is not None and len(raw.encode())>65536:raise ValueError()
            descriptor=None if raw is None else media_crypto.Metadata.from_dict(json.loads(raw)).to_dict()
            expected=entry['publication'].get('encryption')
            if descriptor!=expected or digest(descriptor)!=target_encryption_digest(obj):raise ValueError()
        except (ValueError,TypeError,KeyError,AttributeError,RecursionError,UnicodeError):raise TransferError('migration_cancel_encryption_changed') from None

    async def _clear(self,conn,grant):
        traffic=self.t.traffic.snapshot()
        if traffic.get('can_run') is not True:return 'waiting_low_traffic'
        if traffic.get('readers_clear') is not True:return 'waiting_reader'
        if await conn.scalar(select(s.read_leases.c.lease_id).where(s.read_leases.c.member_id==grant['target_id'],
            s.read_leases.c.expires_at>self.t.wall()).limit(1)):return 'waiting_reader'
        return ''

    async def _close_scans(self,task_id):
        for name in ('publication_scan','target_verification'):
            scan=getattr(self.t,name)
            if scan is not None and scan.key[0]==task_id:
                await joined_io(scan.close);setattr(self.t,name,None)
        self.t.publication_roster=None;self.t.hash_state=None

    async def stop(self,node,relation,request_id,grant):
        identifier(request_id)
        value,_=await self.t._bind(grant,'target',expected='cancel',authenticated_relation=relation)
        phase='waiting_writer'
        if not self.t.target_lock.locked():
            async with self.t.target_lock:await self._close_scans(value['task_id']);phase='stopped'
        return dict(format='frontiercloud-python-target-cancel-stop',version=1,request_id=request_id,node_id=node['node_id'],
            task_id=value['task_id'],epoch=value['epoch'],phase=phase,expires_at=value['expires_at'],source_files_removed=False,capacity_settled=False)

    def _live(self,value,proof):
        self.t._alive(value)
        if self.t.wall()>=min(proof['authorization']['payload']['expires_at'],proof['source_reply']['payload']['expires_at']):
            raise TransferError('migration_cancel_source_proof_expired')

    def _reply(self,request_id,value,unit,phase,*,processed=0):
        progress=unit['publication'].get('target_cancel',{})
        return dict(format='frontiercloud-python-target-cancel',version=1,request_id=request_id,node_id=value['target_id'],task_id=value['task_id'],
            unit_id=unit['unit_id'],unit_version=unit['source_version'],epoch=value['epoch'],expires_at=value['expires_at'],phase=phase,
            processed_objects=processed,deleted_count=progress.get('deleted_count',0),deleted_bytes=progress.get('deleted_bytes',0),
            receipt=progress.get('receipt'),source_files_removed=False,capacity_settled=False)

    def _progress(self,progress,value,unit,master_key):
        if isinstance(progress,dict) and progress.get('version')==2:
            from app.services.resource_cancel_private_target import PrivateTargetCancellation
            return PrivateTargetCancellation(self.t)._progress(progress,value,unit,master_key)
        required={'version','step','started_at','deleted_count','deleted_bytes','roster_sha256','initial_proof'}
        optional={'final_proof','empty_ready','receipt_after','receipt_count','receipt_bytes','receipt_roster'}
        try:
            if (not isinstance(progress,dict) or not required<=progress.keys() or progress.keys()-required-optional or
                    type(progress['version']) is not int or progress['version']!=1 or progress['step'] not in {'objects','directory','receipt'} or
                    type(progress['started_at']) is not int or not 0<progress['started_at']<=self.t.wall()+5):raise ValueError()
            for name,maximum in (('deleted_count',unit['object_count']),('deleted_bytes',unit['total_bytes']),
                    ('receipt_count',unit['object_count']),('receipt_bytes',unit['total_bytes'])):
                item=progress.get(name,0)
                if type(item) is not int or not 0<=item<=maximum:raise ValueError()
            identifier(progress['roster_sha256'],long=True)
            if 'receipt_roster' in progress:identifier(progress['receipt_roster'],long=True)
            if progress.get('receipt_after'):identifier(progress['receipt_after'],long=unit['resource_kind']=='media')
            initial=progress['initial_proof']
            if not isinstance(initial,dict) or initial.keys()!={'authorization','source_reply'}:raise ValueError()
            permission=initial['authorization']['payload'];epoch=permission['epoch']
            if type(epoch) is not int or not 1<=epoch<=value['epoch']:raise ValueError()
            verify_source(initial['source_reply'],master_key,initial['authorization'],
                dict(value,epoch=epoch,runner_id=permission['runner_id']),selected(unit,value),progress['started_at'])
        except (p.ProtocolError,ValueError,TypeError,KeyError,RecursionError):raise TransferError('migration_cancel_journal_changed') from None

    async def advance(self,node,relation,request_id,grant,unit_id,proof):
        identifier(request_id);value=self._signed(node,relation,grant)
        async with self.store.database.begin() as conn:unit,expected_root=await self._unit(conn,node,relation,value,unit_id)
        progress=unit['publication'].get('target_cancel')
        private=isinstance(progress,dict) and progress.get('version')==2
        if progress is None and unit['state'] in {'pending','copying','verified'} and 'ready' not in unit['publication']:
            async with self.store.database.connect() as conn:
                count=await conn.scalar(select(func.count()).select_from(s.storage_journal).where(
                    s.storage_journal.c.task_id==value['task_id'],s.storage_journal.c.unit_id==unit_id))
                receiving=await conn.scalar(select(s.storage_journal.c.logical_id).where(s.storage_journal.c.task_id==value['task_id'],
                    s.storage_journal.c.unit_id==unit_id,s.storage_journal.c.state=='receiving').limit(1))
            private=count<unit['object_count'] or receiving is not None
        if private:
            from app.services.resource_cancel_private_target import PrivateTargetCancellation
            return await PrivateTargetCancellation(self.t).advance(node,relation,request_id,grant,unit_id,proof)
        if unit['state']!='cancelled':
            if not isinstance(proof,dict) or proof.keys()!={'authorization','source_reply'}:raise TransferError('migration_cancel_source_proof_invalid')
            verify_source(proof['source_reply'],relation['peer_key'],proof['authorization'],value,selected(unit,value),self.t.wall())
        value,_=await self.t._bind(grant,'target',expected='cancel',authenticated_relation=relation)
        if self.t.target_lock.locked():return self._reply(request_id,value,unit,'waiting_writer')
        async with self.t.target_lock:
            await self._close_scans(value['task_id'])
            async with media_mutation_lock,self.store.database.begin() as conn:
                unit,expected_root=await self._unit(conn,node,relation,value,unit_id,control=True)
                if unit['state']=='cancelled':
                    await self._recorded(unit,value,relation['peer_key'],expected_root)
                    return self._reply(request_id,value,unit,'cancelled')
                self._live(value,proof)
                progress=unit['publication'].get('target_cancel')
                if progress is None:
                    counts=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.storage_journal.c.expected_bytes),0)).where(
                        s.storage_journal.c.task_id==value['task_id'],s.storage_journal.c.unit_id==unit_id,
                        s.storage_journal.c.state.in_(['verified','publishing','published'])))).one()
                    if tuple(counts)!=(unit['object_count'],unit['total_bytes']):raise TransferError('migration_cancel_roster_incomplete')
                    progress=dict(version=1,step='objects',started_at=int(self.t.wall()),deleted_count=0,deleted_bytes=0,
                        roster_sha256=digest([]),initial_proof=proof)
                else:self._progress(progress,value,unit,relation['peer_key'])
                progress={**progress,'final_proof':proof}
                unit={**unit,'publication':{**unit['publication'],'target_cancel':progress}}
                await conn.execute(update(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit_id).values(publication=unit['publication']))
                waiting=await self._clear(conn,value)
                if waiting:return self._reply(request_id,value,unit,waiting)
            root=self.t.root_provider()['media' if unit['resource_kind']=='media' else 'recordings']
            if progress['step']=='objects':
                async with self.store.database.begin() as conn:
                    logical=await conn.scalar(select(s.storage_journal.c.logical_id).where(s.storage_journal.c.task_id==value['task_id'],
                        s.storage_journal.c.unit_id==unit_id,s.storage_journal.c.state!='aborted').order_by(s.storage_journal.c.logical_id).limit(1))
                if logical:
                    await self._file(node,relation,value,unit,expected_root,root,logical,proof)
                    async with self.store.database.begin() as conn:unit,_=await self._unit(conn,node,relation,value,unit_id,control=True)
                    return self._reply(request_id,value,unit,'objects',processed=1)
                unit=await self._save(node,relation,value,unit,step='directory')
                return self._reply(request_id,value,unit,'directory')
            if progress['step']=='directory':
                self._live(value,proof)
                if 'ready' in unit['publication']:
                    async with self.store.database.begin() as conn:
                        logical=await conn.scalar(select(s.storage_journal.c.logical_id).where(s.storage_journal.c.task_id==value['task_id'],
                            s.storage_journal.c.unit_id==unit_id).order_by(s.storage_journal.c.logical_id).limit(1))
                        _,obj=await self._object(conn,value,unit,logical)
                    await joined_io(files.remove_directory,root,value['task_id'],unit,obj,expected_root)
                else:
                    if 'empty_ready' not in progress:
                        intent=await joined_io(files.inspect_empty_ready,root,value['task_id'],unit_id,expected_root)
                        unit=await self._save(node,relation,value,unit,empty_ready=intent)
                        progress=unit['publication']['target_cancel']
                    if progress['empty_ready'] is not None:
                        await joined_io(files.remove_empty_ready,root,value['task_id'],unit_id,expected_root,progress['empty_ready'])
                await joined_io(staging.remove_unit,root,value['task_id'],unit_id,expected_root)
                self._live(value,proof)
                unit=await self._save(node,relation,value,unit,step='receipt')
                return self._reply(request_id,value,unit,'receipt')
            return await self._finish(node,relation,request_id,value,unit,expected_root,proof)

    async def _save(self,node,relation,value,unit,**fields):
        async with media_mutation_lock,self.store.database.begin() as conn:
            current,_=await self._unit(conn,node,relation,value,unit['unit_id'],control=True)
            if current['publication']!=unit['publication']:raise TransferError('migration_cancel_journal_changed')
            result={**current,'publication':{**current['publication'],'target_cancel':{**current['publication']['target_cancel'],**fields}}}
            await conn.execute(update(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit['unit_id']).values(publication=result['publication']))
            return result

    async def _file(self,node,relation,value,unit,expected_root,root,logical,proof):
        async with self.store.database.begin() as conn:
            current,_=await self._unit(conn,node,relation,value,unit['unit_id'],control=True)
            entry,obj=await self._object(conn,value,current,logical)
            await self._metadata(conn,entry,obj)
        observed=entry
        if entry['state']!='aborting':
            expected=entry['publication'].get('assembled_native') if entry['publication_phase']>=2 else entry['publication'].get('verified_native')
            if not isinstance(expected,dict):raise TransferError('migration_cancel_file_identity_missing')
            if entry['state']=='publishing' and entry['publication_phase']==1:
                expected=await joined_io(publication.assemble,root,value['task_id'],unit['unit_id'],obj,expected_root,unit['publication'],expected)
                entry={**entry,'publication_phase':2,'publication':{**entry['publication'],'assembled_native':expected}}
            self._live(value,proof)
            intent=await joined_io(partial(files.inspect_object,root,value['task_id'],unit,obj,expected_root,expected,
                assembled=entry['publication_phase']>=2))
            async with media_mutation_lock,self.store.database.begin() as conn:
                await self._unit(conn,node,relation,value,unit['unit_id'],control=True)
                actual,_=await self._object(conn,value,unit,logical)
                if actual!=observed:raise TransferError('migration_cancel_journal_changed')
                await self._metadata(conn,actual,obj)
                if await self._clear(conn,value):raise TransferError('migration_cancel_reader_changed')
                entry={**entry,'state':'aborting','cleanup_version':digest(intent),'publication':{**entry['publication'],'target_cancel':dict(intent=intent)}}
                await conn.execute(update(s.storage_journal).where(s.storage_journal.c.task_id==value['task_id'],s.storage_journal.c.logical_id==logical)
                    .values(state='aborting',publication_phase=entry['publication_phase'],publication=entry['publication'],cleanup_version=entry['cleanup_version']))
                await self.store.log(conn,'migration_target_cancel_intent',value['master_id'],task_id=value['task_id'],unit_id=unit['unit_id'],logical_id=logical)
        intent=entry['publication'].get('target_cancel',{}).get('intent')
        if not isinstance(intent,dict) or digest(intent)!=entry['cleanup_version']:raise TransferError('migration_cancel_intent_changed')
        self._live(value,proof)
        await joined_io(files.unlink_object,root,value['task_id'],unit,obj,expected_root,intent)
        self._live(value,proof)
        async with media_mutation_lock,self.store.database.begin() as conn:
            current,_=await self._unit(conn,node,relation,value,unit['unit_id'],control=True)
            actual,_=await self._object(conn,value,current,logical)
            if actual['state']!='aborting' or actual['cleanup_version']!=entry['cleanup_version'] or actual['publication']!=entry['publication']:
                raise TransferError('migration_cancel_journal_changed')
            await self._metadata(conn,actual,obj)
            if obj['resource_kind']=='media' and entry['publication_phase']==3:
                await conn.execute(text('DELETE FROM media_objects WHERE media_id=:id'),dict(id=obj['source_object']))
                await media_crypto.forget_encryption(conn,obj['source_object'])
            progress=current['publication']['target_cancel']
            progress={**progress,'deleted_count':progress['deleted_count']+1,'deleted_bytes':progress['deleted_bytes']+obj['expected_bytes'],
                'roster_sha256':digest([progress['roster_sha256'],logical,entry['cleanup_version']])}
            await conn.execute(update(s.storage_journal).where(s.storage_journal.c.task_id==value['task_id'],s.storage_journal.c.logical_id==logical)
                .values(state='aborted',updated_at=int(self.t.wall())))
            await conn.execute(update(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit['unit_id'])
                .values(publication={**current['publication'],'target_cancel':progress}))
            await self.store.log(conn,'migration_target_shadow_removed',value['master_id'],task_id=value['task_id'],unit_id=unit['unit_id'],logical_id=logical)

    async def _recorded(self,unit,grant,master_key,root):
        progress=unit['publication'].get('target_cancel')
        if isinstance(progress,dict) and progress.get('version')==2:
            from app.services.resource_cancel_private_target import PrivateTargetCancellation
            return await PrivateTargetCancellation(self.t)._recorded(unit,grant,master_key,root)
        if (not isinstance(progress,dict) or not isinstance(progress.get('final_proof'),dict) or
                progress['final_proof'].keys()!={'authorization','source_reply'}):
            raise TransferError('migration_cancel_journal_changed')
        self._progress({name:value for name,value in progress.items() if name!='receipt'},grant,unit,master_key)
        if (progress['step']!='receipt' or (progress.get('receipt_count'),progress.get('receipt_bytes'),progress.get('receipt_roster'))!=
                (unit['object_count'],unit['total_bytes'],progress['roster_sha256'])):
            raise TransferError('migration_cancel_journal_changed')
        receipt=progress.get('receipt')
        result=verify_receipt(receipt,grant['target_key'],master_key,grant,selected(unit,grant),self.t.wall(),proof=progress['final_proof'])
        if (result['root_sha256']!=digest(root) or result['initial_proof_sha256']!=digest(progress.get('initial_proof')) or
                (result['deleted_count'],result['deleted_bytes'],result['roster_sha256'])!=(progress.get('deleted_count'),progress.get('deleted_bytes'),progress.get('roster_sha256'))):
            raise TransferError('migration_cancel_root_or_journal_changed')
        physical=self.t.root_provider()['media' if unit['resource_kind']=='media' else 'recordings']
        descriptor=await joined_io(partial(staging._directory,physical,grant['task_id'],expected_root=root,create=False),on_cancel=os.close)
        os.close(descriptor)
        return receipt

    async def _finish(self,node,relation,request_id,value,unit,root,proof):
        # Exact final roster is paged rather than materialized. No receipt is
        # produced until every owned unlink and native registry change agrees.
        progress=unit['publication']['target_cancel']
        count=progress.get('receipt_count',0);size=progress.get('receipt_bytes',0)
        roster=progress.get('receipt_roster',digest([]));after=progress.get('receipt_after','')
        self._live(value,proof)
        async with self.store.database.begin() as conn:
            await self._unit(conn,node,relation,value,unit['unit_id'],control=True)
            logicals=list((await conn.execute(select(s.storage_journal.c.logical_id).where(s.storage_journal.c.task_id==value['task_id'],
                s.storage_journal.c.unit_id==unit['unit_id'],s.storage_journal.c.logical_id>after).order_by(s.storage_journal.c.logical_id).limit(16))).scalars())
            for logical in logicals:
                entry,obj=await self._object(conn,value,unit,logical)
                if entry['state']!='aborted' or digest(entry['publication'].get('target_cancel',{}).get('intent'))!=entry['cleanup_version']:
                    raise TransferError('migration_cancel_roster_changed')
                await self._metadata(conn,entry,obj,deleted=True)
                count+=1;size+=obj['expected_bytes'];roster=digest([roster,logical,entry['cleanup_version']]);after=logical
        if logicals:
            self._live(value,proof)
            unit=await self._save(node,relation,value,unit,receipt_after=after,receipt_count=count,receipt_bytes=size,receipt_roster=roster)
            return self._reply(request_id,value,unit,'receipt')
        async with self.store.database.begin() as conn:
            first=await conn.scalar(select(s.storage_journal.c.logical_id).where(s.storage_journal.c.task_id==value['task_id'],
                s.storage_journal.c.unit_id==unit['unit_id']).order_by(s.storage_journal.c.logical_id).limit(1))
            _,obj=await self._object(conn,value,unit,first)
        physical_root=self.t.root_provider()['media' if unit['resource_kind']=='media' else 'recordings']
        await joined_io(files.verify_removed,physical_root,value['task_id'],unit,obj,root)
        async with media_mutation_lock,self.store.database.begin() as conn:
            current,root=await self._unit(conn,node,relation,value,unit['unit_id'],control=True);self._live(value,proof)
            progress=current['publication']['target_cancel']
            if (count,size,roster)!=(current['object_count'],current['total_bytes'],progress['roster_sha256']) or (count,size)!=(progress['deleted_count'],progress['deleted_bytes']):
                raise TransferError('migration_cancel_roster_changed')
            if await self._clear(conn,value):raise TransferError('migration_cancel_reader_changed')
            chosen=selected(current,value)
            payload=dict(format=RECEIPT,version=1,node_id=value['target_id'],**{name:value[name] for name in IDENTITIES},
                **{name:chosen[name] for name in ('unit_id','resource_kind','unit_version','object_count','total_bytes')},unit_sha256=digest(chosen),
                epoch=value['epoch'],started_at=progress['started_at'],completed_at=int(self.t.wall()),deleted_count=count,deleted_bytes=size,
                root_sha256=digest(root),roster_sha256=roster,initial_proof_sha256=digest(progress['initial_proof']),
                final_authorization_sha256=digest(proof['authorization']),final_source_reply=proof['source_reply'],
                source_files_removed=False,ownership_changed=False,capacity_settled=False)
            envelope=p.sign(self.store.unseal(node['private_key']),payload)
            verify_receipt(envelope,value['target_key'],relation['peer_key'],value,selected(current,value),self.t.wall(),proof=proof)
            result={**current,'state':'cancelled','publication':{**current['publication'],'target_cancel':{**progress,'receipt':envelope}}}
            await conn.execute(update(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit['unit_id'])
                .values(state='cancelled',publication=result['publication']))
            await self.store.log(conn,'migration_target_unit_cancelled',value['master_id'],task_id=value['task_id'],unit_id=unit['unit_id'],receipt_sha256=digest(envelope))
        return self._reply(request_id,value,result,'cancelled')
