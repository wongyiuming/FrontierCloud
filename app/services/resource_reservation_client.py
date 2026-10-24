"""Master-side bounded signed controls for native preparation, never media bytes."""
import uuid

from app.services.federation import protocol as p
from app.services.resource_control_client import signed_request
from app.services.resource_observation import digest
from app.services.resource_peer_reservations import ReservationError


class ReservationClient:
    def __init__(self,store,relation,transport,reservation):
        self.store,self.relation,self.transport,self.reservation=store,relation,transport,reservation
        self.binding=digest(reservation)

    async def _call(self,action,**values):
        r=self.reservation
        if (self.store.node.get('role')!='Master' or self.relation.get('direction')!='downstream' or
            self.relation.get('state')!='active' or self.relation['peer_id']!=r[r['side']+'_id'] or
            self.relation['relationship_id']!=r[r['side']+'_relationship'] or r['master_id']!=self.store.node['node_id']):
            raise ReservationError('migration_reservation_member_mismatch')
        request_id=uuid.uuid4().hex
        value=await signed_request(self.store,self.relation,self.transport,'/internal/v1/migration/reservations/'+action,
            dict(request_id=request_id,**values),limit=8*1024,error_type=ReservationError,prefix='migration_reservation')
        fields={'format','version','request_id','node_id','master_id','task_id','side','state','binding_sha256','revision',
                'unit_count','object_count','total_bytes','units_sha256','capacity_reserved','committed_bytes','cancelled_bytes','writable_verified','execution_authorized'}
        if (not isinstance(value,dict) or value.keys()!=fields or value['format']!='frontiercloud-python-native-reservation' or
            type(value['version']) is not int or value['version']!=1 or value['request_id']!=request_id or
            value['node_id']!=self.relation['peer_id'] or value['master_id']!=r['master_id'] or value['task_id']!=r['task_id'] or
            value['side']!=r['side'] or value['binding_sha256']!=self.binding or
            not isinstance(value['state'],str) or value['state'] not in {'preparing','prepared','active','aborting','aborted','released'} or
            type(value['execution_authorized']) is not bool or type(value['writable_verified']) is not bool or
            (value['state'] not in {'active','aborting'} and (value['execution_authorized'] or value['writable_verified'])) or
            (r['side']!='target' and value['writable_verified']) or
            type(value['revision']) is not int or value['revision']<1 or
            not isinstance(value['units_sha256'],str) or not p.OBJECT_ID.fullmatch(value['units_sha256'])):
            raise ReservationError('migration_reservation_signed_response_invalid')
        for name in ('unit_count','object_count','total_bytes'):
            if type(value[name]) is not int or not 0<=value[name]<=r[name]:
                raise ReservationError('migration_reservation_signed_response_invalid')
        if (type(value['committed_bytes']) is not int or not 0<=value['committed_bytes']<=r['total_bytes'] or
                type(value['cancelled_bytes']) is not int or not 0<=value['cancelled_bytes']<=r['total_bytes'] or
                value['committed_bytes']+value['cancelled_bytes']>r['total_bytes'] or
                (value['committed_bytes'] or value['cancelled_bytes']) and value['state'] not in {'active','released'}):
            raise ReservationError('migration_reservation_signed_response_invalid')
        expected=r['total_bytes']-value['committed_bytes']-value['cancelled_bytes'] if r['side']=='target' and value['state'] not in {'aborted','released'} else 0
        if type(value['capacity_reserved']) is not int or value['capacity_reserved']!=expected:
            raise ReservationError('migration_reservation_signed_response_invalid')
        return value

    async def begin(self):return await self._call('begin',reservation=self.reservation)
    async def add_unit(self,unit):return await self._call('unit',task_id=self.reservation['task_id'],unit=unit)
    async def seal(self):return await self._call('seal',task_id=self.reservation['task_id'])
    async def status(self):return await self._call('status',task_id=self.reservation['task_id'])
    async def abort_unstarted(self):return await self._call('abort-unstarted',task_id=self.reservation['task_id'])
    async def cancel_unstarted(self):return await self._call('cancel-unstarted',reservation=self.reservation)
