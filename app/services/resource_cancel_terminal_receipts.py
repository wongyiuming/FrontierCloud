"""Read-only cancellation witnesses shared by replay and retirement history."""
from app.services.federation import protocol as p
from app.services.resource_cancel_agreement import IDENTITIES
from app.services.resource_cancel_source_peers import selected,verify_receipt as verify_source
from app.services.resource_cancel_target import verify_receipt as verify_target
from app.services.resource_cancel_target_receipts import released,source_record
from app.services.resource_cancel_unit_receipts import verify as verify_settled
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import TransferError,grant_shape,identifier

UNIT_FORMAT='frontiercloud-python-master-unit-cancelled'
TASK_FORMAT='frontiercloud-python-master-task-cancelled'
COMPLETION='cancel_completion'


def master_unit(envelope,master_key,task,unit,now):
    try:
        value=p.verify(master_key,envelope);chosen=selected(unit,task)
        expected=dict(format=UNIT_FORMAT,version=1,node_id=task['master_id'],**chosen,scope_released=True,source_drain_released=False,
            source_files_removed=False,ownership_changed=False,task_completed=False,capacity_refunded=unit['total_bytes'])
        if (not isinstance(value,dict) or value.keys()!={*expected,*IDENTITIES,'epoch','revision','completed_at','source_receipt','target_receipt','settlements'}
                or any(value.get(name)!=item for name,item in expected.items()) or unit['state']!='cancelled' or unit['native_acknowledged']!=1
                or any(type(value[name]) is not int for name in ('version','epoch','revision','completed_at','capacity_refunded','object_count','total_bytes'))
                or any(type(value[name]) is not bool for name in ('scope_released','source_drain_released','source_files_removed','ownership_changed','task_completed'))
                or any(value[name]!=task[name] for name in ('task_id','master_id','source_id','target_id','source_relationship','target_relationship','plan_sha256'))
                or value['units_sha256']!=task['plan']['units_sha256'] or not 0<value['completed_at']<=now+5
                or value['epoch']<1 or value['revision']<1 or not isinstance(value['settlements'],dict) or value['settlements'].keys()!={'source','target'}):raise ValueError()
        verify_target(value['target_receipt'],value['target_key'],master_key,value,chosen,now)
        verify_source(value['source_receipt'],master_key,value,chosen,now,target_receipt=value['target_receipt'])
        for side in ('source','target'):
            verify_settled(value['settlements'][side],value,side,chosen,now,source_receipt=value['source_receipt'],target_receipt=value['target_receipt'],
                source_settlement=None if side=='source' else value['settlements']['source'])
        return value
    except (p.ProtocolError,ValueError,TypeError,KeyError,RecursionError):
        raise TransferError('migration_cancel_master_receipt_invalid') from None


def target_record(record,grant,source,witness,now):
    if not isinstance(record,dict) or record.keys()!={'preparation','receipt'}:
        raise TransferError('migration_cancel_finalize_target_record_invalid')
    roots=record['preparation'].get('payload',{}).get('roots_sha256') if isinstance(record['preparation'],dict) else None
    return released(record['receipt'],grant,source,witness,roots,record['preparation'],now)


def master_task(envelope,master_key,task,now):
    try:
        value=p.verify(master_key,envelope)
        expected=dict(format=TASK_FORMAT,version=1,**{name:task[name] for name in ('task_id','master_id','source_id','target_id',
            'source_relationship','target_relationship','plan_sha256')},units_sha256=task['plan']['units_sha256'],
            unit_count=task['plan']['unit_count'],object_count=task['object_count'],total_bytes=task['total_bytes'],
            committed_bytes=task['committed_bytes'],task_completed=True,source_files_removed=False,ownership_changed=False)
        if (not isinstance(value,dict) or value.keys()!={*expected,'epoch','runner_id','completed_at','census_sha256','grant','source_record','target_record'}
                or any(value.get(name)!=item for name,item in expected.items()) or task['state']!='cancelled'
                or task['plan'].get(COMPLETION+'_sha256')!=digest(envelope)
                or any(type(value[name]) is not int or not 0<=value[name]<2**63 for name in
                    ('version','epoch','completed_at','unit_count','object_count','total_bytes','committed_bytes'))
                or value['epoch']<1 or not task['created_at']<=value['completed_at']<=task['updated_at']<=now+5
                or any(type(value[name]) is not bool for name in ('task_completed','source_files_removed','ownership_changed'))):raise ValueError()
        identifier(value['runner_id']);identifier(value['census_sha256'],long=True)
        grant=p.verify(master_key,value['grant']);grant_shape(grant,grant['created_at'])
        if grant['purpose']!='finalize' or grant['epoch']!=value['epoch'] or grant['runner_id']!=value['runner_id']:raise ValueError()
        for name in ('task_id','master_id','source_id','target_id','source_relationship','target_relationship','plan_sha256','units_sha256'):
            if grant[name]!=value[name]:raise ValueError()
        source=source_record(value['source_record'],grant,(task['plan']['unit_count'],task['object_count'],task['total_bytes']),master_key,now)
        target=target_record(value['target_record'],grant,source,value['source_record'],now)
        if source['committed_bytes']!=task['committed_bytes'] or target['completed_at']>value['completed_at']+5:raise ValueError()
        return value
    except (p.ProtocolError,ValueError,TypeError,KeyError,RecursionError):
        raise TransferError('migration_cancel_finalize_completion_invalid') from None


def cleaned_unit(unit,now):
    try:
        publication=unit['publication'];commit=publication['commit'];cleanup=publication.get('source_cleanup_reply',{})
        if (unit['state']!='cleaned' or unit['native_acknowledged']!=1 or digest(commit)!=publication.get('commit_sha256')
                or unit['committed_at']!=commit['committed_at'] or unit['cleanup_after']!=commit['cleanup_after']
                or unit['cleanup_after']-unit['committed_at']!=3600 or now<unit['cleanup_after']
                or set(publication.get('native_acknowledgements',{}))!={'source','target'}
                or cleanup.get('phase')!='cleaned' or cleanup.get('commit_sha256')!=digest(commit)
                or any(type(cleanup.get(name)) is not int for name in ('deleted_count','deleted_bytes'))
                or (cleanup.get('deleted_count'),cleanup.get('deleted_bytes'))!=(unit['object_count'],unit['total_bytes'])):raise ValueError()
        return dict(commit=commit,native_acknowledgements=publication['native_acknowledgements'],source_cleanup_reply=cleanup)
    except (ValueError,TypeError,KeyError):
        raise TransferError('migration_cancel_finalize_cleanup_history_changed') from None
