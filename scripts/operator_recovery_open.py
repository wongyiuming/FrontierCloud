"""Open only the verified old-state replacement, with a durable one-way boundary."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import uuid

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_prepare as preparation
from scripts import operator_registry as registry
from scripts import operator_upgrade as upgrade


def fail(code,message):
    mapping.fail(code,message)


def validate_receipt(directory, current, receipt):
    """Read-only binding of a restored deployment to its retained failed upgrade."""
    parent = backup.absolute(receipt['original_prepared_directory'])
    original = registry.private_json(parent/'prepared.json')
    state = upgrade._read(parent/'upgrade-state.json')
    recovery = state.get('recovery',{})
    attempt = recovery.get('attempt') or {}
    if recovery.get('mode') == 'original_abort':
        from scripts.operator_abort import EARLY_PHASES
        if (recovery.get('from_phase') not in EARLY_PHASES or receipt.get('recovery_mode') != 'original_abort'
                or recovery.get('restored_roots') != state.get('roots')
                or attempt.get('control') != state['roots']['control']['path']):
            fail('recovery_receipt_changed','the aborted upgrade cannot claim a restored or different resource namespace')
    elif receipt.get('recovery_mode') is not None:
        fail('recovery_receipt_changed','the recovery receipt changed its selected recovery mode')
    if (state['phase'] not in {'restored_opening','restored_opened'} or not state.get('business_may_have_written')
            or state.get('id') != current['id'] or state.get('project') != current['project']
            or state.get('project_directory') != str(directory)
            or backup.digest(parent/'prepared.json')[1] != state.get('prepared_sha256')
            or receipt.get('original_prepared_sha256') != state.get('prepared_sha256')
            or original.get('id') != state['id'] or receipt.get('recovery_id') != recovery.get('id')
            or attempt.get('directory') != current.get('prepared_directory')
            or attempt.get('compose_sha256') != receipt.get('compose_sha256')
            or attempt.get('configuration_sha256') != receipt.get('configuration_sha256')
            or recovery.get('prepared_sha256') != current.get('prepared_sha256')
            or current.get('roots') != recovery.get('restored_roots')
            or current.get('generation') != state['preflight']['generation']
            or current.get('node_id') != state['preflight']['node_id']):
        fail('recovery_receipt_changed','the restored deployment no longer binds its verified original state')
    for name in ('source_files','source_env_files','source_inputs'):
        if receipt.get(name) != original.get(name):
            fail('recovery_receipt_changed','the restored deployment changed its original source/private input mapping')
    expected = original['old_deployment']['services']
    if set(receipt.get('images',{})) != set(expected):
        fail('recovery_receipt_changed','the restored service image inventory changed')
    platform = original['images']['web']
    for name,image in receipt['images'].items():
        if image != {'id':expected[name]['image_id'],'os':platform['os'],'architecture':platform['architecture']}:
            fail('recovery_receipt_changed','a restored service no longer uses its exact original image/platform')


class Opening:
    identity_command = 'preflight'
    def __init__(self, transaction):
        self.transaction = transaction
        self.operation = transaction.operation

    @property
    def state(self):
        return self.transaction.state

    def attempt(self, name):
        directory = self.operation.prepared.directory/(name+'-'+uuid.uuid4().hex)
        directory.mkdir(mode=0o700)
        backup.fsync_directory(directory.parent)
        return directory

    def failed_resources_cold(self):
        """The recovered project may run, but no process may use failed roots."""
        plan = self.operation.deployment.plan
        roots = [resource.root for resource in plan.resources.values()]
        volumes = {resource.source for resource in plan.resources.values() if resource.kind == 'volume'}
        self.operation._owned_fence()
        for container in plan.docker.containers():
            if not container.get('State',{}).get('Running'):
                continue
            for mount in container.get('Mounts',[]):
                path = Path(mount.get('Source','/__unknown__'))
                if mount.get('Name') in volumes or any(root == path or root in path.parents or path in root.parents for root in roots):
                    fail('recovery_failed_root_active','a running container uses the retained failed deployment')
        backup.assert_no_open_writers(roots)

    def retire_added_containers(self, execute):
        """Remove exact stopped, admitted service additions, retaining all volumes."""
        operation = self.operation
        docker = operation.prepared.docker
        rows = docker.containers()
        if 'retired_containers' not in self.state:
            added = set(operation.prepared.config['services'])-set(operation.deployment.plan.config['services'])
            retained = []
            seen = set()
            for container in rows:
                labels = container.get('Config',{}).get('Labels') or {}
                service = labels.get(mapping.LABEL+'service')
                if labels.get(mapping.LABEL+'project') != operation.state['project'] or service not in added:
                    continue
                expected_id = operation.state.get('activation',{}).get('services',{}).get(service,{}).get('container_id')
                if (container.get('State',{}).get('Running')
                        or service in seen or expected_id and expected_id != container['Id']
                        or container.get('Image') != operation.prepared.receipt['images'][service]['id']
                        or labels.get(mapping.LABEL+'project.working_dir') != operation.state['project_directory']
                        or labels.get(mapping.LABEL+'project.config_files') != str(operation.prepared.directory/'compose.json')
                        or labels.get(mapping.LABEL+'oneoff','False').lower() != 'false'):
                    fail('recovery_retired_container_changed','an unrecognized service addition is preserved')
                definition = operation.prepared.config['services'][service]
                temporary = {item.split(':',1)[0] for item in definition.get('tmpfs',[])}
                actual_mounts = {item['Destination']:{'type':item.get('Type'),
                    'source':item.get('Name') if item.get('Type') == 'volume' else item.get('Source'),
                    'read_only':not item.get('RW',False)} for item in container.get('Mounts',[])
                    if not (item.get('Type') == 'tmpfs' and item.get('Destination') in temporary)}
                image = docker.run(['docker','image','inspect',container['Image']],json_output=True)[0]
                environment = dict(item.split('=',1) for item in image.get('Config',{}).get('Env',[]) if '=' in item)
                environment.update({name:str(value) for name,value in definition.get('environment',{}).items()})
                observed_env = dict(item.split('=',1) for item in container.get('Config',{}).get('Env',[]) if '=' in item)
                if (actual_mounts != mapping.mounts(operation.prepared.config,service) or observed_env != environment
                        or container.get('HostConfig',{}).get('Privileged')):
                    fail('recovery_retired_container_changed','a service addition changed its admitted mounts/environment; it is preserved')
                seen.add(service)
                retained.append({'name':container['Name'],'identity':mapping._container_identity(container)})
            self.transaction._save('verifying',retired_containers=retained,retired_done=[])
        for owner in self.state['retired_containers']:
            identifier = owner['identity']['Id']
            rows = docker.containers()
            found = [row for row in rows if row.get('Id') == identifier or row.get('Name') == owner['name']]
            if found:
                if len(found) != 1 or mapping._container_identity(found[0]) != owner['identity'] or found[0].get('Name') != owner['name']:
                    fail('recovery_retired_container_changed','a replaced service container is preserved')
                if identifier in self.state['retired_done']:
                    fail('recovery_retired_container_changed','a removed service container unexpectedly reappeared')
                execute(docker,['docker','rm',identifier],self.attempt('recovery-retire'),'retired-service-remove')
            self.transaction._save('verifying',retired_done=sorted(set(self.state['retired_done'])|{identifier}))

    def helper(self, plan):
        operation = copy.copy(self.operation)
        operation.deployment = copy.copy(self.operation.deployment)
        operation.deployment.plan,operation.deployment.docker = plan,plan.docker
        operation.root = plan.resources['control'].root
        operation.state = {**self.operation.state,'database_node_id':self.operation.state['preflight']['node_id']}
        return operation

    def baseline(self):
        path = Path(self.state['attempt']['directory'])/'original-baseline.json'
        content = backup.json_bytes(self.operation.state['preflight'])
        if not path.exists() and not path.is_symlink():
            backup.private_write(path,content)
            descriptor = os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
            try:
                os.fchown(descriptor,10001,10001)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            backup.fsync_directory(path.parent)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 10001
                or stat.S_IMODE(info.st_mode) != 0o600 or path.read_bytes() != content):
            fail('recovery_baseline_changed','the readonly restored identity baseline changed')
        return path,backup.digest(path)[1]

    def prepare_receipt(self, plan):
        original = self.operation.prepared.receipt
        images,cache = {},{}
        platform = original['images']['web']
        for name,row in original['old_deployment']['services'].items():
            identifier = row['image_id']
            if identifier not in cache:
                value = plan.docker.run(['docker','image','inspect',identifier],json_output=True)[0]
                cache[identifier] = {'id':value['Id'],'os':value['Os'],'architecture':value['Architecture']}
            images[name] = cache[identifier]
            if images[name] != {'id':identifier,'os':platform['os'],'architecture':platform['architecture']}:
                fail('recovery_image_changed','an original service image or platform changed before restored opening')
        receipt = {'version':1,'phase':'restored_original','id':self.operation.state['id'],'project':plan.name,
            'project_directory':self.operation.state['project_directory'],'recovery_id':self.state['id'],
            'original_prepared_directory':str(self.operation.prepared.directory),
            'original_prepared_sha256':self.operation.state['prepared_sha256'],
            'compose_sha256':self.state['attempt']['compose_sha256'],
            'configuration_sha256':self.state['attempt']['configuration_sha256'],'images':images,
            **{name:original[name] for name in ('source_files','source_env_files','source_inputs')}}
        if self.state.get('mode') == 'original_abort':
            receipt['recovery_mode'] = 'original_abort'
        path = Path(self.state['attempt']['directory'])/'prepared.json'
        if path.exists() or path.is_symlink():
            if registry.private_json(path) != receipt:
                fail('recovery_receipt_changed','an existing restored receipt is preserved')
        else:
            preparation._store(path,receipt)
        return backup.digest(path)[1]

    def publish(self, phase):
        operation = self.operation
        value = {'version':1,'id':operation.state['id'],'phase':phase,'project':operation.state['project'],
            'project_directory':operation.state['project_directory'],'origin':'controlled_recovery',
            'prepared_directory':self.state['attempt']['directory'],'prepared_sha256':self.state['prepared_sha256'],
            'node_id':operation.state['preflight']['node_id'],'generation':operation.state['preflight']['generation'],
            'roots':self.state['restored_roots']}
        previous = self.state.get('registry_sha256',operation.state.get('registry_sha256',operation.state.get('previous_registry_sha256')))
        if phase == 'opening':
            current = registry.current_hash(operation.deployment.docker.directory,operation.state['project'])
            if current is not None and registry.private_json(operation.deployment.docker.directory/registry.CURRENT) == registry.sealed({**value,'phase':'ready'}):
                return current  # A lost final reply must never downgrade a ready receipt.
        return registry.publish(operation.deployment.docker.directory,value,previous_sha256=previous)

    def verify_cold(self, plan, execute):
        self.operation.prepared.docker.stop()
        self.transaction._original_cold()
        self.transaction._restored_roots(plan)
        self.transaction._payloads_at_target(plan)
        self.retire_added_containers(execute)
        self.transaction._cold_plan(plan).assert_cold()
        attempt = self.attempt('recovery-preflight')
        try:
            if plan.engine == 'mysql':
                execute(plan.docker,plan.docker.base+['up','-d','--force-recreate','--no-deps','--no-build','--pull','never',
                    '--wait','--wait-timeout','90','mysql'],attempt,'restored-engine-start',timeout=120)
                self.failed_resources_cold()
            checked = self.helper(plan)._database_helper(self.identity_command,attempt,execute=execute)
        finally:
            # Abrupt process death is reconciled by repeating this whole
            # verifying phase, which first joins every project writer.
            plan.docker.stop()
        self.transaction._original_cold()
        self.transaction._cold_plan(plan).assert_cold()
        self.transaction._restored_roots(plan)
        if not self.valid_identity(checked):
            fail('recovery_identity_changed','restored schema generation, node identity, keys or idle state changed')
        baseline,checksum = self.baseline()
        receipt_sha = self.prepare_receipt(plan)
        self.transaction._save('verified',preflight=checked,baseline=str(baseline),baseline_sha256=checksum,prepared_sha256=receipt_sha)

    def valid_identity(self, checked):
        return checked == self.operation.state['preflight']

    def result_details(self):
        return {'failed_roots_retained':True,'database_restored':True,'payloads_copied':False}

    def verify_created(self):
        directory = Path(self.state['attempt']['directory'])
        receipt = registry.private_json(directory/'prepared.json')
        current = mapping.Deployment.discover(self.operation.deployment.docker.directory,
            prepared=SimpleNamespace(directory=directory,receipt=receipt))
        if (mapping.checksum(current.plan.config) != self.state['attempt']['configuration_sha256']
                or any(row['running'] for row in current.observed.values())
                or {name:row['image_id'] for name,row in current.observed.items()}
                != {name:row['id'] for name,row in receipt['images'].items()}
                or {name:upgrade._identity(resource.root) for name,resource in current.plan.resources.items()}
                != self.state['restored_roots']):
            fail('recovery_created_configuration_changed','stopped restored containers do not bind the exact replacement images and roots')

    def open(self, *, execute=preparation._command):
        operation = self.operation
        with backup.operation_lock(operation.state['project']):
            if upgrade._read(operation.path) != operation.state:
                fail('upgrade_state_changed','reload the persisted controlled recovery before opening')
            if self.state and self.state['stage'] == 'opened':
                return copy.deepcopy(self.state['open_result'])
            self.transaction._validate()
            if self.state['stage'] not in {'restored_fenced','verifying','verified','opening'}:
                fail('recovery_phase_invalid','restored opening requires completed state and payload recovery')
            plan = self.transaction._plan()
            if self.state['stage'] in {'restored_fenced','verifying'}:
                if self.state['stage'] == 'restored_fenced':
                    self.transaction._save('verifying')
                self.verify_cold(plan,execute)
            baseline,checksum = self.baseline()
            if str(baseline) != self.state['baseline'] or checksum != self.state['baseline_sha256']:
                fail('recovery_baseline_changed','the retained original identity baseline changed after verification')
            if self.prepare_receipt(plan) != self.state['prepared_sha256']:
                fail('recovery_receipt_changed','the retained old-image deployment receipt changed')
            root = plan.resources['control'].root
            marker = root/upgrade.HARD
            if self.state['stage'] == 'verified':
                plan.docker.stop()
                self.transaction._original_cold()
                self.transaction._cold_plan(plan).assert_cold()
                self.transaction._restored_roots(plan)
                self.transaction._payloads_at_target(plan)
                # Compose may reuse an unchanged service even when its input
                # file moved. Rebind every stopped container before opening,
                # without starting it or removing any persistent volume.
                execute(plan.docker,plan.docker.base+['create','--force-recreate','--no-build','--pull','never'],
                    self.attempt('recovery-create'),'restored-package-create',timeout=150)
                self.transaction._original_cold()
                self.transaction._cold_plan(plan).assert_cold()
                self.transaction._restored_roots(plan)
                self.transaction._payloads_at_target(plan)
                self.verify_created()
                # Persist before publishing/unlinking. Later retries can never
                # restore an old snapshot over possibly newer business writes.
                self.transaction._save('opening',opening_boundary=True)
            self.failed_resources_cold()
            fenced = marker.exists() or marker.is_symlink()
            self.transaction._restored_roots(plan,fenced=fenced)
            registered = self.publish('opening')
            self.transaction._save('opening',registry_sha256=registered)
            if fenced:
                plan.docker.stop()
                self.transaction._original_cold()
                self.transaction._cold_plan(plan).assert_cold()
                self.transaction._restored_roots(plan)
                self.transaction._payloads_at_target(plan)
                marker.unlink()
                backup.fsync_directory(root)
            attempt = self.attempt('recovery-opening')
            execute(plan.docker,plan.docker.base+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','120'],
                attempt,'restored-business-start',timeout=150)
            current = mapping.Deployment.discover(operation.deployment.docker.directory)
            if mapping.checksum(current.plan.config) != self.state['attempt']['configuration_sha256']:
                fail('recovery_opened_configuration_changed','restored running services differ from the retained old configuration')
            helper = self.helper(plan)
            helper.state['database_baseline_sha256'] = checksum
            proof = helper._database_helper('probe-restored',attempt,baseline=baseline,execute=execute)
            tls = str(plan.config['services']['web'].get('environment',{}).get('TLS_ENABLED',False)).lower() in {'true','1','yes','on','y'}
            if (proof.get('phase') != 'endpoint_restored' or proof.get('generation') != operation.state['preflight']['generation']
                    or proof.get('node_id') != operation.state['preflight']['node_id'] or proof.get('baseline_sha256') != checksum
                    or proof.get('business_opened') is not True or proof.get('readiness_status') != 200
                    or proof.get('tls_enabled') is not tls
                    or tls and (proof.get('certificate_verified') is not True or proof.get('signed_identity_verified') is not True)):
                fail('recovery_endpoint_verification_failed','the recovered endpoint does not match its original schema/identity proof')
            self.failed_resources_cold()
            self.transaction._restored_roots(plan,fenced=False)
            if mapping.Deployment.discover(operation.deployment.docker.directory).summary() != current.summary():
                fail('recovery_opened_configuration_changed','restored services changed during endpoint verification')
            registered = self.publish('ready')
            result = {**proof,'phase':'restored_opened','project':operation.state['project'],
                'recovery_id':self.state['id'],**self.result_details(),
                'services':current.observed,'recovery_complete':True,'acceptance_only':operation.state['acceptance_only']}
            self.transaction._save('opened',registry_sha256=registered,open_result=result)
            return copy.deepcopy(result)
