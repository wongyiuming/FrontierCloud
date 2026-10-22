"""Versioned host bootstrap: verified source owns its upgrade implementation."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import sys
import uuid

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_prepare as preparation
from scripts import operator_registry as registry
from scripts import operator_release as releases
from scripts import operator_source as sources
from scripts import operator_install_entry as installations

HOST_ROOT = Path('/var/lib/frontiercloud-operator')
PROTOCOL = {'version':1,'entry':'scripts/frontiercloud_operator.py','handoff':'--operator-handoff'}
TERMINAL = {'opened':('open_result','upgrade_complete'),
    'restored_opened':('recovery','recovery_complete'),'aborted':('abort_result','abort_complete')}


def _private_directory(path, *, create=False):
    path=backup.absolute(path,exists=False)
    if create and not path.exists():
        path.mkdir(mode=0o700)
        backup.fsync_directory(path.parent)
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700:
        mapping.fail('operator_host_directory_unsafe','host operator material needs a private owned directory')
    return path


def _outside_mounts(docker, root):
    docker.local_root()
    for container in docker.containers():
        for row in container.get('Mounts',[]):
            if row.get('Type') not in {'bind','volume'}:
                continue
            source=backup.absolute(row['Source'],exists=False)
            if source==root or source in root.parents or root in source.parents:
                mapping.fail('operator_records_overlap_mount','private host operator material overlaps a container mount')


def _operation(directory):
    reference=registry.operation(directory)
    if reference is None:
        return None,None
    state=registry.private_json(Path(reference['prepared_directory'])/'upgrade-state.json')
    if not isinstance(state,dict) or not isinstance(state.get('phase'),str) or not 0<len(state['phase'])<=128:
        mapping.fail('upgrade_identity_changed','the recorded operation has no valid phase')
    body={key:value for key,value in state.items() if key!='state_sha256'}
    if (state.get('version')!=1 or mapping.checksum(body)!=state.get('state_sha256')
            or any(state.get(key)!=reference.get(key) for key in ('id','project','project_directory','prepared_sha256'))):
        mapping.fail('upgrade_identity_changed','the recorded operation no longer binds its preparation')
    return reference,state


def _complete(state):
    if not state or state.get('phase') not in TERMINAL:
        return False
    key,flag=TERMINAL[state['phase']]
    result=state.get(key)
    if key=='recovery' and isinstance(result,dict):
        result=result.get('open_result')
    if (not isinstance(result,dict) or result.get('phase')!=state['phase']
            or result.get(flag) is not True or result.get('project')!=state['project']):
        mapping.fail('upgrade_completion_invalid','terminal operation lacks its bound completion result')
    return True


def _stage(path):
    path=_private_directory(path)
    registry.private_json(path/'source-receipt.json')
    checked=sources.verify_stage(path)
    if checked['provenance'].get('public_provenance_verified') is not True:
        mapping.fail('source_not_published','host handoff requires verified published Python source')
    protocol=path/'source/protocol/python-operator.json'
    if not protocol.is_file() or backup.bounded_json(protocol)!=PROTOCOL:
        mapping.fail('source_operator_missing','the selected release does not support this host operator protocol')
    entry=path/'source'/PROTOCOL['entry']
    if not entry.is_file():
        mapping.fail('source_operator_missing','the selected release has no host operator entry')
    return checked


def _lock(path):
    import fcntl
    descriptor=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    try:
        info=os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o600:
            mapping.fail('operator_lock_unsafe','the host entry lock is not a private owned file')
        try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            mapping.fail('operator_entry_busy','another host upgrade entry is already running for this directory')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def start(directory, *, command='upgrade', host_root=HOST_ROOT,storage=None):
    """Public path: no caller-supplied version, source, plan or acceptance flag."""
    if command not in {'upgrade','resume','recover','install'} or type(storage) not in {bool,type(None)} or command!='install' and storage is not None:
        mapping.fail('operator_command_invalid','unsupported host entry operation')
    directory=backup.absolute(directory)
    docker=mapping.ComposeDocker(directory,[],'host-entry',[])
    host_root=backup.absolute(host_root,exists=False)
    _outside_mounts(docker,host_root)
    _private_directory(host_root,create=True)
    cache=_private_directory(host_root/hashlib.sha256(str(directory).encode()).hexdigest(),create=True)
    lock_path=cache/'entry.lock'
    descriptor=_lock(lock_path)
    try:
        return _start_locked(directory,command,host_root,cache,descriptor,docker,storage=storage)
    finally:
        os.close(descriptor)


def _start_locked(directory,command,host_root,cache,descriptor,docker, *, storage=None):
    reference,state=_operation(directory)
    install_reference,install_receipt,install_state=installations.recorded(directory) if reference is None else (None,None,None)
    previous_install=backup.digest(directory/installations.packages.INSTALLATION)[1] if install_reference else None
    if command=='install' and reference:
        mapping.fail('install_existing_deployment','an existing upgrade is preserved; use its recorded operation')
    installed=installations.complete(install_state)
    if command=='upgrade' and install_reference and not installed:
        mapping.fail('installation_incomplete','resume the recorded installation with install before upgrading')
    previous=backup.digest(directory/registry.OPERATION)[1] if reference else None
    if command not in {'upgrade','install'} and reference is None:
        mapping.fail('upgrade_not_found','this directory has no recorded host upgrade operation')
    receipt=registry.private_json(Path(reference['prepared_directory'])/'prepared.json') if reference else None
    original_stage=backup.absolute(receipt['stage_directory']) if receipt else None
    original_checked=_stage(original_stage) if reference else None
    if reference and (original_checked['stage_sha256']!=receipt['source_stage_sha256'] or original_checked['provenance']!=receipt['provenance']):
        mapping.fail('source_stage_changed','the recorded operation lost its selected source binding')
    terminal=bool(reference) and _complete(state)
    select_retained=command=='upgrade' and terminal and Path(__file__).parent.parent!=original_stage/'source'
    installed_stage=backup.absolute(install_receipt['stage_directory']) if install_receipt else None
    select_installed=command=='upgrade' and installed and Path(__file__).parent.parent!=installed_stage/'source'
    retained=reference is not None and (command!='upgrade' or not terminal or select_retained)
    deployment=None
    if command=='install' and install_reference or select_installed:
        stage=installed_stage
        checked=installations.stage(stage)
        installations.bind(install_receipt,stage,checked)
        recorded_storage=install_receipt['fresh_resources']['role']=='storage'
        if storage is not None and storage is not recorded_storage:
            mapping.fail('install_role_changed','the recorded installation role cannot change')
        storage=recorded_storage
        intent='select_install_upgrade' if select_installed else 'resume_install'
    elif command=='install':
        selected=releases.select_latest()
        attempt=_private_directory(cache/uuid.uuid4().hex,create=True)
        stage=attempt/'stage'
        sources.stage(stage,_selected=selected)
        checked=installations.stage(stage)
        storage=False if storage is None else storage
        intent='new_install'
    elif retained:
        stage=original_stage
        checked=original_checked
        intent=command if command!='upgrade' else 'select_upgrade' if select_retained else 'resume'
    else:
        deployment=mapping.Deployment.discover(directory)
        preparation.host_records_outside_data(deployment)
        selected=releases.select_latest()
        if deployment.operator_phase=='ready':
            current,receipt=registry.read(directory,deployment.plan.name)
            completed=state and state['phase']=='opened' or installed and current.get('origin')=='source_install'
            if (completed and receipt['provenance']==selected and all(row['running'] for name,row in deployment.observed.items()
                    if name not in {'secrets-init','media-init','certificate-init'})):
                return {'state':'already_current','revision':selected['revision'],'project':deployment.plan.name,
                    'deployment_changed':False,'business_read_verified':False}
        attempt=_private_directory(cache/uuid.uuid4().hex,create=True)
        stage=attempt/'stage'
        sources.stage(stage,_selected=selected)
        checked=_stage(stage)
        intent='new_upgrade'
    attempt=_private_directory(cache/uuid.uuid4().hex,create=True)
    request=registry.sealed({'version':1,'phase':'host_handoff','project_directory':str(directory),
        'directory_identity':registry.root_identity(directory),'stage_directory':str(stage),
        'source_stage_sha256':checked['stage_sha256'],'command':command,
        'intent':intent,'previous_installation_sha256':previous_install,
        'previous_operation_sha256':previous,'old_deployment':deployment.summary() if deployment else None,
        'host_root':str(host_root),'lock_path':str(cache/'entry.lock'),'lock_fd':descriptor,
        **({'storage':storage} if command=='install' else {})})
    preparation._store(attempt/'request.json',request)
    arguments=[sys.executable,'-I','-B',str(stage/'source'/PROTOCOL['entry']),PROTOCOL['handoff'],str(attempt/'request.json')]
    preparation._store(attempt/'handoff-input.json',{'argv':arguments,'working_directory':str(directory),
        'source_stage_sha256':checked['stage_sha256'],'intent':request['intent']})
    _outside_mounts(docker,host_root)
    if (installations.stage(stage) if command=='install' or select_installed else _stage(stage))!=checked:
        mapping.fail('source_stage_changed','verified source changed before host execution')
    os.set_inheritable(descriptor,True)
    # Exec retains the directory lock across the version change. There is
    # no orphan supervising parent or timeout that can outlive the lock.
    os.chdir(directory)
    os.execve(sys.executable,arguments,docker.environment)
    raise RuntimeError('host exec returned unexpectedly')

def continue_upgrade(path):
    """Private protocol, executed only from its bound verified source tree."""
    import fcntl
    from scripts.operator_upgrade import Upgrade
    path=backup.absolute(path)
    _private_directory(path.parent)
    request=registry.private_json(path)
    if not isinstance(request,dict):
        mapping.fail('operator_handoff_invalid','the private source handoff is not an object')
    command=request.get('command','upgrade')
    if (request.get('version')!=1 or request.get('phase')!='host_handoff' or registry.sealed(request)!=request
            or not isinstance(command,str) or command not in {'upgrade','resume','recover','install'}
            or request.get('intent') not in ({'new_upgrade','resume','select_upgrade','select_install_upgrade'} if command=='upgrade'
                else {'new_install','resume_install'} if command=='install' else {command})):
        mapping.fail('operator_handoff_invalid','the private source handoff is incomplete')
    stage=backup.absolute(request['stage_directory'])
    checked=installations.stage(stage) if command=='install' or request['intent']=='select_install_upgrade' else _stage(stage)
    if checked['stage_sha256']!=request['source_stage_sha256'] or Path(__file__).parent.parent!=stage/'source':
        mapping.fail('operator_handoff_source_changed','handoff must execute its exact verified source')
    descriptor=request['lock_fd']
    if not isinstance(descriptor,int) or isinstance(descriptor,bool) or descriptor<3:
        mapping.fail('operator_handoff_lock_changed','the inherited host entry lock is invalid')
    lock_path=backup.absolute(request['lock_path'])
    _private_directory(lock_path.parent)
    observed=os.fstat(descriptor)
    expected=lock_path.lstat()
    if (not stat.S_ISREG(observed.st_mode) or observed.st_nlink!=1 or observed.st_uid!=os.geteuid()
            or stat.S_IMODE(observed.st_mode)!=0o600 or (observed.st_dev,observed.st_ino)!=(expected.st_dev,expected.st_ino)):
        mapping.fail('operator_handoff_lock_changed','the inherited host entry lock no longer identifies its file')
    try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        mapping.fail('operator_entry_busy','another host entry owns this directory')
    os.set_inheritable(descriptor,False)
    try:
        directory=backup.absolute(request['project_directory'])
        if registry.root_identity(directory)!=request['directory_identity']:
            mapping.fail('operator_handoff_directory_changed','the deployment directory changed across source handoff')
        reference,state=_operation(directory)
        install_reference,install_receipt,install_state=installations.recorded(directory) if reference is None else (None,None,None)
        previous_install=backup.digest(directory/installations.packages.INSTALLATION)[1] if install_reference else None
        if previous_install!=request.get('previous_installation_sha256'):
            mapping.fail('operator_handoff_installation_changed','the indexed installation changed across source handoff')
        previous=backup.digest(directory/registry.OPERATION)[1] if reference else None
        if previous!=request['previous_operation_sha256']:
            mapping.fail('operator_handoff_operation_changed','the indexed operation changed across source handoff')
        preparation._store(path.parent/'handoff-started.json',{'version':1,'phase':'source_entered',
            'source_stage_sha256':checked['stage_sha256'],'intent':request['intent']})
        if command=='install':
            if reference is not None or (install_reference is not None) is not (request['intent']=='resume_install'):
                mapping.fail('operator_handoff_installation_changed','installation continuation no longer matches its indexed state')
            if install_reference:installations.bind(install_receipt,stage,checked)
            return installations.continue_install(directory,path,request,checked)
        if request['intent']=='select_install_upgrade':
            if reference is not None or not installations.complete(install_state):
                mapping.fail('operator_handoff_installation_changed','selection requires the same completed installation')
            installations.bind(install_receipt,stage,checked)
            return _reselect(directory,request,lock_path,descriptor)
        if request['intent'] in {'resume','recover','select_upgrade'}:
            if reference is None:
                mapping.fail('operator_handoff_operation_changed','the selected operation is no longer indexed')
            receipt=registry.private_json(Path(reference['prepared_directory'])/'prepared.json')
            if (backup.absolute(receipt['stage_directory'])!=stage or receipt['source_stage_sha256']!=checked['stage_sha256']
                    or receipt['provenance']!=checked['provenance']):
                mapping.fail('operator_handoff_source_changed','the indexed operation binds another source stage')
            if request['intent']=='recover':
                from scripts.operator_abort import recover
                return recover(directory)
            if request['intent']=='select_upgrade':
                if not _complete(state):
                    mapping.fail('operator_handoff_operation_changed','new-version selection requires the same completed operation')
                return _reselect(directory,request,lock_path,descriptor)
        else:
            deployment=mapping.Deployment.discover(directory)
            if deployment.summary()!=request['old_deployment']:
                mapping.fail('deployment_inputs_changed','the deployment changed across source handoff')
            prepared=preparation.prepare(deployment,stage,path.parent/'prepared')
            Upgrade.begin(deployment,prepared)
        return Upgrade.advance(directory)
    finally:
        os.close(descriptor)


def _reselect(directory,request,lock_path,descriptor):
    host_root=_private_directory(Path(request['host_root']))
    cache=_private_directory(host_root/hashlib.sha256(str(directory).encode()).hexdigest())
    if lock_path!=cache/'entry.lock':
        mapping.fail('operator_handoff_lock_changed','selection must retain this deployment directory lock')
    docker=mapping.ComposeDocker(directory,[],'host-entry',[])
    _outside_mounts(docker,host_root)
    return _start_locked(directory,'upgrade',host_root,cache,descriptor,docker)
