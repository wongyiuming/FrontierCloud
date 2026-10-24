"""Task-wide native census and release of only the successful task's fences."""
from sqlalchemy import delete,func,select,text,update

from app.core.joined_io import joined_io
from app.services import karaoke_schema as ks,resource_final_roots as roots,resource_migration_schema as s,resource_transfer_files as staging
from app.services.federation import protocol as p,schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_final_receipts import verify as verify_receipt
from app.services.resource_observation import digest
from app.services.resource_target_verify import TargetVerification,live_proof
from app.services.resource_transfer_protocol import TransferError,identifier


class FinalizePeers:
    def __init__(self,transfers):self.t,self.store=transfers,transfers.store

    async def _row(self,conn,node,relation,value,side):
        header=await self.t.native._row(conn,node,relation,value['task_id'])
        if header['side']!=side or header['state'] not in {'active','released'}:
            raise TransferError('migration_finalize_native_state_changed')
        if header['state']=='released':
            if self.t.wall()>=value['expires_at'] or self.t.epochs.get(value['task_id'])!=(value['epoch'],value['runner_id'],'finalize'):
                raise TransferError('migration_transfer_window_expired')
            receipt=header['payload'].get('final_receipt')
            verify_receipt(receipt,value,side,self._counts(header),self.t.wall(),
                source_receipt=header['payload'].get('source_final_receipt'))
            return header
        current=await self.t._control_row(conn,value)
        if header['payload']!=current['payload']:raise TransferError('migration_finalize_native_state_changed')
        states=['cleaned'] if side=='source' else ['committed']
        counts=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.units.c.object_count),0),
            func.coalesce(func.sum(s.units.c.total_bytes),0)).where(s.units.c.task_id==value['task_id'],s.units.c.state.in_(states)))).one()
        total=await conn.scalar(select(func.count()).select_from(s.units).where(s.units.c.task_id==value['task_id']))
        journals=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.storage_journal.c.expected_bytes),0)).where(
            s.storage_journal.c.task_id==value['task_id'],s.storage_journal.c.state==('deleted' if side=='source' else 'published')))).one()
        journal_total=await conn.scalar(select(func.count()).select_from(s.storage_journal).where(s.storage_journal.c.task_id==value['task_id']))
        scopes=await conn.scalar(select(func.count()).select_from(s.scopes.join(s.units,
            (s.scopes.c.task_id==s.units.c.task_id)&(s.scopes.c.resource_kind==s.units.c.resource_kind)&
            (s.scopes.c.parent_path==s.units.c.parent_path))).where(s.scopes.c.task_id==value['task_id'],s.scopes.c.member_id==node['node_id']))
        own_scopes=await conn.scalar(select(func.count()).select_from(s.scopes).where(s.scopes.c.task_id==value['task_id']))
        if (tuple(counts)!=self._counts(header) or total!=header['expected_units'] or
                tuple(journals)!=(header['expected_objects'],header['expected_bytes']) or journal_total!=header['expected_objects'] or
                scopes!=header['expected_units'] or own_scopes!=header['expected_units']):
            raise TransferError('migration_finalize_native_work_incomplete')
        if side=='target':
            member=(await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id==node['node_id']).with_for_update())).mappings().one()
            if member['used_bytes']<header['expected_bytes'] or member['used_bytes']+member['reserved_bytes']>member['allocated_bytes']:
                raise TransferError('migration_finalize_target_usage_inconsistent')
        return header

    @staticmethod
    def _counts(header):return header['expected_units'],header['expected_objects'],header['expected_bytes']

    def _reply(self,request_id,value,side,phase,receipt=None):
        return dict(format='frontiercloud-python-migration-finalization',version=1,request_id=request_id,node_id=value[side+'_id'],
            task_id=value['task_id'],side=side,epoch=value['epoch'],expires_at=value['expires_at'],phase=phase,receipt=receipt)

    async def _source_quiet(self,conn,value):
        snapshot=self.t.traffic.snapshot()
        if snapshot.get('can_run') is not True:raise TransferError('migration_finalize_waiting_low_traffic')
        if snapshot.get('readers_clear') is not True or await conn.scalar(select(s.read_leases.c.lease_id).where(
                s.read_leases.c.member_id==value['source_id'],s.read_leases.c.expires_at>int(self.t.wall())).limit(1)):
            raise TransferError('migration_finalize_waiting_reader')
        member=(await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id==value['source_id']).with_for_update())).mappings().one()
        if member['reserved_bytes'] or await conn.scalar(select(fs.upload_sessions.c.upload_id).where(fs.upload_sessions.c.state=='reserved').limit(1)):
            raise TransferError('migration_finalize_source_writer_pending')
        for statement in ('SELECT media_id FROM media_objects LIMIT 1',
                'SELECT operation_id FROM media_delete_operations LIMIT 1'):
            if await conn.scalar(text(statement)):raise TransferError('migration_finalize_source_registry_remaining')
        if (await conn.scalar(select(fs.global_media.c.media_id).where(fs.global_media.c.state=='active').limit(1)) or
                await conn.scalar(select(ks.recordings.c.recording_id).where(ks.recordings.c.storage_member_id==value['source_id']).limit(1))):
            raise TransferError('migration_finalize_source_registry_remaining')
        for table,condition in ((s.peer_reservations,s.peer_reservations.c.state.not_in(['aborted','released'])),
                (s.capacity,True),(s.scopes,True),(s.drains,True),
                (s.storage_journal,s.storage_journal.c.state.in_(['receiving','verified','publishing','deleting','aborting']))):
            owner=table.c.owner_id if table is s.drains else table.c.task_id
            if await conn.scalar(select(next(iter(table.primary_key.columns))).where(owner!=value['task_id'],condition).limit(1)):
                raise TransferError('migration_finalize_source_foreign_work')
        return dict(member)

    async def _release(self,conn,node,value,header,census,*,source_receipt=None):
        side=header['side'];now=int(self.t.wall())
        member=(await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id==node['node_id']).with_for_update())).mappings().one()
        record=dict(format='frontiercloud-python-migration-final-receipt',version=1,side=side,node_id=node['node_id'],
            **{name:value[name] for name in ('task_id','master_id','source_id','target_id','source_relationship',
                'target_relationship','plan_sha256','units_sha256')},binding_sha256=value[side+'_binding'],
            epoch=value['epoch'],unit_count=header['expected_units'],object_count=header['expected_objects'],total_bytes=header['expected_bytes'],
            finished_at=now,census_sha256=census,source_receipt_sha256='' if side=='source' else digest(source_receipt),
            native_usage_reconciled=side=='source',native_used_before=member['used_bytes'],
            native_used_after=0 if side=='source' else member['used_bytes'],native_reserved_bytes=member['reserved_bytes'])
        receipt=p.sign(self.store.unseal(node['private_key']),record)
        verify_receipt(receipt,value,side,self._counts(header),self.t.wall(),source_receipt=source_receipt)
        if side=='source':
            await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==node['node_id']).values(used_bytes=0,updated_at=now))
        for table in (s.capacity,s.scopes):await conn.execute(delete(table).where(table.c.task_id==value['task_id'],table.c.member_id==node['node_id']))
        await conn.execute(delete(s.drains).where(s.drains.c.member_id==node['node_id'],s.drains.c.owner_id==value['task_id'],s.drains.c.reason=='migration'))
        await conn.execute(update(s.peer_reservations).where(s.peer_reservations.c.task_id==value['task_id']).values(state='released',
            revision=header['revision']+1,updated_at=now,payload={**header['payload'],'final_receipt':receipt,
                **({'source_final_receipt':source_receipt} if side=='target' else {})}))
        await self.store.log(conn,'migration_native_finalized',value['master_id'],task_id=value['task_id'],side=side,
            receipt_sha256=digest(receipt),used_before=record['native_used_before'],used_after=record['native_used_after'])
        return receipt

    async def source(self,node,relation,request_id,grant):
        identifier(request_id)
        if self.t.source_lock.locked():raise TransferError('migration_finalize_source_busy')
        async with self.t.source_lock:
            try:
                value,_=await self.t._bind(grant,'source',expected='finalize',authenticated_relation=relation)
                async with self.store.database.begin() as conn:
                    header=await self._row(conn,node,relation,value,'source')
                    if header['state']=='released':return self._reply(request_id,value,'source','released',header['payload']['final_receipt'])
                    await self._source_quiet(conn,value)
                expected=header['payload']['transfer'].get('source_roots');paths=self.t.root_provider()
                key=(value['task_id'],value['epoch'],value['runner_id'],digest(expected))
                scan=self.t.source_finalization_scan
                if scan is not None and (scan.key!=key or self.t.wall()>=scan.deadline):
                    await joined_io(scan.close);scan=None;self.t.source_finalization_scan=None
                if scan is None:
                    scan=await joined_io(roots.EmptySource,paths,expected,key,self.t.wall(),on_cancel=lambda item:item.close())
                    self.t.source_finalization_scan=scan
                complete=await joined_io(scan.page)
                if not complete:return self._reply(request_id,value,'source','inventory')
                async with media_mutation_lock:
                    await joined_io(roots.check,paths,expected)
                    async with self.store.database.begin() as conn:
                        header=await self._row(conn,node,relation,value,'source');await self._source_quiet(conn,value)
                        receipt=await self._release(conn,node,value,header,digest(dict(roots=expected,directories=scan.directories)))
                await joined_io(scan.close);self.t.source_finalization_scan=None
                return self._reply(request_id,value,'source','released',receipt)
            except BaseException:
                if self.t.source_finalization_scan is not None:
                    await joined_io(self.t.source_finalization_scan.close);self.t.source_finalization_scan=None
                raise

    async def target(self,node,relation,request_id,grant,source_receipt):
        identifier(request_id)
        if self.t.target_lock.locked():raise TransferError('migration_finalize_target_busy')
        async with self.t.target_lock:
            value,_=await self.t._bind(grant,'target',expected='finalize',authenticated_relation=relation)
            async with media_mutation_lock,self.store.database.begin() as conn:
                header=await self._row(conn,node,relation,value,'target')
                verify_receipt(source_receipt,value,'source',self._counts(header),self.t.wall())
                if header['state']=='released':
                    if header['payload']['source_final_receipt']!=source_receipt:raise TransferError('migration_finalize_source_receipt_changed')
                    return self._reply(request_id,value,'target','released',header['payload']['final_receipt'])
                progress=header['payload']['transfer'].get('finalization',{})
                if (progress.get('epoch'),progress.get('runner_id'),progress.get('source_receipt_sha256'))!=(
                        value['epoch'],value['runner_id'],digest(source_receipt)):
                    progress=dict(epoch=value['epoch'],runner_id=value['runner_id'],source_receipt_sha256=digest(source_receipt),
                        after='',unit_count=0,object_count=0,total_bytes=0,census_sha256=digest(['target-final-census',1]))
                unit=(await conn.execute(select(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id>progress['after'])
                    .order_by(s.units.c.unit_id).limit(1))).mappings().first()
                original=header['payload']['transfer'].get('finalization',{})
            if unit is None:
                if (progress['unit_count'],progress['object_count'],progress['total_bytes'])!=self._counts(header):
                    raise TransferError('migration_finalize_target_census_incomplete')
                for kind in ('media','recording'):
                    root=self.t.root_provider()['media' if kind=='media' else 'recordings']
                    await joined_io(staging.finish_task,root,value['task_id'],header['payload']['transfer'].get('staging_roots',{}).get(kind),header['binding_sha256'])
                async with media_mutation_lock,self.store.database.begin() as conn:
                    current=await self._row(conn,node,relation,value,'target')
                    if current['payload']['transfer'].get('finalization',{})!=original:
                        raise TransferError('migration_finalize_progress_changed')
                    receipt=await self._release(conn,node,value,current,progress['census_sha256'],source_receipt=source_receipt)
                return self._reply(request_id,value,'target','released',receipt)
        # The bounded verifier owns the same Target I/O lock for each page.
        result=await TargetVerification(self.t,purpose='finalize').advance(node,relation,request_id,grant,unit['unit_id'])
        if result['phase']=='verified':
            live_proof(result['proof'],value,unit['publication']['commit'],self.t.wall())
            async with self.t.target_lock:
                root=self.t.root_provider()['media' if unit['resource_kind']=='media' else 'recordings']
                await joined_io(staging.remove_unit,root,value['task_id'],unit['unit_id'],header['payload']['transfer']['staging_roots'][unit['resource_kind']])
                async with media_mutation_lock,self.store.database.begin() as conn:
                    current=await self._row(conn,node,relation,value,'target')
                    if current['state']!='active' or current['payload']['transfer'].get('finalization',{})!=original:
                        raise TransferError('migration_finalize_progress_changed')
                    progress={**progress,'after':unit['unit_id'],'unit_count':progress['unit_count']+1,
                        'object_count':progress['object_count']+unit['object_count'],'total_bytes':progress['total_bytes']+unit['total_bytes'],
                        'census_sha256':digest([progress['census_sha256'],unit['publication']['commit_sha256'],result['proof']['payload']['directory_version']])}
                    await conn.execute(update(s.peer_reservations).where(s.peer_reservations.c.task_id==value['task_id']).values(
                        payload={**current['payload'],'transfer':{**current['payload']['transfer'],'finalization':progress}}))
        return self._reply(request_id,value,'target','inventory')
