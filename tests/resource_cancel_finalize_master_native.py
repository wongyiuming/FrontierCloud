"""Master terminal reconciliation uses trusted native receipts and no media copy."""
import time
from unittest.mock import patch

from sqlalchemy import func,select,update

from app.services import resource_migration_schema as s
from app.services.federation import schema as fs
from app.services.federation import protocol as p
from app.services.node_retirement_history import NativeHistory
from app.services.node_retirement_master_history import MasterHistory
from app.services.resource_cancel_finalize_master import CancellationFinalization


async def finish(f,transport,traffic,lease,originals,heartbeat,check):
    service=CancellationFinalization(f.master.store,transport,traffic=traffic)
    async with f.master.database.connect() as conn:
        before=[dict(row) for row in (await conn.execute(select(fs.global_media))).mappings()]
        target_usage=await conn.scalar(select(fs.storage_members.c.used_bytes).where(fs.storage_members.c.member_id==f.target.store.node['node_id']))
    phases=[]
    for _ in range(40):
        await heartbeat();result=await service.advance(lease);phases.append(result['phase'])
        service=CancellationFinalization(f.master.store,transport,traffic=traffic)
        if result['task_completed']:break
    else:raise AssertionError('Master terminal cancellation did not converge')
    retained=sum(len(data) for _,_,data in originals)
    async with f.master.database.connect() as conn:
        task=dict((await conn.execute(select(s.tasks))).mappings().one())
        check('actual_master_cancel_terminal_receipt_is_signed_and_recoverable',task['state']=='cancelled' and
            service._recorded(task)==result['completion'])
        check('actual_master_cancel_preserves_every_original_catalog_row',before==[dict(row) for row in (await conn.execute(select(fs.global_media))).mappings()])
        check('actual_master_cancel_reconciles_retained_usage_without_resetting_target',
            await conn.scalar(select(fs.storage_members.c.used_bytes).where(fs.storage_members.c.member_id==f.source.store.node['node_id']))==retained and
            await conn.scalar(select(fs.storage_members.c.used_bytes).where(fs.storage_members.c.member_id==f.target.store.node['node_id']))==target_usage)
        check('actual_master_cancel_releases_owned_scopes_capacity_drain_and_runner',all([
            await conn.scalar(select(func.count()).select_from(table))==0 for table in (s.scopes,s.capacity,s.drains)]) and
            await conn.scalar(select(s.runners.c.expires_at))==0)
    # Discard the returned completion, reconstruct the coordinator, then make
    # an unrelated later usage change before retrying the terminal request.
    completion=result['completion']
    async with f.master.database.begin() as conn:
        await conn.execute(update(fs.storage_members).where(fs.storage_members.c.member_id==f.source.store.node['node_id']).values(used_bytes=retained+1234))
    replay=await CancellationFinalization(f.master.store,transport,traffic=traffic).advance(lease)
    async with f.master.database.connect() as conn:
        check('actual_master_cancel_lost_return_replay_preserves_later_usage',replay['completion']==completion and
            await conn.scalar(select(fs.storage_members.c.used_bytes).where(fs.storage_members.c.member_id==f.source.store.node['node_id']))==retained+1234 and
            await conn.scalar(select(func.count()).select_from(fs.audit).where(fs.audit.c.action=='migration_cancelled'))==1)
    check('actual_master_cancel_preserves_all_plain_and_ciphertext',all(path.read_bytes()==data for _,path,data in originals))
    for peer in (f.master,f.source,f.target):
        async with peer.database.connect() as conn:
            before={table.name:[dict(row) for row in (await conn.execute(select(table))).mappings()] for table in s.metadata.tables.values()}
            with patch.object(p,'sign',side_effect=AssertionError('History issued a capability')):
                if peer.name=='master':history=await MasterHistory(peer.store,time.time()).verify(conn,peer.store.node,f.source.store.node['node_id'])
                else:history=await NativeHistory(peer.store,time.time()).verify(conn,peer.store.node)
            after={table.name:[dict(row) for row in (await conn.execute(select(table))).mappings()] for table in s.metadata.tables.values()}
            expected=dict(master_tasks=1) if peer.name=='master' else dict(native_tasks=1)
            check('actual_cancel_history_is_readonly_after_native_restarts_'+peer.name,before==after and history==expected)
    return dict(master_terminal_cancellation=True,master_cancel_native_tls=True,master_cancel_coordinator_reconstructed=True,
        master_cancel_independent_process_restart=False,master_cancel_stages=phases,master_cancel_lost_return_recovered=True,
        master_drain_released=True,master_cancellation_history_verified=True,native_cancellation_history_verified=True,
        full_cancellation_acceptance=False)
