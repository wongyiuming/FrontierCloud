"""Signed Target preparation and release records preserve all public data."""
from app.services.federation import protocol as p
from app.services.resource_cancel_agreement import IDENTITIES
from app.services.resource_cancel_source_receipts import verify as verify_source
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import TransferError,identifier

PREPARED='frontiercloud-python-target-cancel-prepared'
RELEASED='frontiercloud-python-target-cancel-release'
COUNTS=('unit_count','object_count','total_bytes','committed_units','committed_objects','committed_bytes')


def roots_digest(roots):
    if isinstance(roots,dict):return digest(roots)
    identifier(roots,long=True)
    return roots


def source_record(record,grant,counts,master_key,now):
    if (not isinstance(record,dict) or record.keys()!={'version','agreement','source_census','receipt'}
            or type(record['version']) is not int or record['version']!=1):
        raise TransferError('migration_cancel_target_source_record_invalid')
    return verify_source(record['receipt'],grant,counts,now,master_key=master_key,
        agreement=record['agreement'],source_census=record['source_census'])


def prepared(envelope,grant,source,source_witness,roots,now):
    try:
        if not isinstance(envelope,dict) or envelope.keys()!={'payload','signature'}:raise ValueError()
        value=p.verify(grant['target_key'],envelope)
        expected=dict(format=PREPARED,version=1,node_id=grant['target_id'],**{name:grant[name] for name in IDENTITIES},
            **{name:source[name] for name in COUNTS},source_record_sha256=digest(source_witness),roots_sha256=roots_digest(roots))
        if (not isinstance(value,dict) or value.keys()!={*expected,'epoch','runner_id','prepared_at','units_sha256_current'}
                or any(value.get(name)!=item for name,item in expected.items())
                or any(type(value[name]) is not int or not 0<=value[name]<2**63 for name in (*COUNTS,'version','epoch','prepared_at'))
                or not 1<=value['epoch']<=grant['epoch'] or not 0<value['prepared_at']<=now+5
                or value['prepared_at']<source['completed_at']):raise ValueError()
        identifier(value['runner_id']);identifier(value['units_sha256_current'],long=True)
        return value
    except (p.ProtocolError,ValueError,KeyError,TypeError,RecursionError):
        raise TransferError('migration_cancel_target_prepared_invalid') from None


def released(envelope,grant,source,source_witness,roots,preparation,now):
    try:
        prior=prepared(preparation,grant,source,source_witness,roots,now)
        if not isinstance(envelope,dict) or envelope.keys()!={'payload','signature'}:raise ValueError()
        value=p.verify(grant['target_key'],envelope)
        expected=dict(format=RELEASED,version=1,node_id=grant['target_id'],**{name:grant[name] for name in IDENTITIES},
            **{name:source[name] for name in COUNTS},source_record_sha256=digest(source_witness),roots_sha256=roots_digest(roots),
            preparation_sha256=digest(preparation),units_sha256_current=prior['units_sha256_current'],
            own_reserved_bytes=0,scope_released=True,media_files_removed=False,ownership_changed=False,task_completed=False)
        if (not isinstance(value,dict) or value.keys()!={*expected,'epoch','runner_id','completed_at','native_used_before','native_used_after',
                    'native_reserved_before','native_reserved_after'} or any(value.get(name)!=item for name,item in expected.items())
                or any(type(value[name]) is not int or not 0<=value[name]<2**63 for name in (*COUNTS,'version','epoch','completed_at',
                    'native_used_before','native_used_after','native_reserved_before','native_reserved_after','own_reserved_bytes'))
                or any(type(value[name]) is not bool for name in ('scope_released','media_files_removed','ownership_changed','task_completed'))
                or not prior['epoch']<=value['epoch']<=grant['epoch'] or not prior['prepared_at']<=value['completed_at']<=now+5
                or value['native_used_before']!=value['native_used_after'] or value['native_used_before']<source['committed_bytes']
                or value['native_reserved_before']!=value['native_reserved_after']):raise ValueError()
        identifier(value['runner_id'])
        return value
    except (p.ProtocolError,ValueError,KeyError,TypeError,RecursionError):
        raise TransferError('migration_cancel_target_release_receipt_invalid') from None
