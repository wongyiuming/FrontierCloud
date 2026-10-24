"""Typed private-copy cancellation: logical coverage and physical removal differ."""
from app.services.federation import protocol as p
from app.services.resource_cancel_source import reply as source_reply,verify as verify_source
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import TransferError,identifier

FORMAT='frontiercloud-python-target-private-cancelled'
COUNTS=('processed_count','processed_bytes','deleted_count','deleted_bytes','absent_count')


def totals(value,unit):
    limits=dict(processed_count=unit['object_count'],processed_bytes=unit['total_bytes'],
        deleted_count=unit['object_count'],deleted_bytes=unit['total_bytes'],absent_count=unit['object_count'])
    if (any(type(value.get(name)) is not int or not 0<=value[name]<=limit for name,limit in limits.items()) or
            value['deleted_count']+value['absent_count']!=value['processed_count'] or
            value['deleted_bytes']>value['processed_bytes'] or not value['deleted_count'] and value['deleted_bytes']):
        raise TransferError('migration_cancel_private_counts_invalid')
    return value


def progress(value,grant,unit,master_key,now):
    required={'version','step','started_at','after','roster_sha256','initial_proof',*COUNTS}
    optional={'final_proof','receipt_after','receipt_count','receipt_bytes','receipt_roster','receipt',
        'receipt_deleted_count','receipt_deleted_bytes','receipt_absent_count'}
    try:
        if (not isinstance(value,dict) or not required<=value.keys() or value.keys()-required-optional or
                type(value['version']) is not int or value['version']!=2 or value['step'] not in {'roster','objects','directory','receipt'} or
                type(value['started_at']) is not int or not 0<value['started_at']<=now+5):raise ValueError()
        totals(value,unit);identifier(value['roster_sha256'],long=True)
        for name in ('after','receipt_after'):
            item=value.get(name,'')
            if item:identifier(item,long=unit['resource_kind']=='media')
            elif item!='':raise ValueError()
        if 'receipt_roster' in value:identifier(value['receipt_roster'],long=True)
        for name,limit in (('receipt_count',unit['object_count']),('receipt_bytes',unit['total_bytes']),
                ('receipt_deleted_count',unit['object_count']),('receipt_deleted_bytes',unit['total_bytes']),('receipt_absent_count',unit['object_count'])):
            if name in value and (type(value[name]) is not int or not 0<=value[name]<=limit):raise ValueError()
        initial=value['initial_proof']
        if not isinstance(initial,dict) or initial.keys()!={'authorization','source_reply'}:raise ValueError()
        permission=initial['authorization']['payload'];epoch=permission['epoch']
        if type(epoch) is not int or not 1<=epoch<=grant['epoch']:raise ValueError()
        verify_source(initial['source_reply'],master_key,initial['authorization'],
            dict(grant,epoch=epoch,runner_id=permission['runner_id']),unit,value['started_at'])
        if value['step']=='roster' and (value['after'] or any(value[name] for name in COUNTS)):raise ValueError()
        if value['step'] in {'directory','receipt'} and (value['processed_count'],value['processed_bytes'])!=(unit['object_count'],unit['total_bytes']):raise ValueError()
        return value
    except (p.ProtocolError,ValueError,TypeError,KeyError,RecursionError):raise TransferError('migration_cancel_private_progress_invalid') from None


def verify(envelope,key,master_key,grant,unit,now,*,proof=None):
    from app.services.resource_cancel_target import IDENTITIES
    try:
        if not isinstance(envelope,dict) or envelope.keys()!={'payload','signature'}:raise ValueError()
        value=p.verify(key,envelope)
        expected=dict(format=FORMAT,version=1,node_id=grant['target_id'],**{name:grant[name] for name in IDENTITIES},
            **{name:unit[name] for name in ('unit_id','resource_kind','unit_version','object_count','total_bytes')},unit_sha256=digest(unit),
            bindings_sha256=unit['unit_version'],source_files_removed=False,ownership_changed=False,capacity_settled=False)
        fields={'epoch','started_at','completed_at','root_sha256','roster_sha256','initial_proof_sha256',
            'final_authorization_sha256','final_source_reply',*COUNTS}
        if (not isinstance(value,dict) or value.keys()!={*expected,*fields} or any(value.get(name)!=item for name,item in expected.items()) or
                any(type(value[name]) is not int for name in ('version','epoch','started_at','completed_at')) or
                any(type(value[name]) is not bool for name in ('source_files_removed','ownership_changed','capacity_settled')) or
                not 1<=value['epoch']<=grant['epoch'] or not 0<value['started_at']<=value['completed_at']<=now+5 or
                (value['processed_count'],value['processed_bytes'])!=(unit['object_count'],unit['total_bytes'])):raise ValueError()
        totals(value,unit)
        for name in ('root_sha256','roster_sha256','initial_proof_sha256','final_authorization_sha256'):identifier(value[name],long=True)
        observed=source_reply(value['final_source_reply'],dict(grant,epoch=value['epoch']),unit,value['completed_at'])
        if observed['phase']!='verified' or observed['processed_objects']!=unit['object_count']:raise ValueError()
        if proof is not None:
            permission=proof['authorization']['payload']
            if (permission['epoch']!=value['epoch'] or digest(proof['authorization'])!=value['final_authorization_sha256'] or
                    proof['source_reply']!=value['final_source_reply']):raise ValueError()
            verify_source(proof['source_reply'],master_key,proof['authorization'],
                dict(grant,epoch=value['epoch'],runner_id=permission['runner_id']),unit,value['completed_at'])
        return value
    except (p.ProtocolError,ValueError,TypeError,KeyError,RecursionError):raise TransferError('migration_cancel_private_receipt_invalid') from None
