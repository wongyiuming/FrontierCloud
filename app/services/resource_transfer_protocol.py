"""Short Master grants and Target proofs; no shared Master credential delegation."""
import time
import uuid

from app.services.federation import protocol as p
from app.services.resource_observation import digest
from app.services.resource_pool import validate_media_path

WINDOW_SECONDS=45
WINDOW_BYTES=4*1024*1024
CHUNK_BYTES=64*1024
CONTROL_BYTES=16*1024
SOURCE_PATH='/internal/v1/migration/transfer/source'
GRANT_FIELDS={'format','version','task_id','master_id','source_id','target_id','source_relationship','target_relationship',
    'source_endpoint','target_endpoint','source_key','target_key','source_binding','target_binding','plan_sha256',
    'units_sha256','epoch','runner_id','purpose','created_at','expires_at','nonce','object'}
OBJECT_FIELDS={'logical_id','unit_id','resource_kind','source_object','source_path','target_path','source_version',
    'unit_version','expected_bytes','sha256','metadata_sha256','encryption_sha256','user_id'}
IMMUTABLE_FIELDS=('task_id','master_id','source_id','target_id','source_relationship','target_relationship',
    'source_endpoint','target_endpoint','source_key','target_key','source_binding','target_binding','plan_sha256','units_sha256','object')


class TransferError(p.ProtocolError):
    pass


def identifier(value,*,long=False):
    if not isinstance(value,str) or not (p.OBJECT_ID if long else p.IDENTIFIER).fullmatch(value):
        raise TransferError('migration_transfer_identity_invalid')
    return value


def grant_shape(value,now):
    if (not isinstance(value,dict) or value.keys()!=GRANT_FIELDS or value['format']!='frontiercloud-python-transfer-grant' or
            type(value['version']) is not int or value['version']!=1):
        raise TransferError('migration_transfer_grant_invalid')
    for name in ('task_id','master_id','source_id','target_id','source_relationship','target_relationship','runner_id','nonce'):
        identifier(value[name])
    for name in ('source_binding','target_binding','plan_sha256','units_sha256'):identifier(value[name],long=True)
    if (len({value['master_id'],value['source_id'],value['target_id']})!=3 or
            value['source_relationship']==value['target_relationship'] or type(value['epoch']) is not int or value['epoch']<1 or
            value['purpose'] not in {'probe','register','copy','abort','cancel','publish','verify','commit','inspect','cleanup','finalize'} or
            any(type(value[name]) is not int for name in ('created_at','expires_at')) or
            value['expires_at']-value['created_at']!=WINDOW_SECONDS or
            not value['created_at']-5<=now<value['expires_at']):
        raise TransferError('migration_transfer_grant_expired_or_invalid')
    for side in ('source','target'):
        if p.endpoint(value[side+'_endpoint'])!=value[side+'_endpoint'] or len(p.decode(value[side+'_key']))!=32:
            raise TransferError('migration_transfer_peer_invalid')
    obj=value['object']
    if value['purpose'] not in {'copy','register'}:
        if obj is not None:raise TransferError('migration_transfer_grant_invalid')
        return value
    return object_shape(value)


