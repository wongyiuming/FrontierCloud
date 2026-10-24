"""Prepare a fresh Python Compose installation without starting or initializing it."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
import re
from types import SimpleNamespace
import uuid

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_prepare as preparation
from scripts import operator_registry as registry
from scripts import operator_source as sources

INSTALLATION='.frontiercloud-operator-installation.json'


def _fail(code,message):mapping.fail(code,message)


def _inputs(directory,overrides):
    implicit=directory/'.env'
    return mapping._inputs(overrides+([implicit] if implicit.exists() or implicit.is_symlink() else []))


def _material(directory,source,files):
    """Plan canonical files; never overwrite a local Compose or private input."""
    result=[]
    for path in files:
        if path.parent!=source:
            continue
        target=directory/path.name
        expected=backup.digest(path)[1]
        present=target.exists() or target.is_symlink()
        if present and backup.digest(backup.absolute(target))[1]!=expected:
            _fail('install_existing_compose_differs','an existing canonical Compose differs from the selected source; preserve it and use upgrade or a fresh directory')
        result.append({'source':str(path),'target':str(target),'sha256':expected,'already_present':present})
    return result


def _render(directory,source,storage,overrides):
    base=source/('docker-compose.storage.yaml' if storage else 'docker-compose.yaml')
    files=[base,*overrides]
    probe=mapping.ComposeDocker(directory,[],'install-discovery',[])
    def render(selected):
        command=['docker','compose','--project-directory',str(directory)]
        for path in selected:command+=['-f',str(path)]
        return backup.compose_values(probe.run(command+['config','--format','json'],json_output=True))
    config=render(files)
    if not storage and config['services']['web'].get('environment',{}).get('DB_TYPE','sqlite')=='mysql':
        files=[base,source/'docker-compose.mysql.yaml',*overrides]
        config=render(files)
    return files,config


def observe(directory,config,docker,private_paths=()):
    """No data/volume/container creation; occupied material always refuses."""
    directory=backup.absolute(directory)
    docker.local_root()
    name=config.get('name','')
    if not isinstance(name,str) or not backup.NAME.fullmatch(name):
        _fail('install_project_invalid','fresh installation requires a valid Compose project')
    role,engine=mapping._role(config)
    for filename in (registry.CURRENT,registry.OPERATION,INSTALLATION):
        if (directory/filename).exists() or (directory/filename).is_symlink():
            _fail('install_existing_deployment','host deployment/operation material already exists; use its recorded operation')
    web=config['services']['web']
    try:cpu=float(web.get('cpus',0))
    except (TypeError,ValueError):cpu=0
    if not 0<cpu<=1:
        _fail('install_resource_budget_invalid','Web must have an explicit limit of at most one CPU')
    if web.get('environment',{}).get('WEB_CONCURRENCY') not in {None,'1',1}:
        _fail('install_resource_budget_invalid','multiple Web workers are unsupported')
    roots=set();volumes={};container_names=set()
    for service,definition in config['services'].items():
        if definition.get('privileged') or definition.get('devices') or definition.get('pid')=='host':
            _fail('install_service_unsafe','privileged devices and host PID access are unsupported')
        if definition.get('container_name'):container_names.add('/'+definition['container_name'])
        for target,row in mapping.mounts(config,service,existing=False).items():
            if row['type']=='bind':
                if target!='/app/data':_fail('install_mount_unsupported','fresh installation only admits the declared control bind')
                root=Path(row['source'])
                if (len(root.parts)<3 or root in {Path('/var/lib'),Path('/root'),Path('/home'),Path('/etc'),Path('/usr'),Path('/var/lib/docker')}
                        or root==directory or root in directory.parents):
                    _fail('install_data_path_unsafe','fresh data cannot overlap broad system or operator directories')
                if any(root==path or root in path.parents or path in root.parents for path in private_paths):
                    _fail('install_data_path_unsafe','fresh data and retained source/host material cannot overlap')
                roots.add(root)
            else:
                logical=next(mount['source'] for mount in definition.get('volumes',[]) if mount['target']==target)
                if row['source'] in volumes and volumes[row['source']]!=logical:
                    _fail('install_volume_alias','distinct logical volumes cannot share an installation namespace')
                volumes[row['source']]=logical
    if len(roots)!=1:
        _fail('install_data_path_unsafe','fresh installation needs exactly one independent control root')
    root=next(iter(roots))
    if root.exists():
        info=root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or next(root.iterdir(),None) is not None:
            _fail('install_data_occupied','existing nonempty or foreign data is preserved; use upgrade')
        control={'path':str(root),'exists':True,'identity':registry.root_identity(root)}
    else:
        parent=backup.absolute(root.parent)
        control={'path':str(root),'exists':False,'parent':registry.root_identity(parent)}
    if docker.run(['docker','volume','ls','-q','--filter','label='+mapping.LABEL+'project='+name]):
        _fail('install_volume_occupied','existing project volumes are preserved; use upgrade or restore')
    for volume in volumes:
        if docker.volume(volume) is not None:
            _fail('install_volume_occupied','an intended installation volume already exists and is preserved')
    for container in docker.containers():
        labels=container.get('Config',{}).get('Labels') or {}
        if labels.get(mapping.LABEL+'project.working_dir')==str(directory):
            _fail('install_directory_occupied','this directory already owns a Compose deployment; use upgrade or an independent directory')
        if labels.get(mapping.LABEL+'project')==name or container.get('Name') in container_names:
            _fail('install_container_occupied','an existing project or named container is preserved')
        for mount in container.get('Mounts',[]):
            if mount.get('Type') not in {'bind','volume'}:continue
            path=backup.absolute(mount['Source'],exists=False)
            if path==root or path in root.parents or root in path.parents:
                _fail('install_foreign_mount','a container already mounts the intended control namespace')
    empty=SimpleNamespace(docker=docker,observed={},plan=SimpleNamespace(name=name,config={'services':{}}))
    preparation._port_conflicts(empty,config)
    return {'project':name,'role':role,'database':engine,'control':control,'volumes':volumes}


@dataclass
class Package:
    directory:Path
    docker:mapping.ComposeDocker
    config:dict
    receipt:dict

    @classmethod
    def load(cls,directory, *, verify=sources.verify_stage,acceptance=False):
        directory=backup.absolute(directory)
        info=directory.stat()
        if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700:
            _fail('install_package_unsafe','installation package requires its private owned directory')
        receipt=registry.private_json(directory/'install-package.json')
        if (not isinstance(receipt,dict) or receipt.get('version')!=1 or receipt.get('phase')!='install_package_prepared'
                or not isinstance(receipt.get('provenance'),dict) or not re.fullmatch('[0-9a-f]{32}',receipt.get('id',''))
                or receipt.get('acceptance_only') is not (receipt.get('provenance',{}).get('public_provenance_verified') is not True)):
            _fail('install_package_invalid','installation package is incomplete')
        if receipt['acceptance_only'] and not acceptance:
            _fail('source_not_published','native installation fixtures cannot authorize public installation')
        stage=backup.absolute(receipt['stage_directory'])
        if stage.stat().st_uid!=os.geteuid() or stat.S_IMODE(stage.stat().st_mode)!=0o700:
            _fail('source_stage_unsafe','installation source requires its private host-owned directory')
        checked=verify(stage)
        if checked['stage_sha256']!=receipt['source_stage_sha256'] or checked['provenance']!=receipt['provenance']:
            _fail('source_stage_changed','installation source changed')
        if backup.digest(directory/'compose.json')[1]!=receipt['compose_sha256']:
            _fail('install_package_changed','pinned installation Compose changed')
        docker=mapping.ComposeDocker(Path(receipt['project_directory']),[directory/'compose.json'],receipt['project'],[])
        config=docker.config()
        if mapping.checksum(config)!=receipt['configuration_sha256'] or set(config['services'])!=set(receipt['images']):
            _fail('install_package_changed','installation configuration or image inventory changed')
        for name,definition in config['services'].items():
            expected=receipt['images'][name]
            if definition.get('image')!=expected['id'] or definition.get('build') or definition.get('pull_policy')!='never':
                _fail('install_package_changed','installation services must use their exact pinned images')
            actual=docker.run(['docker','image','inspect',expected['id']],json_output=True)[0]
            if {'id':actual['Id'],'os':actual['Os'],'architecture':actual['Architecture']}!=expected:
                _fail('install_image_changed','an installation image changed or is unavailable')
            if name in preparation.APP_SERVICES|{'nginx'}:
                labels=actual.get('Config',{}).get('Labels') or {}
                if labels.get('io.frontiercloud.operator.id')!=receipt['id'] or labels.get('io.frontiercloud.operator.source')!=receipt['source_stage_sha256']:
                    _fail('install_image_changed','an installation image lost its source binding')
        return cls(directory,docker,config,receipt)


def prepare(directory,stage,output, *, storage=False,overrides=(),verify=sources.verify_stage,acceptance=False,execute=preparation._command):
    """Only private package files and images change; no deployment startup."""
    directory=backup.absolute(directory);stage=backup.absolute(stage);output=backup.absolute(output,exists=False)
    checked=verify(stage)
    if stage.stat().st_uid!=os.geteuid() or stat.S_IMODE(stage.stat().st_mode)!=0o700:
        _fail('source_stage_unsafe','fresh installation needs a private host-owned source stage')
    if checked['provenance'].get('public_provenance_verified') is not True and not acceptance:
        _fail('source_not_published','fresh installation requires verified published source')
    source=stage/'source'
    overrides=[backup.absolute(path) for path in overrides]
    if overrides and not acceptance:
        _fail('install_override_unreviewed','fresh public installation uses canonical Compose and private environment inputs')
    files,config=_render(directory,source,storage,overrides)
    config['services']['web'].setdefault('mem_limit',512*1024*1024)
    name=config['name']
    docker=mapping.ComposeDocker(directory,files,name,[])
    canonical=_material(directory,source,files)
    inputs=_inputs(directory,overrides)
    observed=observe(directory,config,docker,(stage,output))
    if (observed['role']=='storage') is not storage:
        _fail('install_role_mismatch','the selected canonical base does not match the requested installation role')
    output=preparation._private_directory(output)
    identifier=uuid.uuid4().hex
    builders=[]
    for service,definition in config['services'].items():
        definition.pop('profiles',None)
        if service in preparation.APP_SERVICES|{'nginx'}:
            flavor='nginx' if service=='nginx' else 'web'
            definition['image']='frontiercloud-python-install-'+identifier+'-'+flavor
            definition.pop('build',None)
            if service in {'web','nginx'}:
                definition['build']={'context':str(source),'dockerfile':'nginx/Dockerfile' if flavor=='nginx' else 'Dockerfile',
                    'labels':{'io.frontiercloud.operator.id':identifier,'io.frontiercloud.operator.source':checked['stage_sha256'],
                        'org.opencontainers.image.revision':checked['provenance']['revision']}}
                builders.append(service)
    preparation._store(output/'build-compose.json',backup.compose_values(config,encode=True))
    build=mapping.ComposeDocker(directory,[output/'build-compose.json'],name,[])
    if build.config()!=config:_fail('compose_roundtrip_changed','fresh installation literal inputs changed while rendered')
    preparation._store(output/'install-intent.json',{'version':1,'id':identifier,'phase':'install_preparing','project':name,
        'project_directory':str(directory),'source_stage_sha256':checked['stage_sha256'],'fresh_resources':observed,
        'canonical_files':canonical,'private_inputs':inputs,'acceptance_only':not checked['provenance'].get('public_provenance_verified',False)})
    execute(build,build.base+['build',*builders],output,'install-image-build')
    platform=docker.run(['docker','version','--format','{{json .Server}}'],json_output=True)
    images={}
    for service,definition in config['services'].items():
        if service not in preparation.APP_SERVICES|{'nginx'}:
            execute(build,['docker','pull',definition['image']],output,'install-image-pull-'+service)
        image=build.run(['docker','image','inspect',definition['image']],json_output=True)[0]
        if image['Os']!='linux' or image['Architecture']!=platform['Arch']:
            _fail('install_image_platform_changed','installation images must match the local Linux daemon architecture')
        if service in preparation.APP_SERVICES|{'nginx'}:
            labels=image.get('Config',{}).get('Labels') or {}
            if labels.get('io.frontiercloud.operator.id')!=identifier or labels.get('io.frontiercloud.operator.source')!=checked['stage_sha256']:
                _fail('install_image_source_changed','the built image does not bind this source and installation')
        images[service]={'id':image['Id'],'os':image['Os'],'architecture':image['Architecture']}
        definition['image']=image['Id'];definition.pop('build',None);definition['pull_policy']='never'
    if (verify(stage)!=checked or _material(directory,source,files)!=canonical
            or _inputs(directory,overrides)!=inputs
            or observe(directory,config,docker,(stage,output))!=observed):
        _fail('install_inputs_changed','source, private inputs or fresh resources changed during image preparation')
    preparation._store(output/'compose.json',backup.compose_values(config,encode=True))
    target=mapping.ComposeDocker(directory,[output/'compose.json'],name,[])
    if target.config()!=config:_fail('compose_roundtrip_changed','pinned installation literal inputs changed while rendered')
    receipt={'version':1,'id':identifier,'phase':'install_package_prepared','project':name,'project_directory':str(directory),
        'stage_directory':str(stage),'source_stage_sha256':checked['stage_sha256'],'provenance':checked['provenance'],
        'acceptance_only':checked['provenance'].get('public_provenance_verified') is not True,'canonical_files':canonical,
        'private_inputs':inputs,'fresh_resources':observed,'images':images,'configuration_sha256':mapping.checksum(config),
        'compose_sha256':backup.digest(output/'compose.json')[1],'deployment_changed':False}
    preparation._store(output/'install-package.json',receipt)
    return Package(output,target,config,receipt)
