"""Durable signed retirement observations, never deletion or transfer grants."""
from app.services.federation import protocol as p
from app.services.node_retirement_fences import RetirementError
from app.services.resource_observation import digest

FORMAT='frontiercloud-python-node-retirement-receipt'
BASE={'format','version','side','node_id','retirement_id','relationship_id','master_id','member_id',
    'binding_sha256','started_at','completed_at','history'}
INSPECTION={'version','retained_files','retained_bytes','directories','external_control_files','external_control_bytes','roots_sha256'}


def fail():raise RetirementError('retirement_receipt_invalid')


def integer(value,*,positive=False):
    if type(value) is not int or not (0<value<2**63 if positive else 0<=value<2**63):fail()
    return value


def checksum(value):
    if not isinstance(value,str) or not p.OBJECT_ID.fullmatch(value):fail()
    return value


def _base(envelope,key,binding,side,now):
    try:
        if not isinstance(envelope,dict) or envelope.keys()!={'payload','signature'} or len(p.canonical(envelope))>16384:fail()
        value=p.verify(key,envelope)
        extra={'roots_sha256','inspection'} if side=='peer' else {'peer_receipt','peer_receipt_sha256'}
        if not isinstance(value,dict) or value.keys()!=BASE|extra:fail()
        expected=dict(format=FORMAT,version=1,side=side,node_id=binding['member_id' if side=='peer' else 'master_id'],
            **{name:binding[name] for name in ('retirement_id','relationship_id','master_id','member_id')},binding_sha256=digest(binding))
        if any(value.get(name)!=item for name,item in expected.items()) or type(value['version']) is not int:fail()
        integer(value['started_at'],positive=True);integer(value['completed_at'],positive=True)
        if not value['started_at']<=value['completed_at']<=now+p.AUTH_SKEW_SECONDS:fail()
        history=value['history'];name='native_tasks' if side=='peer' else 'master_tasks'
        if not isinstance(history,dict) or history.keys()!={name}:fail()
        integer(history[name])
        return value
    except RetirementError:raise
    except (ValueError,TypeError,KeyError,RecursionError):fail()


def peer(envelope,key,binding,now,*,roots=None):
    value=_base(envelope,key,binding,'peer',now);inspection=value['inspection']
    checksum(value['roots_sha256'])
    if not isinstance(inspection,dict) or inspection.keys()!=INSPECTION or type(inspection['version']) is not int or inspection['version']!=1:fail()
    for name in INSPECTION-{'version','roots_sha256'}:integer(inspection[name])
    checksum(inspection['roots_sha256'])
    if roots is not None and (not isinstance(roots.get('native'),dict) or value['roots_sha256']!=digest(roots) or
            inspection['roots_sha256']!=digest(roots['native'])):fail()
    return value


def master(envelope,key,peer_key,binding,now,*,roots=None):
    value=_base(envelope,key,binding,'master',now)
    observed=peer(value['peer_receipt'],peer_key,binding,now)
    if value['peer_receipt_sha256']!=digest(value['peer_receipt']) or observed['completed_at']>value['completed_at']+p.AUTH_SKEW_SECONDS:fail()
    if roots is not None and roots!=dict(version=1,peer_roots_sha256=observed['roots_sha256']):fail()
    return value


def recorded(store,node,relation,row,now):
    if row['side']=='peer':
        value=peer(row['receipt'],p.public_key(store.unseal(node['private_key'])),row['binding'],now,roots=row['roots'])
    else:
        value=master(row['receipt'],p.public_key(store.unseal(node['private_key'])),relation['peer_key'],row['binding'],now,roots=row['roots'])
    if value['started_at']!=row['started_at'] or value['completed_at']!=row['completed_at']:fail()
    return value
