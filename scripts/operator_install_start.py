"""First Compose startup and identity verification; no old-state restoration."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import re
from types import SimpleNamespace
import uuid

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_install_probe as probes
from scripts import operator_install_resources as resources
from scripts import operator_prepare as preparation
from scripts import operator_registry as registry
from scripts.operator_upgrade import _join_helper


def _same_inode(actual,expected):
    return all(actual.get(name)==expected.get(name) for name in ('path','device','inode'))


class Installation(resources.Installation):
    def _join_helpers(self):
        for path in self.package.directory.glob('install-probe-*/helper-owner.json'):
            owner=registry.private_json(path)
            if (owner.get('version')!=1 or owner.get('operation')!=self.state['id'] or owner.get('project')!=self.state['project']
                    or not isinstance(owner.get('token'),str) or not re.fullmatch('[a-f0-9]{32}',owner['token'])
                    or owner.get('name')!='fc-op-helper-'+owner['token']
                    or owner.get('image')!=self.package.receipt['images']['web']['id']):
                mapping.fail('install_probe_owner_changed','an unrecognized helper is retained')
            _join_helper(self.package.docker,owner)

    def _roots(self):
        control=registry.root_identity(self.state['control_identity']['path'])
        if not _same_inode(control,self.state['control_identity']):
            mapping.fail('install_control_changed','the created control directory was replaced')
        for name,key in self.package.receipt['fresh_resources']['volumes'].items():
            volume=self.package.docker.volume(name)
            labels=volume.get('Labels',{}) if volume else {}
            if (not volume or volume.get('Driver')!='local' or volume.get('Options')
                    or labels.get(resources.LABEL)!=self.state['id'] or labels.get(mapping.LABEL+'project')!=self.state['project']
                    or labels.get(mapping.LABEL+'volume')!=key):
                mapping.fail('install_volume_changed','a created installation volume changed or disappeared')
            root=backup.absolute(volume['Mountpoint'])
            if root!=self.package.docker.local_root()/'volumes'/name/'_data':
                mapping.fail('install_volume_changed','an installation volume mountpoint changed')
            if not _same_inode(registry.root_identity(root),self.state['volume_identities'][name]):
                mapping.fail('install_volume_changed','a created volume directory was replaced')

    def _containers(self):
        docker=self.package.docker
        hashes={}
        for line in docker.run(docker.base+['config','--hash','*']).splitlines():
            parts=line.split()
            if len(parts)!=2 or not re.fullmatch('[a-f0-9]{64}',parts[1]) or parts[0] in hashes:
                mapping.fail('install_config_hash_invalid','Compose returned an invalid service hash inventory')
            hashes[parts[0]]=parts[1]
        if set(hashes)!=set(self.package.config['services']):mapping.fail('install_config_hash_invalid','Compose service hashes are incomplete')
        seen=set()
        for container in docker.containers():
            item=container.get('Config',{});labels=item.get('Labels') or {}
            if labels.get(mapping.LABEL+'project')!=self.state['project']:continue
            name=labels.get(mapping.LABEL+'service');definition=self.package.config['services'].get(name)
            if (definition is None or name in seen or labels.get(mapping.LABEL+'oneoff','').lower()!='false'
                    or labels.get(mapping.LABEL+'config-hash')!=hashes[name]
                    or labels.get(mapping.LABEL+'project.working_dir')!=str(self.directory)
                    or labels.get(mapping.LABEL+'project.config_files')!=str(self.package.directory/'compose.json')
                    or labels.get(mapping.LABEL+'project.environment_file','')
                    or container.get('Image')!=self.package.receipt['images'][name]['id'] or item.get('Image')!=definition['image']):
                mapping.fail('install_container_changed','an unknown or changed partial installation container is retained')
            seen.add(name)
            if definition.get('container_name') and container.get('Name')!='/'+definition['container_name']:
                mapping.fail('install_container_changed','a partial installation container name changed')
            actual={}
            temporary={entry.split(':',1)[0] for entry in definition.get('tmpfs',[])}
            for mount in container.get('Mounts',[]):
                target=mount.get('Destination')
                if mount.get('Type')=='tmpfs' and target in temporary:continue
                if target in actual:mapping.fail('install_container_changed','duplicate partial container mounts')
                actual[target]={'type':mount.get('Type'),'source':mount.get('Name') if mount.get('Type')=='volume' else mount.get('Source'),
                    'read_only':not mount.get('RW',False)}
            if actual!=mapping.mounts(self.package.config,name):mapping.fail('install_container_changed','partial container mounts changed')
            if {key:sorted(value or [],key=backup.json_bytes) for key,value in (container.get('HostConfig',{}).get('PortBindings') or {}).items()}!=mapping._ports(definition):
                mapping.fail('install_container_changed','partial container ports changed')
            image=docker.run(['docker','image','inspect',definition['image']],json_output=True)[0]
            image_env=dict(row.split('=',1) for row in image.get('Config',{}).get('Env',[]) if '=' in row)
            actual_env=dict(row.split('=',1) for row in item.get('Env',[]) if '=' in row)
            if actual_env!=image_env|{key:str(value) for key,value in definition.get('environment',{}).items()}:
                mapping.fail('install_container_changed','partial container environment changed')

    def _prepared(self):
        receipt=self.package.receipt
        files=[Path(row['target']) for row in receipt['canonical_files']]
        inputs=[Path(row['path']) for row in receipt['private_inputs']]
        files+= [path for path in inputs if path!=self.directory/'.env']
        value={'version':1,'phase':'installed_package','id':receipt['id'],'project':receipt['project'],
            'project_directory':str(self.directory),'compose_sha256':receipt['compose_sha256'],
            'configuration_sha256':receipt['configuration_sha256'],'images':receipt['images'],
            'stage_directory':receipt['stage_directory'],'source_directory':str(Path(receipt['stage_directory'])/'source'),
            'source_stage_sha256':receipt['source_stage_sha256'],'provenance':receipt['provenance'],
            'acceptance_only':receipt['acceptance_only'],'source_files':[str(path) for path in files],
            'source_env_files':[],'source_inputs':mapping._inputs(files+([self.directory/'.env'] if (self.directory/'.env').exists() else [])),
            'install_package_sha256':self.state['package_sha256']}
        path=self.package.directory/'prepared.json'
        if path.exists():
            if registry.private_json(path)!=value:mapping.fail('install_prepared_changed','installed package mapping changed')
        else:preparation._store(path,value)
        return SimpleNamespace(directory=self.package.directory,receipt=value)

    def _registration(self):
        return {'version':1,'id':self.state['id'],'phase':'ready','project':self.state['project'],
            'project_directory':str(self.directory),'prepared_directory':str(self.package.directory),
            'prepared_sha256':self.state['prepared_sha256'],'node_id':self.state['identity']['node_id'],
            'generation':self.state['identity']['generation'],'roots':self.state['initialized_roots'],'origin':'source_install'}

    def _published(self):
        path=self.directory/registry.CURRENT
        if not (path.exists() or path.is_symlink()):
            if self.state['phase']=='ready':mapping.fail('install_receipt_missing','the completed installation receipt disappeared')
            return False
        if (self.state['phase'] not in {'verifying','ready'} or not self.state.get('install_result')
                or registry.private_json(path)!=registry.sealed(self._registration())):
            mapping.fail('install_receipt_changed','a different deployment receipt is preserved')
        registry.read(self.directory,self.state['project'])
        self._roots();self._containers()
        digest=backup.digest(path)[1]
        if self.state['phase']=='ready':
            if digest!=self.state.get('registry_sha256'):mapping.fail('install_receipt_changed','the completed deployment receipt changed')
        else:self._save('ready',registry_sha256=digest)
        return True

    def start(self, *, execute=preparation._command):
        if self.state['phase'] in resources.RESOURCE_PHASES:self.resources()
        with backup.operation_lock(self.state['project']):
            self._join_helpers()
            self._check(allow_project=True,allow_current=self.state['phase'] in {'verifying','ready'})
            if self._published():return copy.deepcopy(self.state['install_result'])
            self._roots();self._containers()
            if self.state['phase']=='resources_prepared':
                self._control()
                for name,key in self.package.receipt['fresh_resources']['volumes'].items():self._volume(name,key)
                self._save('starting',business_may_have_written=True)
            prepared=self._prepared()
            if self.state['phase']=='starting':
                attempt=self.package.directory/('install-start-'+uuid.uuid4().hex);attempt.mkdir(mode=0o700)
                execute(self.package.docker,self.package.docker.base+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','180'],
                    attempt,'install-start',timeout=210)
                self._roots();self._containers()
                self._save('started')
            if self.state['phase']=='started':
                identity=probes.run(self.package,'identity-original',execute=execute)
                if (identity.get('version')!=1 or identity.get('phase')!='original_identity_verified'
                        or not re.fullmatch('[a-f0-9]{32}',identity.get('node_id','')) or type(identity.get('generation')) is not int
                        or not identity.get('keys') or not identity.get('public_key_sha256')):
                    mapping.fail('install_identity_invalid','fresh installed node identity was not verified')
                path=self.package.directory/('install-identity-'+uuid.uuid4().hex+'.json')
                backup.private_write(path,backup.json_bytes(identity));os.chown(path,10001,10001,follow_symlinks=False)
                backup.fsync_directory(path.parent)
                self._save('verifying',identity=identity,identity_file=str(path),identity_sha256=backup.digest(path)[1])
            baseline=backup.absolute(self.state['identity_file'])
            if backup.digest(baseline)[1]!=self.state['identity_sha256']:
                mapping.fail('install_identity_changed','fresh identity baseline changed before endpoint verification')
            proof=probes.run(self.package,'probe-open',baseline=baseline,baseline_sha=self.state['identity_sha256'],
                node_id=self.state['identity']['node_id'],execute=execute)
            tls=str(self.package.config['services']['web'].get('environment',{}).get('TLS_ENABLED',False)).lower() in {'true','1','yes','on','y'}
            if (proof.get('phase')!='endpoint_open' or proof.get('node_id')!=self.state['identity']['node_id']
                    or proof.get('generation')!=self.state['identity']['generation'] or proof.get('baseline_sha256')!=self.state['identity_sha256']
                    or proof.get('readiness_status')!=200 or proof.get('business_opened') is not True or proof.get('tls_enabled') is not tls
                    or tls and (proof.get('certificate_verified') is not True or proof.get('signed_identity_verified') is not True)):
                mapping.fail('install_endpoint_invalid','fresh installed endpoint differs from its node identity')
            current=mapping.Deployment.discover(self.directory,prepared=prepared)
            self._roots();self._containers()
            roots={key:registry.root_identity(value.root) for key,value in current.plan.resources.items()}
            result={**proof,'phase':'ready','project':self.state['project'],'revision':self.package.receipt['provenance']['revision'],
                'installation_complete':True,'acceptance_only':self.package.receipt['acceptance_only'],'services':current.observed}
            self._save('verifying',endpoint_proof=proof,initialized_roots=roots,install_result=result,
                prepared_sha256=backup.digest(self.package.directory/'prepared.json')[1])
            registered=registry.publish(self.directory,self._registration(),previous_sha256=None)
            self._save('ready',registry_sha256=registered)
            return copy.deepcopy(result)


def validate_receipt(directory,value,receipt):
    reference=registry.private_json(directory/resources.packages.INSTALLATION)
    folder=backup.absolute(value['prepared_directory'])
    state=resources._read(folder/'install-state.json')
    package=registry.private_json(folder/'install-package.json')
    identity=state.get('identity',{});proof=state.get('endpoint_proof',{})
    config=backup.compose_values(backup.bounded_json(folder/'compose.json'))
    tls=str(config['services']['web'].get('environment',{}).get('TLS_ENABLED',False)).lower() in {'true','1','yes','on','y'}
    if (registry.sealed(reference)!=reference or reference.get('version')!=1 or reference.get('phase')!='installation_operation'
            or state['phase'] not in {'verifying','ready'} or value.get('phase')!='ready' or reference.get('package_directory')!=str(folder)
            or reference.get('project_directory')!=str(directory) or reference.get('id')!=receipt.get('id')
            or any(reference.get(key)!=state.get(key) or reference.get(key)!=package.get(key)
                for key in ('id','project','project_directory'))
            or backup.digest(folder/'install-package.json')[1]!=reference.get('package_sha256')
            or state.get('package_sha256')!=reference['package_sha256'] or receipt.get('install_package_sha256')!=reference['package_sha256']
            or package.get('id')!=receipt['id'] or receipt.get('configuration_sha256')!=package.get('configuration_sha256')
            or any(receipt.get(key)!=package.get(key) for key in ('compose_sha256','images','source_stage_sha256','stage_directory','provenance','acceptance_only'))
            or value.get('roots')!=state.get('initialized_roots') or value.get('prepared_sha256')!=state.get('prepared_sha256')
            or value.get('node_id')!=identity.get('node_id') or value.get('generation')!=identity.get('generation')
            or proof.get('phase')!='endpoint_open' or proof.get('business_opened') is not True
            or proof.get('node_id')!=value.get('node_id') or proof.get('generation')!=value.get('generation')
            or proof.get('baseline_sha256')!=state.get('identity_sha256') or proof.get('readiness_status')!=200
            or proof.get('tls_enabled') is not tls
            or tls and (proof.get('certificate_verified') is not True or proof.get('signed_identity_verified') is not True)
            or not state.get('install_result',{}).get('installation_complete')):
        mapping.fail('install_receipt_changed','fresh installed receipt no longer binds its completed node and source package')
    baseline=backup.absolute(state['identity_file'])
    if (baseline.parent!=folder or backup.digest(baseline)[1]!=state['identity_sha256']
            or backup.bounded_json(baseline)!=identity):
        mapping.fail('install_identity_changed','the retained fresh node identity baseline changed')
