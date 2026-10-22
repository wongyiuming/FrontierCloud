"""Private host receipt for recognizing a generated Compose deployment."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import uuid

from scripts import operator_backup as backup

CURRENT = '.frontiercloud-operator-current.json'
OPERATION = '.frontiercloud-operator-operation.json'
PHASES = frozenset({'active_fenced', 'opening', 'ready'})


def fail(code, message):
    from scripts.operator_deployment import fail as refuse
    refuse(code, message)


def private_json(path):
    path = backup.absolute(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
        fail('operator_receipt_unsafe', 'operator receipts must be private host-owned regular files')
    return backup.bounded_json(path)


def root_identity(path):
    path = backup.absolute(path)
    value = path.lstat()
    if not stat.S_ISDIR(value.st_mode):
        fail('operator_root_changed', 'a recorded persistent root is no longer a real directory')
    return {'path':str(path),'device':value.st_dev,'inode':value.st_ino,
        'uid':value.st_uid,'gid':value.st_gid,'mode':stat.S_IMODE(value.st_mode)}


def sealed(value):
    body = {key:item for key,item in value.items() if key != 'receipt_sha256'}
    return {**body,'receipt_sha256':hashlib.sha256(backup.json_bytes(body)).hexdigest()}


def _reference_hash(directory, project, filename):
    path = backup.absolute(directory)/filename
    if not path.exists() and not path.is_symlink():
        return None
    value = private_json(path)
    if (not isinstance(value,dict) or value.get('project') != project
            or value.get('project_directory') != str(directory) or sealed(value) != value):
        fail('operator_receipt_changed', 'the existing operator receipt belongs to another or changed deployment')
    return backup.digest(path)[1]


def current_hash(directory, project):
    return _reference_hash(directory,project,CURRENT)


def operation(directory):
    directory = backup.absolute(directory)
    path = directory/OPERATION
    if not path.exists() and not path.is_symlink():
        return None
    value = private_json(path)
    if (not isinstance(value,dict) or value.get('version') != 1 or value.get('phase') != 'upgrade_operation'
            or value.get('project_directory') != str(directory) or sealed(value) != value
            or not re.fullmatch('[0-9a-f]{32}',value.get('id',''))
            or not isinstance(value.get('project'),str) or not backup.NAME.fullmatch(value['project'])):
        fail('operator_operation_invalid','the durable operation reference is invalid')
    prepared = backup.absolute(value['prepared_directory'])
    receipt = private_json(prepared/'prepared.json')
    if (backup.digest(prepared/'prepared.json')[1] != value.get('prepared_sha256')
            or receipt.get('id') != value['id'] or receipt.get('project') != value['project']
            or receipt.get('project_directory') != str(directory)):
        fail('operator_operation_changed','the durable operation reference no longer binds its original preparation')
    return value


def read(directory, project):
    """Recognize only the exact completed preparation and original private inputs."""
    from scripts import operator_deployment as mapping
    directory = backup.absolute(directory)
    path = directory/CURRENT
    digest = current_hash(directory,project)
    if digest is None:
        fail('operator_receipt_missing', 'generated Compose requires its retained host deployment receipt')
    value = private_json(path)
    if (value.get('version') != 1 or value.get('phase') not in PHASES
            or not re.fullmatch('[0-9a-f]{32}',value.get('id',''))
            or not re.fullmatch('[0-9a-f]{32}',value.get('node_id',''))
            or type(value.get('generation')) is not int or value['generation'] < 1):
        fail('operator_receipt_invalid', 'the host deployment receipt is incomplete')
    prepared = backup.absolute(value['prepared_directory'])
    receipt = private_json(prepared/'prepared.json')
    if (backup.digest(prepared/'prepared.json')[1] != value.get('prepared_sha256')
            or receipt.get('id') != value['id'] or receipt.get('project') != project
            or backup.digest(prepared/'compose.json')[1] != receipt.get('compose_sha256')):
        fail('operator_receipt_changed', 'the retained source/image preparation differs from this deployment')
    if receipt.get('phase') == 'restored_original' and value.get('origin') == 'controlled_recovery':
        from scripts.operator_recovery_open import validate_receipt
        validate_receipt(directory,value,receipt)
    elif receipt.get('phase')=='installed_package' and value.get('origin')=='source_install':
        from scripts.operator_install_start import validate_receipt
        validate_receipt(directory,value,receipt)
    elif receipt.get('phase') != 'images_prepared' or value.get('origin') is not None:
        fail('operator_receipt_changed','the deployment receipt has an unsupported preparation origin')
    files = [backup.absolute(name) for name in receipt['source_files']]
    env = [backup.absolute(name) for name in receipt['source_env_files']]
    if (not files or files[0].parent != directory or files[0].name not in mapping.BASE_FILES
            or len(files) != len(set(files)) or len(env) != len(set(env))):
        fail('operator_source_mapping_invalid', 'the retained original Compose mapping is invalid')
    implicit = directory/'.env'
    inputs = files+(env if env else [implicit] if implicit.exists() or implicit.is_symlink() else [])
    if mapping._inputs(inputs) != receipt['source_inputs']:
        fail('operator_source_inputs_changed', 'original Compose or private inputs changed; an explicit configuration mapping is required')
    roots = value.get('roots')
    if not isinstance(roots,dict) or not roots or set(roots)-backup.RESOURCE_KEYS:
        fail('operator_root_changed', 'the retained persistent root inventory is invalid')
    for original in roots.values():
        if not isinstance(original,dict) or root_identity(original['path']) != original:
            fail('operator_root_changed', 'a retained persistent root identity changed')
    if backup.digest(path)[1] != digest:
        fail('operator_receipt_changed', 'the host deployment receipt changed during discovery')
    return value,receipt


def _publish(directory, value, *, previous_sha256, filename):
    """Caller holds the project lock; preserve a foreign/newer receipt."""
    directory = backup.absolute(directory)
    path = directory/filename
    value = sealed(value)
    current = _reference_hash(directory,value['project'],filename)
    if current is not None and private_json(path) == value:
        return current
    if current != previous_sha256:
        fail('operator_receipt_changed', 'another operator receipt appeared; it is not overwritten')
    temporary = directory/('.operator-current-'+uuid.uuid4().hex+'.tmp')
    backup.private_write(temporary,backup.json_bytes(value))
    os.replace(temporary,path)
    backup.fsync_directory(directory)
    return backup.digest(path)[1]


def publish(directory, value, *, previous_sha256):
    return _publish(directory,value,previous_sha256=previous_sha256,filename=CURRENT)


def publish_operation(directory, value, *, previous_sha256):
    return _publish(directory,value,previous_sha256=previous_sha256,filename=OPERATION)
