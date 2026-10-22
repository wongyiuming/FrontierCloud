"""Identity-pinned partial/absent private-copy intents, without cancellation authority.

Callers must stop native I/O, verify fresh Source retention and persist an intent
before unlinking. These primitives neither publish nor release any reservation.
An absent copy is recorded as absent, never as a fully copied/deleted object.
"""
from app.services import resource_cancel_files as cancel,resource_transfer_files as staging
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import TransferError,identifier

FORMAT='frontiercloud-python-private-copy-unlink'


def _validate(root,task_id,unit,obj,expected_root):
    cancel._validate(root,task_id,unit,obj,expected_root)
    if (unit['state'] not in {'pending','copying','verified'} or 'ready' in unit['publication'] or
            type(obj['expected_bytes']) is not int or obj['expected_bytes']<=0):
        raise TransferError('migration_cancel_private_unit_required')


def inspect(root,task_id,unit,obj,expected_root,stage_identity):
    _validate(root,task_id,unit,obj,expected_root)
    if not isinstance(stage_identity,str):raise TransferError('migration_cancel_private_stage_changed')
    if stage_identity:identifier(stage_identity,long=True)
    actual=staging.inspect_stage(root,task_id,obj,expected_root)
    if actual is None:
        if stage_identity:raise TransferError('migration_cancel_private_stage_missing')
    elif actual['identity']!=stage_identity and (stage_identity or actual['bytes']):
        raise TransferError('migration_cancel_private_stage_changed')
    return dict(format=FORMAT,version=1,task_id=task_id,unit_id=unit['unit_id'],logical_id=obj['logical_id'],
        object_sha256=digest(obj),root_sha256=digest(expected_root),present=actual is not None,stage=actual)


def validate(intent,task_id,unit,obj,expected_root):
    """Pure validation for native replay and offline history inspection."""
    expected=dict(format=FORMAT,version=1,task_id=task_id,unit_id=unit['unit_id'],logical_id=obj['logical_id'],
        object_sha256=digest(obj),root_sha256=digest(expected_root))
    try:
        if (not isinstance(intent,dict) or intent.keys()!={*expected,'present','stage'} or
                any(intent.get(name)!=value for name,value in expected.items()) or type(intent['version']) is not int or
                type(intent['present']) is not bool):raise ValueError()
        actual=intent['stage']
        if not intent['present']:
            if actual is not None:raise ValueError()
            return dict(deleted_count=0,deleted_bytes=0,absent_count=1)
        if (not isinstance(actual,dict) or actual.keys()!={'identity','version','bytes'} or
                type(actual['bytes']) is not int or not 0<=actual['bytes']<=obj['expected_bytes']):raise ValueError()
        identifier(actual['identity'],long=True);identifier(actual['version'],long=True)
        return dict(deleted_count=1,deleted_bytes=actual['bytes'],absent_count=0)
    except (TransferError,ValueError,KeyError,TypeError):raise TransferError('migration_cancel_private_intent_invalid') from None


def unlink(root,task_id,unit,obj,expected_root,intent):
    _validate(root,task_id,unit,obj,expected_root)
    validate(intent,task_id,unit,obj,expected_root)
    if intent['present']:
        actual=intent['stage']
        staging.unlink_stage(root,task_id,obj,expected_root,actual['identity'],actual['version'])
    elif staging.inspect_stage(root,task_id,obj,expected_root) is not None:
        raise TransferError('migration_cancel_private_stage_reappeared')
