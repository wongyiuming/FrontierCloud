"""Host-only installation selection, retained-source binding and continuation."""
from __future__ import annotations

from pathlib import Path
import re

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_install_package as packages
from scripts import operator_registry as registry

PROTOCOL={'version':1,'entry':'scripts/frontiercloud_operator.py','handoff':'--operator-handoff',
    'roles':['business','storage']}


def recorded(directory):
    """Observe a reference before selecting its version's state implementation."""
    directory=backup.absolute(directory)
    path=directory/packages.INSTALLATION
    if not (path.exists() or path.is_symlink()):return None,None,None
    reference=registry.private_json(path)
    if (not isinstance(reference,dict) or reference.get('version')!=1 or reference.get('phase')!='installation_operation'
            or registry.sealed(reference)!=reference or reference.get('project_directory')!=str(directory)
            or not isinstance(reference.get('id'),str) or not re.fullmatch('[a-f0-9]{32}',reference['id'])
            or not isinstance(reference.get('project'),str) or not backup.NAME.fullmatch(reference['project'])):
        mapping.fail('install_reference_invalid','the private installation reference is invalid')
    from scripts.operator_entry import _private_directory
    folder=_private_directory(Path(reference['package_directory']))
    receipt=registry.private_json(folder/'install-package.json')
    state=registry.private_json(folder/'install-state.json')
    if (not isinstance(receipt,dict) or receipt.get('version')!=1 or receipt.get('phase')!='install_package_prepared'
            or not isinstance(state,dict) or state.get('version')!=1 or registry.sealed(state)!=state
            or not isinstance(state.get('phase'),str) or not 0<len(state['phase'])<=128
            or type(state.get('revision')) is not int or state['revision']<1
            or type(state.get('business_may_have_written')) is not bool
            or backup.digest(folder/'install-package.json')[1]!=reference.get('package_sha256')
            or state.get('package_sha256')!=reference['package_sha256']
            or any(reference.get(key)!=receipt.get(key) or reference.get(key)!=state.get(key)
                for key in ('id','project','project_directory'))
            or state.get('directory_identity')!=registry.root_identity(directory)):
        mapping.fail('install_reference_changed','the indexed installation lost its package, directory or journal binding')
    return reference,receipt,state


def complete(state):
    if not state or state['phase']!='ready':return False
    result=state.get('install_result',{});identity=state.get('identity',{})
    if (not isinstance(result,dict) or not isinstance(identity,dict) or result.get('phase')!='ready'
            or result.get('installation_complete') is not True or result.get('project')!=state['project']
            or state.get('business_may_have_written') is not True or result.get('node_id')!=identity.get('node_id')
            or result.get('generation')!=identity.get('generation')
            or not isinstance(result.get('node_id'),str) or not re.fullmatch('[a-f0-9]{32}',result['node_id'])
            or type(result.get('generation')) is not int or result['generation']<1):
        mapping.fail('install_completion_invalid','completed installation lacks its bound node result')
    return True


def stage(path):
    from scripts.operator_entry import _stage
    checked=_stage(path)
    protocol=path/'source/protocol/python-install.json'
    if not protocol.is_file() or backup.bounded_json(protocol)!=PROTOCOL:
        mapping.fail('source_install_missing','the selected release has no supported source-install protocol')
    return checked


def bind(receipt,path,checked):
    if (backup.absolute(receipt['stage_directory'])!=path or receipt.get('source_stage_sha256')!=checked['stage_sha256']
            or receipt.get('provenance')!=checked['provenance']):
        mapping.fail('install_source_changed','installation continuation must execute its recorded verified source')


def continue_install(directory,path,request,checked):
    from scripts.operator_install_start import Installation
    selected=Path(request['stage_directory'])
    if type(request.get('storage')) is not bool:
        mapping.fail('install_role_invalid','the private installation role is invalid')
    if request['intent']=='new_install':
        package=packages.prepare(directory,selected,path.parent/'installed',storage=request['storage'])
        operation=Installation.begin(package)
    else:
        operation=Installation.load(directory)
        bind(operation.package.receipt,selected,checked)
        if (operation.package.receipt['fresh_resources']['role']=='storage') is not request['storage']:
            mapping.fail('install_role_changed','the recorded installation role cannot change')
    return operation.start()
