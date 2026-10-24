"""Typed native Source observations, without Master catalog or release authority."""
import uuid

from sqlalchemy import func,select

from app.services import resource_migration_schema as s
from app.services.resource_cancel_census import verify_proof
from app.services.resource_control_client import signed_request
from app.services.resource_transfer_master import TransferMaster
from app.services.resource_transfer_protocol import CONTROL_BYTES,TransferError


class CensusClient:
    def __init__(self,store,transport,**options):self.t=TransferMaster(store,transport,**options)

    async def query(self,lease):
        await self.t.journal.renew(lease,ttl=60)
        async with self.t.store.database.begin() as conn:
            task=await self.t.journal._fenced(conn,lease)
            counts=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.units.c.object_count),0),
                func.coalesce(func.sum(s.units.c.total_bytes),0)).where(s.units.c.task_id==lease.task_id,
                s.units.c.state.in_(['cleaned','cancelled']),s.units.c.native_acknowledged==1))).one()
            if task['state']!='cancelling' or tuple(counts)!=(task['plan']['unit_count'],task['object_count'],task['total_bytes']):
                raise TransferError('migration_cancel_census_master_units_incomplete')
            relations=await self.t.relations(task,conn=conn)
        if self.t.journal.traffic.snapshot()['can_run'] is not True:raise TransferError('migration_cancel_census_waiting_low_traffic')
        grant=self.t._grant(task,relations,lease.epoch,lease.runner_id,'finalize');value=grant['payload'];request_id=uuid.uuid4().hex
        reply=await signed_request(self.t.store,relations[task['source_id']],self.t.transport,
            '/internal/v1/migration/transfer/cancel-source-census',dict(request_id=request_id,grant=grant),
            limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_cancel_census')
        expected=dict(format='frontiercloud-python-source-cancel-census-progress',version=1,request_id=request_id,
            node_id=task['source_id'],task_id=task['task_id'],epoch=lease.epoch,expires_at=value['expires_at'],
            native_usage_reconciled=False,source_drain_released=False,task_completed=False)
        if (not isinstance(reply,dict) or reply.keys()!={*expected,'phase','processed_objects','retained_objects','retained_bytes','proof'} or
                any(reply.get(name)!=item for name,item in expected.items()) or
                any(type(reply[name]) is not int for name in ('version','epoch','expires_at','processed_objects','retained_objects','retained_bytes')) or
                not 0<=reply['retained_objects']<=reply['processed_objects']<=task['object_count'] or
                not 0<=reply['retained_bytes']<=task['total_bytes'] or self.t.wall()>=reply['expires_at'] or
                any(reply[name] is not False for name in ('native_usage_reconciled','source_drain_released','task_completed')) or
                not isinstance(reply['phase'],str) or reply['phase'] not in {'roster','physical','verified'} or
                reply['phase']!='verified' and reply['proof'] is not None):raise TransferError('migration_cancel_census_peer_reply_invalid')
        if reply['phase']=='verified':
            proof=verify_proof(reply['proof'],value,self.t.wall())
            if (proof['object_count'],proof['total_bytes'])!=(reply['retained_objects'],reply['retained_bytes']):
                raise TransferError('migration_cancel_census_peer_reply_invalid')
        return {**reply,'master_catalog_verified':False}
