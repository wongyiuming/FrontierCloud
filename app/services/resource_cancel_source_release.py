"""Reconcile retained native Source usage, then release only this task's fences."""
from sqlalchemy import delete,func,select,update

from app.core.joined_io import joined_io
from app.services import resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_cancel_agreement import IDENTITIES,verify as verify_agreement
from app.services.resource_cancel_census import SourceCensus,roots_check
from app.services.resource_cancel_source_receipts import FORMAT,verify as verify_receipt
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import TransferError,identifier


class SourceRelease:
    def __init__(self,transfers):self.t,self.store=transfers,transfers.store;self.census=SourceCensus(transfers)

    @staticmethod
    def _counts(header):return header['expected_units'],header['expected_objects'],header['expected_bytes']

    async def _recorded(self,node,relation,value,header):
        record=header['payload'].get('cancel_release')
        if (not isinstance(record,dict) or record.keys()!={'version','agreement','source_census','receipt'}
                or type(record['version']) is not int or record['version']!=1):
            raise TransferError('migration_cancel_release_journal_changed')
        verify_receipt(record['receipt'],value,self._counts(header),self.t.wall(),master_key=relation['peer_key'],
            agreement=record['agreement'],source_census=record['source_census'])
        if record['receipt']['payload']['roots_sha256']!=digest(header['payload']['transfer']['source_roots']):
            raise TransferError('migration_cancel_release_journal_changed')
        return record

    async def _ready(self,conn,value,header,permission):
        await self.census._ready(conn,value,header)
        total=await conn.scalar(select(func.count()).select_from(s.units).where(s.units.c.task_id==value['task_id']))
        committed=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.units.c.object_count),0),
            func.coalesce(func.sum(s.units.c.total_bytes),0)).where(s.units.c.task_id==value['task_id'],s.units.c.state=='cleaned'))).one()
        scopes=await conn.scalar(select(func.count()).select_from(s.scopes).where(s.scopes.c.task_id==value['task_id']))
        own=(await conn.execute(select(s.capacity).where(s.capacity.c.task_id==value['task_id']))).mappings().one_or_none()
        if (total!=header['expected_units'] or tuple(committed)!=(permission['committed_units'],permission['committed_objects'],permission['committed_bytes'])
                or scopes!=committed[0] or not own or own['member_id']!=value['source_id'] or own['reserved_bytes']!=0):
            raise TransferError('migration_cancel_release_units_or_capacity_changed')
        if await conn.scalar(select(s.scopes.c.scope_id).where(s.scopes.c.task_id==value['task_id'],s.scopes.c.member_id!=value['source_id']).limit(1)):
            raise TransferError('migration_cancel_release_units_or_capacity_changed')
        for table,condition in ((s.peer_reservations,s.peer_reservations.c.state.not_in(['aborted','released'])),
                (s.capacity,True),(s.scopes,True),
                (s.storage_journal,s.storage_journal.c.state.in_(['receiving','verified','publishing','deleting','aborting']))):
            if await conn.scalar(select(next(iter(table.primary_key.columns))).where(
                    table.c.task_id!=value['task_id'],condition).limit(1)):
                raise TransferError('migration_cancel_release_foreign_work')
        if await conn.scalar(select(s.drains.c.owner_id).where(s.drains.c.member_id==value['source_id'],
                s.drains.c.reason=='migration',s.drains.c.owner_id!=value['task_id']).limit(1)):
            raise TransferError('migration_cancel_release_foreign_work')

    def _reply(self,request_id,value,phase,receipt=None,*,agreement=None,source_census=None):
        return dict(format='frontiercloud-python-source-cancel-release-progress',version=1,request_id=request_id,
            node_id=value['source_id'],task_id=value['task_id'],epoch=value['epoch'],expires_at=value['expires_at'],phase=phase,
            receipt=receipt,agreement=agreement,source_census=source_census,task_completed=False,source_files_removed=False)

    async def advance(self,node,relation,request_id,grant,agreement,source_census):
        identifier(request_id)
        if self.t.source_lock.locked():raise TransferError('migration_cancel_release_source_busy')
        async with self.t.source_lock:
            value,active=await self.t._bind(grant,'source',expected='finalize',authenticated_relation=relation)
            async with self.store.database.begin() as conn:
                header=await self.t.native._row(conn,node,active,value['task_id'])
                if header['state']=='released':
                    record=await self._recorded(node,active,value,header)
                    if (agreement is not None and agreement!=record['agreement']
                            or source_census is not None and source_census!=record['source_census']):
                        raise TransferError('migration_cancel_release_journal_changed')
                    return self._reply(request_id,value,'released',record['receipt'],agreement=record['agreement'],source_census=record['source_census'])
                if header['state']!='active' or 'cancel_release' in header['payload']:
                    raise TransferError('migration_cancel_release_journal_changed')
            if agreement is None and source_census is None:return self._reply(request_id,value,'pending')
            permission=verify_agreement(agreement,active['peer_key'],value,self._counts(header),self.t.wall(),source_census=source_census)
            async with media_mutation_lock:
                await roots_check(self.t,header)
                async with self.store.database.begin() as conn:
                    current=await self.census._header(conn,value);progress=self.census._progress(current,value)
                    scan=self.t.source_cancel_census
                    if (not progress or progress['phase']!='verified' or progress['proof']!=source_census
                            or scan is None or scan.key!=(value['task_id'],progress['census_id'],value['epoch'],value['runner_id'])
                            or scan.completed!=digest({**progress,'proof':None})):
                        raise TransferError('migration_cancel_release_current_census_required')
                    if permission['roots_sha256']!=digest(current['payload']['transfer']['source_roots']):
                        raise TransferError('migration_cancel_release_roots_changed')
                    await self._ready(conn,value,current,permission)
                    member=(await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id==value['source_id'])
                        .with_for_update())).mappings().one()
                    verify_agreement(agreement,active['peer_key'],value,self._counts(current),self.t.wall(),source_census=source_census)
                    now=int(self.t.wall())
                    receipt=p.sign(self.store.unseal(node['private_key']),dict(format=FORMAT,version=1,node_id=node['node_id'],side='source',
                        **{name:value[name] for name in IDENTITIES},**{name:permission[name] for name in ('unit_count','object_count','total_bytes',
                            'committed_units','committed_objects','committed_bytes','retained_objects','retained_bytes','catalog_sha256',
                            'roots_sha256','master_state_sha256')},epoch=value['epoch'],runner_id=value['runner_id'],completed_at=now,
                        agreement_sha256=digest(agreement),census_sha256=digest(source_census),native_used_before=member['used_bytes'],
                        native_used_after=permission['retained_bytes'],native_reserved_bytes=member['reserved_bytes'],
                        native_usage_reconciled=True,source_drain_released=True,source_files_removed=False,ownership_changed=False,task_completed=False))
                    verify_receipt(receipt,value,self._counts(current),self.t.wall(),master_key=active['peer_key'],agreement=agreement,source_census=source_census)
                    await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==node['node_id']).values(
                        used_bytes=permission['retained_bytes'],updated_at=now))
                    for table in (s.capacity,s.scopes):
                        await conn.execute(delete(table).where(table.c.task_id==value['task_id'],table.c.member_id==node['node_id']))
                    await conn.execute(delete(s.drains).where(s.drains.c.member_id==node['node_id'],s.drains.c.owner_id==value['task_id'],s.drains.c.reason=='migration'))
                    await conn.execute(delete(s.read_leases).where(s.read_leases.c.member_id==node['node_id'],
                        s.read_leases.c.task_id==value['task_id'],s.read_leases.c.purpose=='migration'))
                    record=dict(version=1,agreement=agreement,source_census=source_census,receipt=receipt)
                    await conn.execute(update(s.peer_reservations).where(s.peer_reservations.c.task_id==value['task_id']).values(
                        state='released',revision=current['revision']+1,updated_at=now,payload={**current['payload'],'cancel_release':record}))
                    await self.store.log(conn,'migration_native_cancel_source_released',value['master_id'],task_id=value['task_id'],
                        receipt_sha256=digest(receipt),used_before=member['used_bytes'],used_after=permission['retained_bytes'])
                    if self.t.wall()>=min(permission['expires_at'],value['expires_at']):
                        raise TransferError('migration_cancel_release_authority_expired')
            scan=self.t.source_cancel_census;self.t.source_cancel_census=None
            if scan is not None:await joined_io(scan.close)
            return self._reply(request_id,value,'released',receipt,agreement=agreement,source_census=source_census)
