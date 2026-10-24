"""Release only a settled Target task; public media and other quotas survive."""
from sqlalchemy import delete,func,select,update

from app.core.joined_io import joined_io
from app.services import resource_migration_schema as s,resource_transfer_files as staging
from app.services.federation import protocol as p,schema as fs
from app.services.media_manager import media_mutation_lock
from app.services.resource_cancel_agreement import IDENTITIES
from app.services.resource_cancel_target_receipts import COUNTS,PREPARED,RELEASED,prepared,released,source_record
from app.services.resource_cancel_unit_peers import UnitSettlement
from app.services.resource_observation import digest
from app.services.resource_target_verify import TargetVerification,live_proof
from app.services.resource_transfer_protocol import TransferError,identifier

KEY='cancel_target_release'


class TargetRelease:
    def __init__(self,transfers):self.t,self.store=transfers,transfers.store

    @staticmethod
    def _counts(header):return header['expected_units'],header['expected_objects'],header['expected_bytes']

    async def _row(self,conn,node,relation,value,witness):
        header=await self.t.native._row(conn,node,relation,value['task_id'])
        source=source_record(witness,value,self._counts(header),relation['peer_key'],self.t.wall())
        if header['side']!='target' or header['state'] not in {'active','released'}:
            raise TransferError('migration_cancel_target_release_state_changed')
        if header['state']=='released':return header,source,None
        header=await self.t._control_row(conn,value)
        if header['payload']['transfer'].get('cancel_started') is not True:
            raise TransferError('migration_cancel_target_release_stop_required')
        complete=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.units.c.object_count),0),
            func.coalesce(func.sum(s.units.c.total_bytes),0)).where(s.units.c.task_id==value['task_id'],
            (s.units.c.state=='committed')|((s.units.c.state=='cancelled')&(s.units.c.native_acknowledged==1))))).one()
        count=await conn.scalar(select(func.count()).select_from(s.units).where(s.units.c.task_id==value['task_id']))
        committed=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.units.c.object_count),0),
            func.coalesce(func.sum(s.units.c.total_bytes),0)).where(s.units.c.task_id==value['task_id'],s.units.c.state=='committed'))).one()
        own=(await conn.execute(select(s.capacity).where(s.capacity.c.task_id==value['task_id']))).mappings().one_or_none()
        scopes=await conn.scalar(select(func.count()).select_from(s.scopes).where(s.scopes.c.task_id==value['task_id']))
        member=(await conn.execute(select(fs.storage_members).where(fs.storage_members.c.member_id==node['node_id']).with_for_update())).mappings().one()
        if (tuple(complete)!=self._counts(header) or count!=header['expected_units']
                or tuple(committed)!=(source['committed_units'],source['committed_objects'],source['committed_bytes'])
                or not own or own['member_id']!=node['node_id'] or own['reserved_bytes']!=0 or scopes!=committed[0]
                or member['used_bytes']<source['committed_bytes'] or member['used_bytes']+member['reserved_bytes']>member['allocated_bytes']):
            raise TransferError('migration_cancel_target_release_work_incomplete')
        if await conn.scalar(select(s.scopes.c.scope_id).where(s.scopes.c.task_id==value['task_id'],s.scopes.c.member_id!=node['node_id']).limit(1)):
            raise TransferError('migration_cancel_target_release_work_incomplete')
        return header,source,dict(member)

    def _progress(self,header,value,source,witness):
        control=header['payload']['transfer'];progress=control.get(KEY)
        if KEY in control:
            expected={'version','epoch','runner_id','after','unit_count','object_count','total_bytes','committed_units',
                      'committed_objects','committed_bytes','units_sha256_current','source_record_sha256','prepared','checkpoint'}
            if (not isinstance(progress,dict) or progress.keys()!=expected or type(progress['version']) is not int or progress['version']!=1
                    or any(type(progress[name]) is not int or not 0<=progress[name]<2**63 for name in (*COUNTS,'epoch'))
                    or progress['epoch']<1 or progress['source_record_sha256']!=digest(witness)
                    or any(progress[name]>source[name] for name in COUNTS)):
                raise TransferError('migration_cancel_target_release_progress_changed')
            try:
                cursor=p.verify(value['target_key'],progress['checkpoint'])
                expected_cursor=dict(format='frontiercloud-python-target-cancel-cursor',version=1,task_id=value['task_id'],
                    binding_sha256=header['binding_sha256'],roots_sha256=digest(control.get('staging_roots',{})),
                    progress_sha256=digest({**progress,'checkpoint':None}))
                if cursor!=expected_cursor:raise ValueError()
            except (p.ProtocolError,ValueError,TypeError,KeyError):
                raise TransferError('migration_cancel_target_release_progress_changed') from None
            identifier(progress['runner_id']);identifier(progress['units_sha256_current'],long=True)
            if progress['after']!='':identifier(progress['after'],long=True)
            if progress['prepared'] is not None:
                token=prepared(progress['prepared'],value,source,witness,control.get('staging_roots',{}),self.t.wall())
                if (any(progress[name]!=source[name] for name in COUNTS) or token['units_sha256_current']!=progress['units_sha256_current']):
                    raise TransferError('migration_cancel_target_release_progress_changed')
                return progress
            if (progress['epoch'],progress['runner_id'])==(value['epoch'],value['runner_id']):return progress
        return dict(version=1,epoch=value['epoch'],runner_id=value['runner_id'],after='',unit_count=0,object_count=0,total_bytes=0,
            committed_units=0,committed_objects=0,committed_bytes=0,units_sha256_current=digest(['cancel-target-final',1]),
            source_record_sha256=digest(witness),prepared=None,checkpoint=None)

    async def _save(self,conn,header,value,progress):
        progress={**progress,'checkpoint':None}
        checkpoint=p.sign(self.store.unseal(self.store.node['private_key']),dict(format='frontiercloud-python-target-cancel-cursor',version=1,
            task_id=value['task_id'],binding_sha256=header['binding_sha256'],roots_sha256=digest(header['payload']['transfer'].get('staging_roots',{})),
            progress_sha256=digest(progress)))
        progress={**progress,'checkpoint':checkpoint}
        await conn.execute(update(s.peer_reservations).where(s.peer_reservations.c.task_id==value['task_id']).values(
            payload={**header['payload'],'transfer':{**header['payload']['transfer'],KEY:progress}},updated_at=int(self.t.wall())))
        await self.store.log(conn,'migration_cancel_target_release_step',value['master_id'],task_id=value['task_id'],unit_count=progress['unit_count'])

    def _reply(self,request_id,value,phase,*,receipt=None,preparation=None,witness=None):
        return dict(format='frontiercloud-python-target-cancel-release-progress',version=1,request_id=request_id,
            node_id=value['target_id'],task_id=value['task_id'],epoch=value['epoch'],expires_at=value['expires_at'],phase=phase,
            receipt=receipt,preparation=preparation,source_record=witness,media_files_removed=False,task_completed=False)

    async def advance(self,node,relation,request_id,grant,source_record):
        identifier(request_id)
        if self.t.target_lock.locked():raise TransferError('migration_cancel_target_release_busy')
        async with self.t.target_lock:
            value,active=await self.t._bind(grant,'target',expected='finalize',authenticated_relation=relation)
            async with media_mutation_lock,self.store.database.begin() as conn:
                header,source,_=await self._row(conn,node,active,value,source_record)
                roots=header['payload']['transfer'].get('staging_roots',{})
                if header['state']=='released':
                    record=header['payload'].get(KEY)
                    if (not isinstance(record,dict) or record.keys()!={'version','source_record','preparation','receipt'}
                            or type(record['version']) is not int or record['version']!=1 or record['source_record']!=source_record):
                        raise TransferError('migration_cancel_target_release_journal_changed')
                    released(record['receipt'],value,source,source_record,roots,record['preparation'],self.t.wall())
                    return self._reply(request_id,value,'released',receipt=record['receipt'],preparation=record['preparation'],witness=source_record)
                if KEY in header['payload']:raise TransferError('migration_cancel_target_release_journal_changed')
                progress=self._progress(header,value,source,source_record)
                if progress['prepared'] is None:
                    unit=(await conn.execute(select(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id>progress['after'])
                        .order_by(s.units.c.unit_id).limit(1))).mappings().first()
                    if unit is None:
                        if any(progress[name]!=source[name] for name in COUNTS):raise TransferError('migration_cancel_target_release_census_incomplete')
                        token=p.sign(self.store.unseal(node['private_key']),dict(format=PREPARED,version=1,node_id=node['node_id'],
                            **{name:value[name] for name in IDENTITIES},**{name:source[name] for name in COUNTS},epoch=value['epoch'],runner_id=value['runner_id'],
                            prepared_at=int(self.t.wall()),source_record_sha256=digest(source_record),roots_sha256=digest(roots),
                            units_sha256_current=progress['units_sha256_current']))
                        prepared(token,value,source,source_record,roots,self.t.wall())
                        progress={**progress,'prepared':token};await self._save(conn,header,value,progress)
                        return self._reply(request_id,value,'prepared')
                    if unit['state']=='cancelled':
                        await UnitSettlement(self.t)._recorded(unit,value,'target',active['peer_key'],roots[unit['resource_kind']])
                        proof=unit['publication']['cancel_settled']['receipt']
                    else:proof=None
                else:unit=None
            if progress['prepared'] is not None:
                for kind in ('media','recording'):
                    await joined_io(staging.finish_task,self.t.root_provider()['media' if kind=='media' else 'recordings'],
                        value['task_id'],roots.get(kind),header['binding_sha256'])
                async with media_mutation_lock,self.store.database.begin() as conn:
                    current,source,member=await self._row(conn,node,active,value,source_record)
                    if self._progress(current,value,source,source_record)!=progress:
                        raise TransferError('migration_cancel_target_release_progress_changed')
                    receipt=p.sign(self.store.unseal(node['private_key']),dict(format=RELEASED,version=1,node_id=node['node_id'],
                        **{name:value[name] for name in IDENTITIES},**{name:source[name] for name in COUNTS},epoch=value['epoch'],runner_id=value['runner_id'],
                        completed_at=int(self.t.wall()),source_record_sha256=digest(source_record),roots_sha256=digest(roots),
                        preparation_sha256=digest(progress['prepared']),units_sha256_current=progress['units_sha256_current'],own_reserved_bytes=0,
                        native_used_before=member['used_bytes'],native_used_after=member['used_bytes'],native_reserved_before=member['reserved_bytes'],
                        native_reserved_after=member['reserved_bytes'],scope_released=True,media_files_removed=False,ownership_changed=False,task_completed=False))
                    released(receipt,value,source,source_record,roots,progress['prepared'],self.t.wall())
                    for table in (s.capacity,s.scopes):await conn.execute(delete(table).where(table.c.task_id==value['task_id'],table.c.member_id==node['node_id']))
                    await conn.execute(delete(s.read_leases).where(s.read_leases.c.task_id==value['task_id'],s.read_leases.c.member_id==node['node_id'],s.read_leases.c.purpose=='migration'))
                    await conn.execute(update(s.peer_reservations).where(s.peer_reservations.c.task_id==value['task_id']).values(state='released',
                        revision=current['revision']+1,updated_at=int(self.t.wall()),payload={**current['payload'],KEY:dict(version=1,
                            source_record=source_record,preparation=progress['prepared'],receipt=receipt)}))
                    await self.store.log(conn,'migration_native_cancel_target_released',value['master_id'],task_id=value['task_id'],receipt_sha256=digest(receipt))
                    self.t._alive(value)
                return self._reply(request_id,value,'released',receipt=receipt,preparation=progress['prepared'],witness=source_record)
        if proof is None:
            result=await TargetVerification(self.t,purpose='finalize').advance(node,active,request_id,grant,unit['unit_id'])
            if result['phase']!='verified':return self._reply(request_id,value,'units')
            proof=result['proof'];live_proof(proof,value,unit['publication']['commit'],self.t.wall())
        async with self.t.target_lock:
            root=self.t.root_provider()['media' if unit['resource_kind']=='media' else 'recordings']
            await joined_io(staging.remove_unit,root,value['task_id'],unit['unit_id'],roots[unit['resource_kind']])
            async with media_mutation_lock,self.store.database.begin() as conn:
                current,source,_=await self._row(conn,node,active,value,source_record)
                fresh=(await conn.execute(select(s.units).where(s.units.c.task_id==value['task_id'],s.units.c.unit_id==unit['unit_id']))).mappings().one()
                if self._progress(current,value,source,source_record)!=progress or fresh['state']!=unit['state']:
                    raise TransferError('migration_cancel_target_release_progress_changed')
                if unit['state']=='committed':live_proof(proof,value,fresh['publication']['commit'],self.t.wall())
                progress={**progress,'after':unit['unit_id'],'unit_count':progress['unit_count']+1,
                    'object_count':progress['object_count']+unit['object_count'],'total_bytes':progress['total_bytes']+unit['total_bytes'],
                    'committed_units':progress['committed_units']+(unit['state']=='committed'),
                    'committed_objects':progress['committed_objects']+(unit['object_count'] if unit['state']=='committed' else 0),
                    'committed_bytes':progress['committed_bytes']+(unit['total_bytes'] if unit['state']=='committed' else 0),
                    'units_sha256_current':digest([progress['units_sha256_current'],unit['unit_id'],unit['state'],digest(proof)])}
                await self._save(conn,current,value,progress)
        return self._reply(request_id,value,'units')
