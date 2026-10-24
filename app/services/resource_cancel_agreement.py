"""Short-lived Master agreement to a native Source census, not a receipt."""
from app.services.federation import protocol as p
from app.services.resource_cancel_census import verify_proof
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import TransferError,identifier

FORMAT='frontiercloud-python-master-cancel-current-agreement'
IDENTITIES=('task_id','master_id','source_id','target_id','source_relationship','target_relationship',
    'plan_sha256','units_sha256','source_binding','target_binding','source_key','target_key')
NUMBERS=('version','epoch','unit_count','object_count','total_bytes','committed_units','committed_objects',
    'committed_bytes','retained_objects','retained_bytes','issued_at','expires_at')


def verify(envelope,master_key,grant,counts,now,*,source_census):
    try:
        if (grant['purpose']!='finalize' or not isinstance(envelope,dict) or envelope.keys()!={'payload','signature'}
                or not isinstance(counts,tuple) or len(counts)!=3 or any(type(item) is not int or item<=0 for item in counts)):
            raise ValueError()
        value=p.verify(master_key,envelope)
        proof=verify_proof(source_census,grant,now)
        expected=dict(format=FORMAT,version=1,purpose='release-retained-source',**{name:grant[name] for name in IDENTITIES},
            epoch=grant['epoch'],runner_id=grant['runner_id'],unit_count=counts[0],object_count=counts[1],total_bytes=counts[2],
            census_sha256=digest(source_census),catalog_sha256=proof['catalog_sha256'],roots_sha256=proof['roots_sha256'],
            retained_objects=proof['object_count'],retained_bytes=proof['total_bytes'],
            source_files_removed=False,ownership_changed=False,task_completed=False)
        extra={'committed_units','committed_objects','committed_bytes','master_state_sha256','issued_at','expires_at'}
        if (not isinstance(value,dict) or value.keys()!={*expected,*extra} or any(value.get(name)!=item for name,item in expected.items())
                or any(type(value[name]) is not int or not 0<=value[name]<2**63 for name in NUMBERS)
                or any(value[name] is not False for name in ('source_files_removed','ownership_changed','task_completed'))
                or not 0<value['issued_at']<=now+5 or not value['issued_at']<value['expires_at']<=value['issued_at']+45
                or value['expires_at']!=min(proof['expires_at'],grant['expires_at']) or now>=value['expires_at']
                or not 0<=value['committed_units']<=value['committed_objects']<=value['object_count']
                or value['committed_units']>value['unit_count'] or value['committed_bytes']>value['total_bytes']
                or value['committed_objects']>value['committed_bytes']
                or (value['committed_units']==0)!=(value['committed_objects']==0)
                or (value['committed_objects']==0)!=(value['committed_bytes']==0)
                or value['retained_objects']>value['object_count']-value['committed_objects']
                or value['retained_bytes']>value['total_bytes']-value['committed_bytes']
                or value['retained_objects']>value['retained_bytes']
                or (value['retained_objects']==0)!=(value['retained_bytes']==0)):
            raise ValueError()
        identifier(value['master_state_sha256'],long=True)
        return value
    except (p.ProtocolError,ValueError,KeyError,TypeError,RecursionError):
        raise TransferError('migration_cancel_agreement_invalid') from None
