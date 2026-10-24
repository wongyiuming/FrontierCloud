"""Journal owned fresh resources; never start services or restore old data."""
from __future__ import annotations

import os
from pathlib import Path
import uuid

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_install_package as packages
from scripts import operator_prepare as preparation
from scripts import operator_registry as registry
from scripts import operator_release as releases
from scripts import operator_source as sources

LABEL='io.frontiercloud.operator.install'
RESOURCE_PHASES={'prepared','provisioning','resources_prepared'}
START_PHASES={'starting','started','verifying','ready'}
PHASES=RESOURCE_PHASES|START_PHASES


def _fail(code,message):mapping.fail(code,message)


def _read(path):
    value=registry.private_json(path)
    if (not isinstance(value,dict) or value.get('version')!=1 or value.get('phase') not in PHASES
            or type(value.get('revision')) is not int or value['revision']<1 or registry.sealed(value)!=value
            or value.get('business_may_have_written') is not (value.get('phase') in START_PHASES)):
        _fail('install_state_invalid','installation resource journal is incomplete or changed')
    return value


def _inputs(receipt):
    paths=[Path(row['path']) for row in receipt['private_inputs']]
    implicit=Path(receipt['project_directory'])/'.env'
    if (implicit.exists() or implicit.is_symlink()) and implicit not in paths:paths.append(implicit)
    return mapping._inputs(paths)


