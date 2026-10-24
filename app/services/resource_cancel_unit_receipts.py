"""Strict native unit-settlement receipts; replay never repeats a refund."""
from app.services.federation import protocol as p
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import TransferError,identifier

FORMAT='frontiercloud-python-cancel-unit-settled'
IDENTITIES=('task_id','master_id','source_id','target_id','source_relationship','target_relationship','plan_sha256','units_sha256',
    'source_binding','target_binding','source_key','target_key')
RECORD_FIELDS={'version','authorization','source_receipt','target_receipt','source_settlement','receipt'}


def verify(envelope,grant,side,unit,now,*,source_receipt=None,target_receipt=None,source_settlement=None,authorization=None):
    try:
        if side not in {'source','target'} or not isinstance(envelope,dict) or envelope.keys()!={'payload','signature'}:raise ValueError()
        value=p.verify(grant[side+'_key'],envelope)
        expected=dict(format=FORMAT,version=1,node_id=grant[side+'_id'],side=side,**{name:grant[name] for name in IDENTITIES},
            **{name:unit[name] for name in ('unit_id','resource_kind','unit_version','object_count','total_bytes')},unit_sha256=digest(unit),
            scope_released=True,source_files_removed=False,ownership_changed=False,task_completed=False,
            capacity_refunded=unit['total_bytes'] if side=='target' else 0)
        extra={'epoch','completed_at','scope_id','root_sha256','authorization_sha256','source_receipt_sha256','target_receipt_sha256',
            'source_settlement_sha256','fresh_source_verification_sha256','task_reserved_before','task_reserved_after',
            'member_reserved_before','member_reserved_after','used_before','used_after'}
        numbers=('version','epoch','completed_at','object_count','total_bytes','capacity_refunded','task_reserved_before','task_reserved_after',
            'member_reserved_before','member_reserved_after','used_before','used_after')
        if (not isinstance(value,dict) or value.keys()!={*expected,*extra} or any(value.get(name)!=item for name,item in expected.items()) or
                any(type(value[name]) is not int or not 0<=value[name]<2**63 for name in numbers) or
                any(type(value[name]) is not bool for name in ('scope_released','source_files_removed','ownership_changed','task_completed')) or
                not 1<=value['epoch']<=grant['epoch'] or not 0<value['completed_at']<=now+5 or value['used_before']!=value['used_after'] or
                value['task_reserved_before']-value['task_reserved_after']!=value['capacity_refunded'] or
                value['member_reserved_before']-value['member_reserved_after']!=value['capacity_refunded'] or
                side=='source' and (value['task_reserved_before'] or value['task_reserved_after'] or value['source_settlement_sha256']!='')):raise ValueError()
        for name in ('scope_id','root_sha256','authorization_sha256','source_receipt_sha256','target_receipt_sha256','fresh_source_verification_sha256'):
            identifier(value[name],long=True)
        if side=='target':identifier(value['source_settlement_sha256'],long=True)
        for name,item in (('authorization',authorization),('source_receipt',source_receipt),('target_receipt',target_receipt),('source_settlement',source_settlement)):
            if item is not None and digest(item)!=value[name+'_sha256']:raise ValueError()
        return value
    except (p.ProtocolError,ValueError,KeyError,TypeError,RecursionError):raise TransferError('migration_cancel_settlement_receipt_invalid') from None


def recorded(unit,grant,side,now):
    record=unit['publication'].get('cancel_settled')
    if (not isinstance(record,dict) or record.keys()!=RECORD_FIELDS or type(record['version']) is not int or record['version']!=1 or
            unit['state']!='cancelled' or unit['native_acknowledged']!=1 or
            any(not isinstance(record[name],dict) for name in ('authorization','source_receipt','target_receipt','receipt')) or
            side=='source' and record['source_settlement'] is not None or side=='target' and not isinstance(record['source_settlement'],dict)):
        raise TransferError('migration_cancel_settlement_journal_changed')
    from app.services.resource_cancel_source_peers import selected as source_selected
    from app.services.resource_cancel_target import selected as target_selected
    chosen=(source_selected if side=='source' else target_selected)(unit,grant)
    value=verify(record['receipt'],grant,side,chosen,now,authorization=record['authorization'],source_receipt=record['source_receipt'],
        target_receipt=record['target_receipt'],source_settlement=record['source_settlement'])
    return record,value