def object_shape(value):
    """Validate immutable object metadata without issuing a transfer capability."""
    obj=value['object']
    if not isinstance(obj,dict) or obj.keys() not in (OBJECT_FIELDS,OBJECT_FIELDS|{'master_encryption_sha256'}):
        raise TransferError('migration_transfer_object_invalid')
    kind=obj['resource_kind']
    for name in ('unit_id','source_version','unit_version','sha256','metadata_sha256','encryption_sha256'):identifier(obj[name],long=True)
    master=target_encryption_digest(obj)
    if (master!=obj['encryption_sha256'] and obj['encryption_sha256']!=digest(None) or
            kind=='recording' and master!=digest(None)):
        raise TransferError('migration_transfer_object_invalid')
    identifier(obj['logical_id'],long=kind=='media');identifier(obj['source_object'],long=kind=='media')
    if type(obj['expected_bytes']) is not int or not 0<obj['expected_bytes']<2**53:
        raise TransferError('migration_transfer_object_invalid')
    for side in ('source','target'):
        path=obj[side+'_path']
        if kind=='media':
            validate_media_path(path)
            if obj['user_id'] or obj['source_path']!=obj['target_path']:raise TransferError('migration_transfer_object_invalid')
        elif kind=='recording':
            identifier(obj['user_id'])
            if path!=value[side+'_relationship']+'/'+obj['user_id']+'/'+obj['source_object']+'.bin':
                raise TransferError('migration_transfer_object_invalid')
        else:raise TransferError('migration_transfer_object_invalid')
    if obj['unit_id']!=digest([kind,value['source_id'],obj['source_path'].rsplit('/',1)[0]]):
        raise TransferError('migration_transfer_object_invalid')
    return value


def target_encryption_digest(obj):
    """Old immutable grants used one descriptor digest at both peers."""
    value=obj.get('master_encryption_sha256',obj['encryption_sha256'])
    return identifier(value,long=True)


def immutable(grant):
    return {name:grant[name] for name in IMMUTABLE_FIELDS}


def immutable_shape(value):
    if not isinstance(value,dict) or value.keys()!=set(IMMUTABLE_FIELDS):raise TransferError('migration_transfer_binding_invalid')
    for name in ('task_id','master_id','source_id','target_id','source_relationship','target_relationship'):identifier(value[name])
    for name in ('source_binding','target_binding','plan_sha256','units_sha256'):identifier(value[name],long=True)
    if len({value['master_id'],value['source_id'],value['target_id']})!=3 or value['source_relationship']==value['target_relationship']:
        raise TransferError('migration_transfer_binding_invalid')
    for side in ('source','target'):
        if p.endpoint(value[side+'_endpoint'])!=value[side+'_endpoint'] or len(p.decode(value[side+'_key']))!=32:
            raise TransferError('migration_transfer_peer_invalid')
    return object_shape(value)


def target_request(private,grant,*,offset=0,length=0,now=None):
    now=int(time.time()) if now is None else int(now)
    payload=dict(format='frontiercloud-python-transfer-request',version=1,grant_sha256=digest(grant),
        target_id=grant['payload']['target_id'],nonce=uuid.uuid4().hex,created_at=now,offset=offset,length=length,path=SOURCE_PATH)
    return dict(grant=grant,proof=p.sign(private,payload))


def verify_target(value,grant,now):
    if not isinstance(value,dict) or value.keys()!={'grant','proof'}:raise TransferError('migration_transfer_request_invalid')
    proof=p.verify(grant['target_key'],value['proof'])
    expected={'format','version','grant_sha256','target_id','nonce','created_at','offset','length','path'}
    if (proof.keys()!=expected or proof['format']!='frontiercloud-python-transfer-request' or
            type(proof['version']) is not int or proof['version']!=1 or proof['grant_sha256']!=digest(value['grant']) or
            proof['target_id']!=grant['target_id'] or proof['path']!=SOURCE_PATH or
            type(proof['created_at']) is not int or not grant['created_at']<=proof['created_at']<=now+5 or
            abs(now-proof['created_at'])>WINDOW_SECONDS):
        raise TransferError('migration_transfer_request_invalid')
    identifier(proof['nonce'])
    if any(type(proof[name]) is not int or proof[name]<0 for name in ('offset','length')):
        raise TransferError('migration_transfer_range_invalid')
    if grant['purpose']=='copy':
        if not 0<proof['length']<=WINDOW_BYTES or proof['offset']+proof['length']>grant['object']['expected_bytes']:
            raise TransferError('migration_transfer_range_invalid')
    elif proof['offset'] or proof['length']:raise TransferError('migration_transfer_range_invalid')
    return proof
