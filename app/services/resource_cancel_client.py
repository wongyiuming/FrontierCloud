"""Typed signed cancellation observations; no media or credential delegation."""
import uuid

from app.services.federation import protocol as p
from app.services.resource_cancel_source_peers import verify_receipt as verify_source
from app.services.resource_cancel_target import verify_receipt as verify_target
from app.services.resource_control_client import signed_request
from app.services.resource_transfer_protocol import CONTROL_BYTES,IMMUTABLE_FIELDS,TransferError,grant_shape


class CancellationClient:
    def __init__(self,transfer):self.t,self.store,self.transport=transfer,transfer.store,transfer.transport

    async def call(self,task,relations,lease,side,action,**values):
        grant=self.t._grant(task,relations,lease.epoch,lease.runner_id,'cancel');request_id=uuid.uuid4().hex
        envelope=await signed_request(self.store,relations[task[side+'_id']],self.transport,
            '/internal/v1/migration/transfer/'+action,dict(request_id=request_id,grant=grant,**values),
            limit=CONTROL_BYTES,error_type=TransferError,prefix='migration_cancel',return_envelope=True)
        return envelope,grant,request_id

    def stop_reply(self,envelope,grant,side,now,*,request_id=None):
        raw=grant['payload']
        try:
            value=p.verify(raw[side+'_key'],envelope)
            expected=dict(format='frontiercloud-python-'+side+'-cancel-stop',version=1,node_id=raw[side+'_id'],
                task_id=raw['task_id'],epoch=raw['epoch'],expires_at=raw['expires_at'],source_files_removed=False,capacity_settled=False)
            if side=='source':expected['ownership_changed']=False
            if (not isinstance(value,dict) or value.keys()!={*expected,'request_id','phase'} or
                    any(value.get(name)!=item for name,item in expected.items()) or
                    any(type(value[name]) is not int for name in ('version','epoch','expires_at')) or
                    any(type(value[name]) is not bool for name in ('source_files_removed','capacity_settled',*(['ownership_changed'] if side=='source' else []))) or
                    not isinstance(value['phase'],str) or value['phase'] not in ({'stopped','waiting_writer','waiting_reader'} if side=='source' else {'stopped','waiting_writer'}) or
                    request_id is not None and value['request_id']!=request_id or not raw['created_at']-5<=now<value['expires_at']):raise ValueError()
            from app.services.resource_transfer_protocol import identifier
            identifier(value['request_id'])
            return value
        except (p.ProtocolError,ValueError,KeyError,TypeError,RecursionError):raise TransferError('migration_cancel_stop_reply_invalid') from None

    def stopped(self,journal,task,relations,lease):
        if journal is None:return dict(version=1,epoch=lease.epoch,runner_id=lease.runner_id,receipts={})
        try:
            if (not isinstance(journal,dict) or journal.keys()!={'version','epoch','runner_id','receipts'} or
                    type(journal['version']) is not int or journal['version']!=1 or type(journal['epoch']) is not int or
                    not 1<=journal['epoch']<=lease.epoch or not isinstance(journal['receipts'],dict) or
                    'source' not in journal['receipts'] or set(journal['receipts'])-{'source','target'}):raise ValueError()
            from app.services.resource_transfer_protocol import identifier
            identifier(journal['runner_id'])
            master_key=p.public_key(self.store.unseal(self.store.node['private_key']))
            expected=self.t._grant(task,relations,journal['epoch'],journal['runner_id'],'cancel')['payload']
            for side,record in journal['receipts'].items():
                if not isinstance(record,dict) or record.keys()!={'grant','reply'}:raise ValueError()
                raw=p.verify(master_key,record['grant']);raw=grant_shape(raw,raw['created_at'])
                if (any(raw[name]!=expected[name] for name in (*IMMUTABLE_FIELDS,'epoch','runner_id','purpose')) or
                        raw['master_id']!=self.store.node['node_id']):raise ValueError()
                self.stop_reply(record['reply'],record['grant'],side,raw['created_at'])
            if journal['epoch']==lease.epoch:
                if journal['runner_id']!=lease.runner_id:raise ValueError()
                return journal
            return dict(version=1,epoch=lease.epoch,runner_id=lease.runner_id,receipts={})
        except (p.ProtocolError,ValueError,KeyError,TypeError,RecursionError):raise TransferError('migration_cancel_stop_journal_changed') from None

    async def stop(self,task,relations,lease,side):
        envelope,grant,request_id=await self.call(task,relations,lease,side,'cancel-source-stop' if side=='source' else 'cancel-stop')
        value=self.stop_reply(envelope,grant,side,self.t.wall(),request_id=request_id)
        return dict(grant=grant,reply=envelope),value

    async def target(self,task,relations,lease,unit,proof):
        envelope,grant,request_id=await self.call(task,relations,lease,'target','cancel-target',unit_id=unit['unit_id'],proof=proof)
        raw=grant['payload'];value=p.verify(raw['target_key'],envelope)
        expected=dict(format='frontiercloud-python-target-cancel',version=1,request_id=request_id,node_id=task['target_id'],task_id=task['task_id'],
            unit_id=unit['unit_id'],unit_version=unit['unit_version'],epoch=lease.epoch,expires_at=raw['expires_at'],source_files_removed=False,capacity_settled=False)
        if (not isinstance(value,dict) or value.keys()!={*expected,'phase','processed_objects','deleted_count','deleted_bytes','receipt'} or
                any(value.get(name)!=item for name,item in expected.items()) or
                any(type(value[name]) is not int for name in ('version','epoch','expires_at','processed_objects','deleted_count','deleted_bytes')) or
                any(type(value[name]) is not bool for name in ('source_files_removed','capacity_settled')) or
                not 0<=value['processed_objects']<=1 or not 0<=value['deleted_count']<=unit['object_count'] or not 0<=value['deleted_bytes']<=unit['total_bytes'] or
                not isinstance(value['phase'],str) or value['phase'] not in {'roster','objects','directory','receipt','cancelled','waiting_writer','waiting_reader','waiting_low_traffic'} or
                self.t.wall()>=value['expires_at'] or value['phase']!='cancelled' and value['receipt'] is not None):raise TransferError('migration_cancel_target_reply_invalid')
        if value['phase']=='cancelled':
            receipt=verify_target(value['receipt'],raw['target_key'],p.public_key(self.store.unseal(self.store.node['private_key'])),raw,unit,self.t.wall())
            if (value['deleted_count'],value['deleted_bytes'])!=(receipt['deleted_count'],receipt['deleted_bytes']):raise TransferError('migration_cancel_target_reply_invalid')
        return value,raw

    async def source(self,task,relations,lease,unit,authorization,target_receipt,*,query=False):
        envelope,grant,request_id=await self.call(task,relations,lease,'source','cancel-source-retain',unit_id=unit['unit_id'],
            authorization=None if query else authorization,target_receipt=None if query else target_receipt)
        raw=grant['payload'];value=p.verify(raw['source_key'],envelope)
        expected=dict(format='frontiercloud-python-source-cancel-retain',version=1,request_id=request_id,node_id=task['source_id'],task_id=task['task_id'],
            unit_id=unit['unit_id'],unit_version=unit['unit_version'],epoch=lease.epoch,expires_at=raw['expires_at'],source_files_removed=False,ownership_changed=False,capacity_settled=False)
        if (not isinstance(value,dict) or value.keys()!={*expected,'phase','processed_objects','receipt'} or
                any(value.get(name)!=item for name,item in expected.items()) or
                any(type(value[name]) is not int for name in ('version','epoch','expires_at','processed_objects')) or
                any(type(value[name]) is not bool for name in ('source_files_removed','ownership_changed','capacity_settled')) or
                not 0<=value['processed_objects']<=unit['object_count'] or
                not isinstance(value['phase'],str) or value['phase'] not in {'pending','objects','membership','retained','waiting_writer','waiting_reader','waiting_low_traffic'} or
                value['phase']=='pending' and not query or self.t.wall()>=value['expires_at'] or
                value['phase']!='retained' and value['receipt'] is not None):raise TransferError('migration_cancel_retention_reply_invalid')
        if value['phase']=='retained':
            verify_source(value['receipt'],p.public_key(self.store.unseal(self.store.node['private_key'])),raw,unit,self.t.wall(),target_receipt=target_receipt)
            if value['processed_objects']!=unit['object_count']:raise TransferError('migration_cancel_retention_reply_invalid')
        return value,raw