class Installation:
    def __init__(self,package,state,verify,acceptance):
        self.package=package;self.state=state;self.verify=verify;self.acceptance=acceptance
        self.path=package.directory/'install-state.json'
        self.directory=Path(package.receipt['project_directory'])
        package.docker.audit_file=package.directory/'install-resource-inputs.jsonl'

    @classmethod
    def begin(cls,package, *, verify=sources.verify_stage,public_verify=releases.select_latest,acceptance=False):
        loaded=packages.Package.load(package.directory,verify=verify,acceptance=acceptance)
        if loaded.receipt!=package.receipt:_fail('install_package_changed','installation package changed before indexing')
        receipt=loaded.receipt;directory=Path(receipt['project_directory'])
        with backup.operation_lock(receipt['project']):
            if not receipt['acceptance_only'] and public_verify()!=receipt['provenance']:
                _fail('release_proof_changed','public source proof changed before fresh resource creation')
            source=Path(receipt['stage_directory'])/'source'
            if (_inputs(receipt)!=receipt['private_inputs']
                    or packages._material(directory,source,[Path(row['source']) for row in receipt['canonical_files']])!=receipt['canonical_files']):
                _fail('install_inputs_changed','private or canonical inputs changed before installation indexing')
            if packages.observe(directory,loaded.config,loaded.docker,(Path(receipt['stage_directory']),loaded.directory))!=receipt['fresh_resources']:
                _fail('install_inputs_changed','fresh resources changed before installation indexing')
            state=registry.sealed({'version':1,'revision':1,'phase':'prepared','id':receipt['id'],'project':receipt['project'],
                'project_directory':str(directory),'directory_identity':registry.root_identity(directory),
                'package_sha256':backup.digest(loaded.directory/'install-package.json')[1],
                'control_identity':None,'volume_identities':{},'canonical_attempts':[],'business_may_have_written':False})
            path=loaded.directory/'install-state.json'
            if path.exists() or path.is_symlink():
                old=_read(path)
                if old!=state:_fail('install_state_occupied','unindexed installation state is retained and cannot be replaced')
            else:preparation._store(path,state)
            registry._publish(directory,{'version':1,'phase':'installation_operation','id':receipt['id'],'project':receipt['project'],
                'project_directory':str(directory),'package_directory':str(loaded.directory),'package_sha256':state['package_sha256']},
                previous_sha256=None,filename=packages.INSTALLATION)
            return cls(loaded,state,verify,acceptance)

    @classmethod
    def load(cls,directory, *, verify=sources.verify_stage,acceptance=False):
        directory=backup.absolute(directory)
        reference=registry.private_json(directory/packages.INSTALLATION)
        if (not isinstance(reference,dict) or reference.get('version')!=1 or reference.get('phase')!='installation_operation'
                or reference.get('project_directory')!=str(directory) or registry.sealed(reference)!=reference):
            _fail('install_reference_invalid','installation directory reference is invalid')
        package=packages.Package.load(reference['package_directory'],verify=verify,acceptance=acceptance)
        state=_read(package.directory/'install-state.json')
        if (backup.digest(package.directory/'install-package.json')[1]!=reference['package_sha256']
                or any(state.get(key)!=reference.get(key) for key in ('id','project','project_directory','package_sha256'))
                or package.receipt['id']!=reference['id'] or package.receipt['project']!=reference['project']):
            _fail('install_reference_changed','installation reference no longer binds its package and journal')
        return cls(package,state,verify,acceptance)

    def _save(self,phase,**values):
        if _read(self.path)!=self.state:_fail('install_state_changed','reload the installation journal before another mutation')
        updated=registry.sealed({**self.state,**values,'phase':phase,'revision':self.state['revision']+1})
        temporary=self.path.parent/('install-state-'+uuid.uuid4().hex+'.tmp')
        backup.private_write(temporary,backup.json_bytes(updated));os.replace(temporary,self.path);backup.fsync_directory(self.path.parent)
        self.state=updated

    def _check(self, *, allow_project=False,allow_current=False):
        receipt=self.package.receipt
        if _read(self.path)!=self.state or registry.root_identity(self.directory)!=self.state['directory_identity']:
            _fail('install_state_changed','installation state or its host directory changed')
        reference=registry.private_json(self.directory/packages.INSTALLATION)
        if (reference.get('version')!=1 or reference.get('phase')!='installation_operation' or registry.sealed(reference)!=reference
                or any(reference.get(key)!=self.state[key] for key in ('id','project','project_directory','package_sha256'))
                or reference.get('package_directory')!=str(self.package.directory)):
            _fail('install_reference_changed','installation reference changed before resource creation')
        loaded=packages.Package.load(self.package.directory,verify=self.verify,acceptance=self.acceptance)
        if loaded.receipt!=receipt or backup.digest(self.package.directory/'install-package.json')[1]!=self.state['package_sha256']:
            _fail('install_package_changed','installation package changed before resource creation')
        if _inputs(receipt)!=receipt['private_inputs']:
            _fail('install_inputs_changed','private installation inputs changed and are not overwritten')
        for item in receipt['canonical_files']:
            target=backup.absolute(item['target'],exists=False)
            if target.exists():
                if backup.digest(target)[1]!=item['sha256']:_fail('install_existing_compose_differs','existing canonical inputs are preserved')
            elif self.state['phase']=='resources_prepared' or self.state['phase'] in START_PHASES:
                _fail('install_canonical_missing','a completed canonical input disappeared and is not recreated')
        protected=[Path(receipt['fresh_resources']['control']['path']),self.package.directory,Path(receipt['stage_directory'])]
        for container in self.package.docker.containers():
            labels=container.get('Config',{}).get('Labels') or {}
            own=labels.get(mapping.LABEL+'project')==self.state['project']
            if not own and labels.get(mapping.LABEL+'project.working_dir')==str(self.directory):
                _fail('install_directory_occupied','another Compose project now owns this installation directory')
            if own:
                if allow_project:continue
                _fail('install_unexpected_container','a project container exists before installation startup authorization')
            for mount in container.get('Mounts',[]):
                if mount.get('Type') not in {'bind','volume'}:continue
                source=backup.absolute(mount['Source'],exists=False)
                if (mount.get('Name') in receipt['fresh_resources']['volumes']
                        or any(source==root or source in root.parents or root in source.parents for root in protected)):
                    _fail('install_foreign_mount','a container now mounts installation or private host resources')
        for name in (registry.CURRENT,registry.OPERATION):
            if name==registry.CURRENT and allow_current:continue
            if (self.directory/name).exists() or (self.directory/name).is_symlink():
                _fail('install_existing_deployment','another host deployment/upgrade is preserved')

    def _canonical(self):
        from scripts.operator_recovery import rename_noreplace
        for item in self.package.receipt['canonical_files']:
            target=backup.absolute(item['target'],exists=False)
            if target.parent!=self.directory:_fail('install_canonical_invalid','canonical installation files must remain in the project directory')
            if target.exists() or target.is_symlink():
                if backup.digest(backup.absolute(target))[1]!=item['sha256']:
                    _fail('install_existing_compose_differs','an existing canonical Compose is retained and cannot be overwritten')
                continue
            source=backup.absolute(item['source'])
            if backup.digest(source)[1]!=item['sha256']:_fail('source_stage_changed','canonical source changed before publication')
            attempts=self.state['canonical_attempts']
            if len(attempts)>=16:_fail('install_canonical_incomplete','repeated canonical-file attempts remain retained for inspection')
            temporary=self.directory/('.frontiercloud-install-'+self.state['id']+'-'+uuid.uuid4().hex+'.tmp')
            self._save('provisioning',canonical_attempts=attempts+[{'temporary':str(temporary),'target':str(target),'sha256':item['sha256']}])
            backup.private_write(temporary,source.read_bytes())
            if backup.digest(temporary)[1]!=item['sha256']:_fail('install_canonical_changed','prepared canonical bytes changed')
            rename_noreplace(temporary,target)
            backup.fsync_directory(self.directory)

    def _control(self):
        original=self.package.receipt['fresh_resources']['control'];root=backup.absolute(original['path'],exists=False)
        recorded=self.state['control_identity']
        if not original['exists'] and registry.root_identity(root.parent)!=original['parent']:
            _fail('install_control_changed','the declared control parent changed')
        if recorded:
            if registry.root_identity(root)!=recorded:_fail('install_control_changed','the owned empty control root changed')
        elif root.exists():
            identity=registry.root_identity(root)
            if original['exists'] and identity!=original['identity']:
                _fail('install_control_changed','the original empty control root changed')
            if identity['uid']!=os.geteuid():_fail('install_control_changed','an unrecognized control root is preserved')
        else:
            if original['exists'] or registry.root_identity(root.parent)!=original['parent']:
                _fail('install_control_changed','the declared control parent changed')
            root.mkdir(mode=0o700);backup.fsync_directory(root.parent)
        if next(root.iterdir(),None) is not None:_fail('install_data_occupied','new control bytes are preserved; no startup is authorized')
        if not recorded:self._save('provisioning',control_identity=registry.root_identity(root))

    def _volume(self,name,key):
        docker=self.package.docker
        value=docker.volume(name)
        if value is None:
            if name in self.state['volume_identities']:_fail('install_volume_missing','a recorded installation volume disappeared')
            # Phase provisioning and the immutable package reserve this exact
            # name/token before Docker's atomic create. A lost reply reconciles.
            docker.run(['docker','volume','create','--label',mapping.LABEL+'project='+self.state['project'],
                '--label',mapping.LABEL+'volume='+key,'--label',LABEL+'='+self.state['id'],name])
            value=docker.volume(name)
        if not isinstance(value,dict):_fail('install_volume_missing','the intended volume could not be confirmed after create')
        labels=value.get('Labels') or {}
        if (value.get('Name')!=name or value.get('Driver')!='local' or value.get('Options')
                or labels.get(mapping.LABEL+'project')!=self.state['project'] or labels.get(mapping.LABEL+'volume')!=key
                or labels.get(LABEL)!=self.state['id']):
            _fail('install_volume_foreign','an unrecognized volume is retained and cannot be used')
        root=backup.absolute(value['Mountpoint'])
        if root!=docker.local_root()/'volumes'/name/'_data':_fail('install_volume_foreign','volume mountpoint is outside its local Docker namespace')
        identity=registry.root_identity(root)
        recorded=self.state['volume_identities'].get(name)
        if recorded and identity!=recorded:_fail('install_volume_changed','the owned empty volume root changed')
        if next(root.iterdir(),None) is not None:_fail('install_volume_occupied','new volume bytes are preserved; no startup is authorized')
        if not recorded:self._save('provisioning',volume_identities={**self.state['volume_identities'],name:identity})

    def resources(self):
        with backup.operation_lock(self.state['project']):
            if self.state['phase'] not in RESOURCE_PHASES:
                _fail('install_phase_invalid','resource-only preparation cannot run after possible business writes')
            self._check()
            if self.state['phase']=='prepared':self._save('provisioning')
            self._canonical();self._control()
            for name,key in self.package.receipt['fresh_resources']['volumes'].items():self._volume(name,key)
            self._check()
            result={'phase':'resources_prepared','project':self.state['project'],'installation_complete':False,
                'services_started':False,'business_may_have_written':False,'owned_control':self.state['control_identity'],
                'owned_volumes':self.state['volume_identities']}
            if self.state['phase']!='resources_prepared':self._save('resources_prepared',resource_result=result)
            elif self.state.get('resource_result')!=result:_fail('install_state_changed','completed resource preparation changed')
            return result
