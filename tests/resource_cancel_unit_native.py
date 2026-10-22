"""Verified-TLS per-parent settlement and durable lost-reply recovery."""
import asyncio
import uuid

import httpx
from sqlalchemy import select

from app.services import media_crypto_schema as crypto,resource_migration_schema as s
from app.services.federation import schema as fs
from app.services.resource_cancel_unit_master import UnitCancellation
from app.services.resource_control_client import signed_request
from app.services.resource_transfer_protocol import CONTROL_BYTES,TransferError


async def finish(f,transport,traffic,lease,units,originals,source_receipts,target_receipts,restart_source,restart_target,check,domain):
    before=await domain();original_request=transport.request;lost=set();restarts=0;receipts={}
    service=UnitCancellation(f.master.store,transport,traffic=traffic)
    async def heartbeat():
        for peer in (f.source,f.target):
            summary=await transport.request(peer.store.node['endpoint'],'/internal/v1/heartbeat',method='POST',value={},
                credential=peer.credential,relation=peer.relationship)
            await f.master.store.heartbeat(peer.relationship,True,summary=summary)
    async def discard(origin,path,**kwargs):
        result=await original_request(origin,path,**kwargs)
        if path.endswith('/cancel-unit-settle') and result['payload']['phase']=='settled' and origin not in lost:
            lost.add(origin);raise httpx.ReadTimeout('Fixture discarded a received native settlement reply')
        return result
    transport.request=discard
    try:
        for index,unit_id in enumerate(units):
            quiet_observed=False
            for _ in range(70):
                await heartbeat()
                try:lease,result=await service.advance(lease,unit_id,source_receipts[unit_id],target_receipts[unit_id])
                except httpx.ReadTimeout:
                    peer='source' if len(lost)==1 else 'target'
                    await (restart_source if peer=='source' else restart_target)();restarts+=1
                    service=UnitCancellation(f.master.store,transport,traffic=traffic)
                    check('actual_lost_settlement_reply_restarts_'+peer,True)
                    continue
                if result['phase']=='source_waiting_low_traffic':
                    quiet_observed=True;await asyncio.sleep(30.1);continue
                if result['unit_cancelled']:
                    receipts[unit_id]=result['receipt'];break
            else:raise AssertionError('Actual HTTPS per-parent settlement did not converge')
            check('actual_tls_master_cancels_parent_'+str(index),result['task_completed'] is False)
            if index==0:check('source_restart_quiet_window_guards_fresh_settlement',quiet_observed)
    finally:transport.request=original_request
    after=await domain()
    for name,old in before.items():
        new=after[name]
        for table in (fs.identity,s.drains):check('unit_settlement_preserves_'+name+'_'+table.name,new[table.name]==old[table.name])
        if name!='target':
            for table in (fs.global_media,crypto.encryption):
                check('unit_settlement_preserves_'+name+'_'+table.name,new[table.name]==old[table.name])
        check('unit_settlement_releases_only_selected_scopes_'+name,
            new[s.scopes.name]==[row for row in old[s.scopes.name] if row['task_id']!=lease.task_id])
        old_members={row['member_id']:row for row in old[fs.storage_members.name]}
        amount=sum(len(data) for _,_,data in originals) if name in {'master','target'} else 0
        for member in new[fs.storage_members.name]:
            expected=old_members[member['member_id']]
            if member['member_id']==f.target.store.node['node_id']:expected={**expected,'reserved_bytes':expected['reserved_bytes']-amount}
            check('exact_used_and_reserved_after_unit_settlement_'+name+'_'+member['member_id'],member==expected)
        for capacity in new[s.capacity.name]:
            if capacity['task_id']==lease.task_id:check('owned_capacity_settled_'+name,capacity['reserved_bytes']==0)
    async with f.master.database.connect() as conn:
        task=dict((await conn.execute(select(s.tasks).where(s.tasks.c.task_id==lease.task_id))).mappings().one())
        check('unit_settlement_keeps_whole_task_and_source_drain_active',task['state']=='cancelling' and task['committed_bytes']==0 and
            bool(after['master'][s.drains.name]) and bool(after['source'][s.drains.name]))
    check('settlement_keeps_every_original_plain_and_encrypted_byte',all(path.read_bytes()==data for _,path,data in originals))
    check('lost_both_native_settlement_replies_were_recovered',len(lost)==2 and restarts==2)
    # Released parents may legitimately acquire later files. Historical replay
    # must not reinspect/remove these as if they were the old Target shadow.
    later=f.target.roots['media']/originals[0][1].relative_to(f.source.roots['media'])
    later.parent.mkdir(parents=True,exist_ok=True);later.write_bytes(b'later independently owned Target bytes after settlement')
    stable=await domain();await heartbeat()
    async with f.master.database.begin() as conn:task=await service.t.journal._fenced(conn,lease)
    relations=await service.t.relations(task);grant=service.t._grant(task,relations,lease.epoch,lease.runner_id,'cancel')
    for side in ('source','target'):
        result=await signed_request(f.master.store,relations[task[side+'_id']],transport,'/internal/v1/migration/transfer/cancel-unit-settle',
            dict(request_id=uuid.uuid4().hex,grant=grant,unit_id=units[0],authorization=None,source_receipt=None,target_receipt=None,source_settlement=None),
            limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_cancel')
        check('signed_native_settlement_replay_'+side,result['phase']=='settled')
    _,result=await UnitCancellation(f.master.store,transport,traffic=traffic).advance(lease,units[0])
    check('master_settlement_replay_is_stable',result['receipt']==receipts[units[0]])
    check('settlement_replay_keeps_later_material_and_never_refunds_twice',await domain()==stable and
        later.read_bytes()==b'later independently owned Target bytes after settlement')
    return dict(unit_capacity_settlement=True,quota_refund=True,settled_parent_count=len(receipts),
        settlement_process_restarts=restarts,master_terminal_cancellation=False,full_cancellation_acceptance=False)
