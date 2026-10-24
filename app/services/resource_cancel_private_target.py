"""Cancel a private parent containing partial, verified and never-started copies."""
import hashlib
import json

from sqlalchemy import insert,select,update

from app.core.joined_io import joined_io
from app.services import resource_cancel_files as removed,resource_cancel_private_files as files
from app.services import resource_migration_schema as s,resource_transfer_files as staging
from app.services.federation import protocol as p
from app.services.media_manager import media_mutation_lock
from app.services.resource_cancel_private_receipts import COUNTS,FORMAT,progress as validate_progress,verify
from app.services.resource_cancel_source import verify as verify_source
from app.services.resource_cancel_target import IDENTITIES,TargetCancellation,columns,decoded,selected
from app.services.resource_migration_planning import extend
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import TransferError,identifier,immutable,immutable_shape


class PrivateTargetCancellation(TargetCancellation):
    @staticmethod
    def _private(unit):
        if unit['state'] not in {'pending','copying','verified','cancelled'} or 'ready' in unit['publication']:
            raise TransferError('migration_cancel_private_unit_required')

    def _progress(self,progress,value,unit,master_key):
        return validate_progress(progress,value,selected(unit,value),master_key,self.t.wall())

    async def _object(self,conn,grant,unit,logical,*,lock=True):
        self._private(unit)
        binding=decoded(s.peer_bindings,(await conn.execute(select(*columns(s.peer_bindings)).where(
            s.peer_bindings.c.task_id==grant['task_id'],s.peer_bindings.c.logical_id==logical))).mappings().first())
        query=select(*columns(s.storage_journal)).where(s.storage_journal.c.task_id==grant['task_id'],s.storage_journal.c.logical_id==logical)
        entry=decoded(s.storage_journal,(await conn.execute(query.with_for_update() if lock else query)).mappings().first())
        try:
            if binding is None:raise ValueError()
            raw=json.loads(binding['binding_json']);immutable_shape(raw);obj=raw['object']
            if (digest(raw)!=binding['binding_sha256'] or any(raw[name]!=item for name,item in immutable(grant).items() if name!='object') or
                    obj['logical_id']!=logical or binding['unit_id']!=obj['unit_id'] or binding['source_path']!=obj['source_path'] or
                    obj['unit_id']!=unit['unit_id'] or obj['unit_version']!=unit['source_version'] or obj['resource_kind']!=unit['resource_kind'] or
                    obj['target_path'].rsplit('/',1)[0]!=unit['parent_path']):raise ValueError()
            if entry is None:return None,obj
            expected=dict(task_id=grant['task_id'],master_id=grant['master_id'],source_id=grant['source_id'],target_id=grant['target_id'],
                logical_id=logical,unit_id=obj['unit_id'],resource_kind=obj['resource_kind'],object_id=obj['source_object'],
                physical_path=obj['target_path'],source_version=obj['source_version'],expected_bytes=obj['expected_bytes'])
            if (any(entry[name]!=item for name,item in expected.items()) or entry['publication_phase']!=0 or
                    entry['state'] not in {'receiving','verified','aborting','aborted'} or entry['sha256'] not in {'',obj['sha256']} or
                    set(entry['publication'])-{'verified_native','private_cancel'}):raise ValueError()
            if entry['sha256']:
                native=entry['publication'].get('verified_native')
                if not isinstance(native,dict) or native.get('bytes')!=obj['expected_bytes']:raise ValueError()
            elif 'verified_native' in entry['publication']:raise ValueError()
            if entry['state']=='verified' and not entry['sha256'] or entry['state']=='receiving' and entry['sha256']:raise ValueError()
            if entry['state'] in {'aborting','aborted'}:
                marker=entry['publication'].get('private_cancel')
                if not isinstance(marker,dict) or marker.keys()!={'intent'} or digest(marker['intent'])!=entry['cleanup_version']:raise ValueError()
                intent=marker['intent']
                if not isinstance(intent,dict):raise ValueError()
                actual=intent.get('stage')
                if intent.get('present') is True:
                    if not isinstance(actual,dict) or entry['stage_identity']!=actual.get('identity'):raise ValueError()
                    if entry['sha256'] and actual.get('version')!=digest(entry['publication']['verified_native']):raise ValueError()
                elif intent.get('present') is not False or entry['stage_identity']:raise ValueError()
            elif 'private_cancel' in entry['publication'] or entry['cleanup_version']:raise ValueError()
            return entry,obj
        except (p.ProtocolError,ValueError,TypeError,KeyError,RecursionError):raise TransferError('migration_cancel_private_object_changed') from None

    async def _roster(self,node,relation,value,unit):
        key=('private-cancel',value['task_id'],unit['unit_id'],value['epoch'],value['runner_id'])
        record=self.t.publication_roster
        if record is None or record.get('key')!=key:record=dict(key=key,after='',count=0,bytes=0,checksum=hashlib.sha256())
        async with self.store.database.begin() as conn:
            await self._unit(conn,node,relation,value,unit['unit_id'],control=True)
            logicals=list((await conn.execute(select(s.peer_bindings.c.logical_id).where(s.peer_bindings.c.task_id==value['task_id'],
                s.peer_bindings.c.unit_id==unit['unit_id'],s.peer_bindings.c.logical_id>record['after'])
                .order_by(s.peer_bindings.c.logical_id).limit(16))).scalars())
            for logical in logicals:
                entry,obj=await self._object(conn,value,unit,logical)
                if entry is not None and entry['state'] not in {'receiving','verified'}:raise TransferError('migration_cancel_private_roster_changed')
                extend(record['checksum'],dict(logical_id=logical,source_object=obj['source_object'],source_sha256=obj['sha256'],source_version=obj['source_version']))
                record['after']=logical;record['count']+=1;record['bytes']+=obj['expected_bytes']
        self.t._alive(value)
        if record['count']>unit['object_count'] or record['bytes']>unit['total_bytes']:raise TransferError('migration_cancel_private_roster_changed')
        self.t.publication_roster=record
        if logicals:return unit
        if (record['count'],record['bytes'],record['checksum'].hexdigest())!=(unit['object_count'],unit['total_bytes'],unit['source_version']):
            raise TransferError('migration_cancel_private_roster_changed')
        self.t.publication_roster=None
        return await self._save(node,relation,value,unit,step='objects')

    async def advance(self,node,relation,request_id,grant,unit_id,proof):
        identifier(request_id);value=self._signed(node,relation,grant)
        async with self.store.database.begin() as conn:unit,root=await self._unit(conn,node,relation,value,unit_id)
        self._private(unit)
        if unit['state']!='cancelled':
            if not isinstance(proof,dict) or proof.keys()!={'authorization','source_reply'}:raise TransferError('migration_cancel_source_proof_invalid')
            verify_source(proof['source_reply'],relation['peer_key'],proof['authorization'],value,selected(unit,value),self.t.wall())
        value,_=await self.t._bind(grant,'target',expected='cancel',authenticated_relation=relation)
        if self.t.target_lock.locked():return self._reply(request_id,value,unit,'waiting_writer')
        async with self.t.target_lock:
            async with media_mutation_lock,self.store.database.begin() as conn:
                unit,root=await self._unit(conn,node,relation,value,unit_id,control=True)
                if unit['state']=='cancelled':
                    await self._recorded(unit,value,relation['peer_key'],root)
                    return self._reply(request_id,value,unit,'cancelled')
                self._live(value,proof)
                progress=unit['publication'].get('target_cancel')
                if progress is None:
                    progress=dict(version=2,step='roster',started_at=int(self.t.wall()),after='',roster_sha256=digest([]),
                        initial_proof=proof,**{name:0 for name in COUNTS})
                else:self._progress(progress,value,unit,relation['peer_key'])
                progress={**progress,'final_proof':proof}
                unit={**unit,'publication':{**unit['publication'],'target_cancel':progress}}
                await conn.execute(update(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit_id).values(publication=unit['publication']))
                waiting=await self._clear(conn,value)
                if waiting:return self._reply(request_id,value,unit,waiting)
            if progress['step']=='roster':
                unit=await self._roster(node,relation,value,unit)
                return self._reply(request_id,value,unit,unit['publication']['target_cancel']['step'])
            physical=self.t.root_provider()['media' if unit['resource_kind']=='media' else 'recordings']
            if progress['step']=='objects':
                async with self.store.database.begin() as conn:
                    logical=await conn.scalar(select(s.peer_bindings.c.logical_id).where(s.peer_bindings.c.task_id==value['task_id'],
                        s.peer_bindings.c.unit_id==unit_id,s.peer_bindings.c.logical_id>progress['after']).order_by(s.peer_bindings.c.logical_id).limit(1))
                if logical:
                    await self._file(node,relation,value,unit,root,physical,logical,proof)
                    async with self.store.database.begin() as conn:unit,_=await self._unit(conn,node,relation,value,unit_id,control=True)
                    return self._reply(request_id,value,unit,'objects',processed=1)
                if (progress['processed_count'],progress['processed_bytes'])!=(unit['object_count'],unit['total_bytes']):
                    raise TransferError('migration_cancel_private_roster_changed')
                unit=await self._save(node,relation,value,unit,step='directory')
                return self._reply(request_id,value,unit,'directory')
            if progress['step']=='directory':
                self._live(value,proof)
                await joined_io(staging.remove_unit,physical,value['task_id'],unit_id,root)
                self._live(value,proof)
                unit=await self._save(node,relation,value,unit,step='receipt')
                return self._reply(request_id,value,unit,'receipt')
            return await self._finish(node,relation,request_id,value,unit,root,proof)

    async def _file(self,node,relation,value,unit,root,physical,logical,proof):
        async with self.store.database.begin() as conn:
            current,_=await self._unit(conn,node,relation,value,unit['unit_id'],control=True)
            entry,obj=await self._object(conn,value,current,logical)
            if entry is not None:await self._metadata(conn,entry,obj)
            else:await self._metadata(conn,dict(publication_phase=0),obj)
        observed=entry
        if entry is None or entry['state']!='aborting':
            if entry is not None and entry['state']=='aborted':raise TransferError('migration_cancel_private_journal_changed')
            intent=await joined_io(files.inspect,physical,value['task_id'],unit,obj,root,'' if entry is None else entry['stage_identity'])
            if entry is None and intent['present']:raise TransferError('migration_cancel_private_unjournalled_file')
            if entry is not None and entry['sha256'] and (not intent['present'] or intent['stage']['version']!=digest(entry['publication']['verified_native'])):
                raise TransferError('migration_cancel_private_stage_changed')
            self._live(value,proof)
            async with media_mutation_lock,self.store.database.begin() as conn:
                current,_=await self._unit(conn,node,relation,value,unit['unit_id'],control=True)
                actual,_=await self._object(conn,value,current,logical)
                if actual!=observed or current['publication']!=unit['publication']:raise TransferError('migration_cancel_private_journal_changed')
                await self._metadata(conn,dict(publication_phase=0),obj)
                if await self._clear(conn,value):raise TransferError('migration_cancel_reader_changed')
                if entry is None:
                    await conn.execute(insert(s.storage_journal).values(task_id=value['task_id'],logical_id=logical,
                        unit_id=obj['unit_id'],master_id=value['master_id'],source_id=value['source_id'],target_id=value['target_id'],
                        resource_kind=obj['resource_kind'],object_id=obj['source_object'],physical_path=obj['target_path'],
                        source_version=obj['source_version'],expected_bytes=obj['expected_bytes'],state='receiving',publication={},updated_at=int(self.t.wall())))
                    entry,_=await self._object(conn,value,current,logical)
                publication={**entry['publication'],'private_cancel':dict(intent=intent)}
                identity=intent['stage']['identity'] if intent['present'] else ''
                await conn.execute(update(s.storage_journal).where(s.storage_journal.c.task_id==value['task_id'],s.storage_journal.c.logical_id==logical)
                    .values(state='aborting',stage_identity=identity,cleanup_version=digest(intent),publication=publication))
                await self.store.log(conn,'migration_private_cancel_intent',value['master_id'],task_id=value['task_id'],unit_id=unit['unit_id'],logical_id=logical)
                entry={**entry,'state':'aborting','stage_identity':identity,'cleanup_version':digest(intent),'publication':publication}
        intent=entry['publication']['private_cancel']['intent'];counts=files.validate(intent,value['task_id'],unit,obj,root)
        self._live(value,proof);await joined_io(files.unlink,physical,value['task_id'],unit,obj,root,intent);self._live(value,proof)
        async with media_mutation_lock,self.store.database.begin() as conn:
            current,_=await self._unit(conn,node,relation,value,unit['unit_id'],control=True)
            actual,_=await self._object(conn,value,current,logical)
            if actual!=entry or current['publication']!=unit['publication']:raise TransferError('migration_cancel_private_journal_changed')
            await self._metadata(conn,actual,obj)
            progress=current['publication']['target_cancel']
            progress={**progress,'after':logical,'processed_count':progress['processed_count']+1,
                'processed_bytes':progress['processed_bytes']+obj['expected_bytes'],
                **{name:progress[name]+counts[name] for name in counts},
                'roster_sha256':digest([progress['roster_sha256'],logical,entry['cleanup_version']])}
            await conn.execute(update(s.storage_journal).where(s.storage_journal.c.task_id==value['task_id'],s.storage_journal.c.logical_id==logical)
                .values(state='aborted',updated_at=int(self.t.wall())))
            await conn.execute(update(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit['unit_id'])
                .values(publication={**current['publication'],'target_cancel':progress}))
            await self.store.log(conn,'migration_private_copy_cancelled',value['master_id'],task_id=value['task_id'],unit_id=unit['unit_id'],logical_id=logical,**counts)

    async def _recorded(self,unit,value,master_key,root):
        self._private(unit);progress=unit['publication'].get('target_cancel')
        self._progress(progress,value,unit,master_key)
        if (progress['step']!='receipt' or (progress.get('receipt_count'),progress.get('receipt_bytes'),progress.get('receipt_roster'))!=
                (unit['object_count'],unit['total_bytes'],progress['roster_sha256']) or
                any(progress.get('receipt_'+name)!=progress[name] for name in ('deleted_count','deleted_bytes','absent_count'))):
            raise TransferError('migration_cancel_private_journal_changed')
        result=verify(progress.get('receipt'),value['target_key'],master_key,value,selected(unit,value),self.t.wall(),proof=progress.get('final_proof'))
        if (result['root_sha256']!=digest(root) or result['initial_proof_sha256']!=digest(progress['initial_proof']) or
                result['roster_sha256']!=progress['roster_sha256'] or any(result[name]!=progress[name] for name in COUNTS)):
            raise TransferError('migration_cancel_private_journal_changed')
        # Read-only replay checks the pinned task root, never invokes unlink.
        physical=self.t.root_provider()['media' if unit['resource_kind']=='media' else 'recordings']
        async with self.store.database.connect() as conn:
            logical=await conn.scalar(select(s.peer_bindings.c.logical_id).where(s.peer_bindings.c.task_id==value['task_id'],s.peer_bindings.c.unit_id==unit['unit_id']).limit(1))
            _,obj=await self._object(conn,value,unit,logical,lock=False)
        await joined_io(removed.verify_removed,physical,value['task_id'],unit,obj,root)
        return progress['receipt']

    async def _finish(self,node,relation,request_id,value,unit,root,proof):
        progress=unit['publication']['target_cancel'];count=progress.get('receipt_count',0);size=progress.get('receipt_bytes',0)
        totals={name:progress.get('receipt_'+name,0) for name in ('deleted_count','deleted_bytes','absent_count')}
        roster=progress.get('receipt_roster',digest([]));after=progress.get('receipt_after','')
        self._live(value,proof)
        async with self.store.database.begin() as conn:
            await self._unit(conn,node,relation,value,unit['unit_id'],control=True)
            logicals=list((await conn.execute(select(s.peer_bindings.c.logical_id).where(s.peer_bindings.c.task_id==value['task_id'],
                s.peer_bindings.c.unit_id==unit['unit_id'],s.peer_bindings.c.logical_id>after).order_by(s.peer_bindings.c.logical_id).limit(16))).scalars())
            for logical in logicals:
                entry,obj=await self._object(conn,value,unit,logical)
                if entry is None or entry['state']!='aborted':raise TransferError('migration_cancel_private_journal_changed')
                intent=entry['publication']['private_cancel']['intent'];actual=files.validate(intent,value['task_id'],unit,obj,root)
                await self._metadata(conn,entry,obj,deleted=True)
                count+=1;size+=obj['expected_bytes'];after=logical;roster=digest([roster,logical,entry['cleanup_version']])
                totals={name:totals[name]+actual[name] for name in totals}
        if logicals:
            self._live(value,proof)
            await self._save(node,relation,value,unit,receipt_after=after,receipt_count=count,receipt_bytes=size,receipt_roster=roster,
                **{'receipt_'+name:item for name,item in totals.items()})
            return self._reply(request_id,value,unit,'receipt')
        async with self.store.database.begin() as conn:
            first=await conn.scalar(select(s.peer_bindings.c.logical_id).where(s.peer_bindings.c.task_id==value['task_id'],s.peer_bindings.c.unit_id==unit['unit_id']).limit(1))
            _,obj=await self._object(conn,value,unit,first)
        physical=self.t.root_provider()['media' if unit['resource_kind']=='media' else 'recordings']
        await joined_io(removed.verify_removed,physical,value['task_id'],unit,obj,root)
        async with media_mutation_lock,self.store.database.begin() as conn:
            current,root=await self._unit(conn,node,relation,value,unit['unit_id'],control=True);self._live(value,proof)
            progress=current['publication']['target_cancel']
            if ((count,size,roster)!=(unit['object_count'],unit['total_bytes'],progress['roster_sha256']) or
                    any(totals[name]!=progress[name] for name in totals)):raise TransferError('migration_cancel_private_journal_changed')
            if await self._clear(conn,value):raise TransferError('migration_cancel_reader_changed')
            chosen=selected(current,value)
            payload=dict(format=FORMAT,version=1,node_id=value['target_id'],**{name:value[name] for name in IDENTITIES},
                **{name:chosen[name] for name in ('unit_id','resource_kind','unit_version','object_count','total_bytes')},unit_sha256=digest(chosen),
                bindings_sha256=unit['source_version'],epoch=value['epoch'],started_at=progress['started_at'],completed_at=int(self.t.wall()),
                **{name:progress[name] for name in COUNTS},root_sha256=digest(root),roster_sha256=roster,
                initial_proof_sha256=digest(progress['initial_proof']),final_authorization_sha256=digest(proof['authorization']),
                final_source_reply=proof['source_reply'],source_files_removed=False,ownership_changed=False,capacity_settled=False)
            receipt=p.sign(self.store.unseal(node['private_key']),payload)
            verify(receipt,value['target_key'],relation['peer_key'],value,chosen,self.t.wall(),proof=proof)
            unit={**current,'state':'cancelled','publication':{**current['publication'],'target_cancel':{**progress,'receipt':receipt}}}
            await conn.execute(update(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit['unit_id'])
                .values(state='cancelled',publication=unit['publication']))
            await self.store.log(conn,'migration_private_parent_cancelled',value['master_id'],task_id=value['task_id'],unit_id=unit['unit_id'],receipt_sha256=digest(receipt))
        return self._reply(request_id,value,unit,'cancelled')
