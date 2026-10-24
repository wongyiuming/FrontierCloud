"""Strict durable native completion receipts; replay is observation only."""
from app.services.federation import protocol as p
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import TransferError,identifier

CANCELLED_COMPLETION_FORMAT='frontiercloud-python-migration-cancellation-completion'


def committed_cancellation(task):
    return (task['state']=='cancelled' and isinstance(task['plan'].get('completion'),dict)
        and task['plan']['completion'].get('format')==CANCELLED_COMPLETION_FORMAT)


def verify(envelope,grant,side,counts,now,*,source_receipt=None):
    if not isinstance(envelope,dict) or envelope.keys()!={'payload','signature'}:
        raise TransferError('migration_finalize_receipt_invalid')
    try:value=p.verify(grant[side+'_key'],envelope)
    except p.ProtocolError:raise TransferError('migration_finalize_receipt_invalid') from None
    expected=dict(format='frontiercloud-python-migration-final-receipt',version=1,side=side,node_id=grant[side+'_id'],
        **{name:grant[name] for name in ('task_id','master_id','source_id','target_id','source_relationship',
            'target_relationship','plan_sha256','units_sha256')},binding_sha256=grant[side+'_binding'],
        unit_count=counts[0],object_count=counts[1],total_bytes=counts[2],
        source_receipt_sha256='' if side=='source' else digest(source_receipt),native_usage_reconciled=side=='source')
    numbers=('version','epoch','unit_count','object_count','total_bytes','finished_at','native_used_before','native_used_after','native_reserved_bytes')
    if (not isinstance(value,dict) or value.keys()!={*expected,'epoch','finished_at','census_sha256',
            'native_used_before','native_used_after','native_reserved_bytes'} or
            any(value.get(name)!=item for name,item in expected.items()) or
            any(type(value[name]) is not int or value[name]<0 for name in numbers) or
            type(value['native_usage_reconciled']) is not bool or not 1<=value['epoch']<=grant['epoch'] or
            not 0<value['finished_at']<=now+5 or
            side=='source' and (value['native_used_after']!=0 or value['native_reserved_bytes']!=0) or
            side=='target' and (source_receipt is None or value['native_used_after']!=value['native_used_before'])):
        raise TransferError('migration_finalize_receipt_invalid')
    identifier(value['census_sha256'],long=True)
    return value
