"""Durable Source release evidence; old receipts never authorize another write."""
from app.services.federation import protocol as p
from app.services.resource_cancel_agreement import IDENTITIES,verify as verify_agreement
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import TransferError

FORMAT='frontiercloud-python-source-cancel-release'


def verify(envelope,grant,counts,now,*,master_key,agreement,source_census):
    try:
        if not isinstance(envelope,dict) or envelope.keys()!={'payload','signature'}:raise ValueError()
        value=p.verify(grant['source_key'],envelope)
        if not isinstance(value,dict):raise ValueError()
        epoch=value.get('epoch');finished=value.get('completed_at');runner=value.get('runner_id')
        if (type(epoch) is not int or not 1<=epoch<=grant['epoch'] or type(finished) is not int or not 0<finished<=now+5
                or not isinstance(runner,str) or not p.IDENTIFIER.fullmatch(runner)):raise ValueError()
        historical={**grant,'epoch':epoch,'runner_id':runner}
        authorization=verify_agreement(agreement,master_key,historical,counts,finished,source_census=source_census)
        expected=dict(format=FORMAT,version=1,node_id=grant['source_id'],side='source',**{name:grant[name] for name in IDENTITIES},
            **{name:authorization[name] for name in ('unit_count','object_count','total_bytes','committed_units','committed_objects',
                'committed_bytes','retained_objects','retained_bytes','catalog_sha256','roots_sha256','master_state_sha256')},
            agreement_sha256=digest(agreement),census_sha256=digest(source_census),
            native_used_after=authorization['retained_bytes'],native_reserved_bytes=0,
            native_usage_reconciled=True,source_drain_released=True,source_files_removed=False,ownership_changed=False,task_completed=False)
        extra={'epoch','runner_id','completed_at','native_used_before'}
        if (value.keys()!={*expected,*extra} or any(value.get(name)!=item for name,item in expected.items())
                or any(type(value[name]) is not int or not 0<=value[name]<2**63 for name in
                    ('version','epoch','completed_at','native_used_before','native_used_after','native_reserved_bytes','unit_count',
                     'object_count','total_bytes','committed_units','committed_objects','committed_bytes','retained_objects','retained_bytes'))
                or any(type(value[name]) is not bool for name in ('native_usage_reconciled','source_drain_released','source_files_removed',
                    'ownership_changed','task_completed'))):raise ValueError()
        return value
    except (p.ProtocolError,ValueError,KeyError,TypeError,RecursionError):
        raise TransferError('migration_cancel_release_receipt_invalid') from None
