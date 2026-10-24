"""Controlled old-state recovery retaining failed roots and volumes.

This host-only library keeps failed control roots and volumes. Its replacement
control root is a sibling, and excluded payload directories move by rename.
Recovery selection remains internal until the public operator is complete.
"""
from __future__ import annotations

import copy
import ctypes
import os
from pathlib import Path
import re
import uuid

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_prepare as preparation
from scripts import operator_upgrade as upgrade


LABEL = 'io.frontiercloud.operator.recovery'
PREPARING = frozenset({'stopping','restoring','state_restored','moving'})
PHASE_BY_STAGE = {**{stage:'recovering' for stage in PREPARING},'restored_fenced':'restored_fenced',
    'verifying':'restored_verifying','verified':'restored_verified','opening':'restored_opening','opened':'restored_opened'}
STAGES = frozenset(PHASE_BY_STAGE)
BEFORE_OPEN = frozenset({'backed_up','database_inspecting','database_preparing',
    'database_prepared','activating','active_fenced','opening_stopping','opening_stopped'})


def _fail(code, message):
    mapping.fail(code,message)


def _exists(path):
    return path.exists() or path.is_symlink()


def _same_inode(path, identity):
    return upgrade._identity(path) == {**identity,'path':str(path)}


def rename_noreplace(source, destination):
    libc = ctypes.CDLL(None,use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError:
        _fail('recovery_rename_unsupported','this host lacks an atomic no-replace directory rename')
    rename.argtypes = [ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100,os.fsencode(source),-100,os.fsencode(destination),1) != 0:
        number = ctypes.get_errno()
        raise OSError(number,os.strerror(number))


def no_nested_mounts(root, *, inventory=Path('/proc/self/mountinfo')):
    """A same-device bind mount must not be mistaken for an ordinary directory."""
    for line in inventory.read_text().splitlines():
        fields = line.split()
        if len(fields) < 10 or ' - ' not in line:
            _fail('recovery_mount_inventory_invalid','Linux mount inventory is incomplete')
        name = re.sub(r'\\([0-7]{3})',lambda match:chr(int(match[1],8)),fields[4])
        path = Path(name)
        if path == root or root in path.parents:
            _fail('recovery_nested_mount','a mounted control tree cannot be moved by recovery')


def original_config(operation):
    config = copy.deepcopy(operation.deployment.plan.config)
    for service, definition in config['services'].items():
        definition['image'] = operation.prepared.receipt['old_deployment']['services'][service]['image_id']
        definition.pop('build',None)
        definition.pop('profiles',None)
        definition['pull_policy'] = 'never'
    return config


def replacement_config(operation, control, token):
    """Retain logical volume keys, pinned old images and original private inputs."""
    config = original_config(operation)
    for definition in config['services'].values():
        for mount in definition.get('volumes',[]):
            if mount.get('target') == '/app/data':
                mount['source'] = str(control)
    for key,definition in config.get('volumes',{}).items():
        if definition.get('external') or definition.get('driver','local') != 'local' or definition.get('driver_opts'):
            _fail('recovery_volume_unsupported','recovery requires plain owned local volumes')
        definition['name'] = 'fc-recovered-'+token+'-'+mapping.checksum(key)[:12]
        definition.setdefault('labels',{})[LABEL] = token
    return config


def load_recovery(operation):
    if operation.state.get('recovery',{}).get('mode') == 'original_abort':
        from scripts.operator_abort import OriginalAbort
        return OriginalAbort(operation)
    return Recovery(operation)


class Recovery:
    def __init__(self, operation, *, docker_factory=mapping.ComposeDocker):
        self.operation = operation
        self.factory = docker_factory

    @property
    def state(self):
        return self.operation.state.get('recovery')

    def _save(self, stage, *, opening_boundary=False, **values):
        if stage not in STAGES:
            _fail('recovery_phase_invalid','unknown controlled recovery phase')
        state = {**self.state,**copy.deepcopy(values),'stage':stage}
        self.operation._save(PHASE_BY_STAGE[stage],recovery=state,
            **({'business_may_have_written':True} if opening_boundary else {}))

    def _cold_plan(self, plan):
        # The failed package can have admitted extra services. Keep their
        # stopped containers, while still rejecting every running writer and
        # every undeclared service. This roster is never used to start services.
        cold = copy.copy(plan)
        cold.config = copy.deepcopy(plan.config)
        for name,definition in self.operation.prepared.config['services'].items():
            cold.config['services'].setdefault(name,definition)
        return cold

    def _original_cold(self):
        operation = self.operation
        operation._reconcile_helpers()
        operation._owned_fence()
        if _exists(operation.root/upgrade.MANUAL):
            _fail('manual_maintenance_active','manual maintenance is preserved during controlled recovery')
        self._cold_plan(operation.deployment.plan).assert_cold()
        no_nested_mounts(operation.root)

    def _snapshot(self):
        operation = self.operation
        snapshot = operation.prepared.directory/operation.state['snapshot']
        manifest = backup.verify(snapshot)
        if (backup.digest(snapshot/'manifest.json')[1] != operation.state['snapshot_manifest_sha256']
                or manifest['project'] != operation.state['project']
                or manifest['database_engine'] != operation.deployment.plan.engine
                or manifest['images'] != operation.deployment.plan.images):
            _fail('recovery_snapshot_changed','the cold snapshot no longer binds the original deployment')
        return snapshot,manifest

    def _begin(self):
        operation = self.operation
        if operation.state.get('business_may_have_written') or operation.state['phase'] not in BEFORE_OPEN:
            _fail('recovery_after_open_refused','an old snapshot cannot overwrite possible post-opening business writes')
        if _exists(operation.root/upgrade.MANUAL):
            _fail('manual_maintenance_active','manual maintenance is preserved; recovery is refused')
        operation._owned_fence()
        operation._prepared_inputs(preparation.sources.verify_stage)
        self._snapshot()
        no_nested_mounts(operation.root)
        payloads = {}
        for name in sorted(backup.EXCLUDED_DATA):
            path = operation.root/name
            identity = upgrade._identity(path) if _exists(path) else None
            if identity and identity['device'] != operation.state['roots']['control']['device']:
                _fail('recovery_payload_mounted','payload directories must share the control filesystem')
            payloads[name] = identity
        identifier = uuid.uuid4().hex
        operation._save('recovering',recovery={'version':1,'id':identifier,'stage':'stopping',
            'from_phase':operation.state['phase'],'parent':upgrade._identity(operation.root.parent),
            'payloads':payloads,'retained_attempts':[],'attempt':None,'moves':{}})

    def _validate(self):
        state = self.state
        if (not isinstance(state,dict) or state.get('version') != 1 or state.get('stage') not in STAGES
                or not re.fullmatch('[0-9a-f]{32}',state.get('id',''))
                or set(state.get('payloads',{})) != set(backup.EXCLUDED_DATA)
                or self.operation.state['phase'] != PHASE_BY_STAGE[state.get('stage')]
                or upgrade._identity(self.operation.root.parent) != state.get('parent')
                or bool(self.operation.state.get('business_may_have_written')) != (state.get('stage') in {'opening','opened'})):
            _fail('recovery_state_changed','the controlled recovery record no longer binds its original roots')
        self._snapshot()

    def _new_attempt(self):
        previous = self.state['attempt']
        retained = self.state['retained_attempts']+[previous] if previous else self.state['retained_attempts']
        if len(retained) >= 3:
            _fail('recovery_incomplete_retained','repeated incomplete state restores remain retained for inspection')
        identifier = uuid.uuid4().hex
        token = self.state['id']+identifier[:12]
        original = self.operation.root
        control = original.parent/(original.name+'.recovered-'+token)
        if _exists(control):
            _fail('recovery_destination_exists','a replacement control destination already exists')
        directory = self.operation.prepared.directory/('recovery-'+identifier)
        directory.mkdir(mode=0o700)
        backup.fsync_directory(directory.parent)
        config = replacement_config(self.operation,control,token)
        preparation._store(directory/'compose.json',backup.compose_values(config,encode=True))
        attempt = {'id':identifier,'token':token,'directory':str(directory),'control':str(control),
            'compose_sha256':backup.digest(directory/'compose.json')[1],
            'configuration_sha256':mapping.checksum(config)}
        self._save('restoring',attempt=attempt,retained_attempts=retained)
        return attempt

    def _plan(self):
        attempt = self.state['attempt']
        directory = backup.absolute(attempt['directory'])
        upgrade._private(directory/'compose.json')
        if backup.digest(directory/'compose.json')[1] != attempt['compose_sha256']:
            _fail('recovery_compose_changed','the retained recovery Compose input changed')
        docker = self.factory(self.operation.deployment.docker.directory,[directory/'compose.json'],self.operation.state['project'],[])
        docker.audit_file = directory/'docker-inputs.jsonl'
        config = docker.config()
        expected = replacement_config(self.operation,Path(attempt['control']),attempt['token'])
        if config != expected or mapping.checksum(config) != attempt['configuration_sha256']:
            _fail('recovery_compose_changed','replacement Compose no longer renders the admitted old configuration')
        plan = backup.Plan.load(docker,restoring=True,snapshot_resources=set(self.operation.deployment.plan.resources))
        if (set(plan.resources) != set(self.operation.deployment.plan.resources)
                or plan.resources['control'].source != attempt['control']
                or plan.images != self.operation.deployment.plan.images):
            _fail('recovery_mapping_changed','replacement resources or old image identities changed')
        return plan

    def _volumes(self, plan, *, create=False):
        token = self.state['attempt']['token']
        used = {mount['source'] for definition in plan.config['services'].values()
            for mount in definition.get('volumes',[]) if mount.get('type') == 'volume'}
        for key,definition in plan.config.get('volumes',{}).items():
            if key not in used:
                continue
            name = definition['name']
            found = plan.docker.volume(name)
            if found is None and create:
                plan.docker.run(['docker','volume','create','--label',mapping.LABEL+'project='+plan.name,
                    '--label',mapping.LABEL+'volume='+key,'--label',LABEL+'='+token,name])
                found = plan.docker.volume(name)
            labels = (found.get('Labels') or {}) if found else {}
            if (not found or found.get('Driver') != 'local' or found.get('Options')
                    or labels.get(mapping.LABEL+'project') != plan.name
                    or labels.get(mapping.LABEL+'volume') != key or labels.get(LABEL) != token):
                _fail('recovery_volume_changed','an unrecognized replacement volume is preserved')
        for resource in plan.resources.values():
            if resource.kind == 'volume':
                resource.root = plan.volume_root(resource)

    def _restored_roots(self, plan, *, fenced=True):
        self._volumes(plan)
        if {key:upgrade._identity(resource.root) for key,resource in plan.resources.items()} != self.state['restored_roots']:
            _fail('recovery_root_changed','a restored persistent root changed before payload reconciliation')
        marker = plan.resources['control'].root/upgrade.HARD
        if fenced:
            info = self.operation._marker(marker)
            if info.st_nlink != 1 or upgrade._fence_identity(info) != self.state['restored_fence_identity']:
                _fail('recovery_fence_changed','the restored fence is not its recorded inode')
        elif _exists(marker):
            _fail('recovery_fence_changed','a hard maintenance fence appeared after restored opening; it is preserved')
        if _exists(plan.resources['control'].root/upgrade.MANUAL):
            _fail('manual_maintenance_active','a new manual fence is preserved during recovery')
        no_nested_mounts(plan.resources['control'].root)

    def _payloads_at_target(self, plan):
        target = plan.resources['control'].root
        for name,identity in self.state['payloads'].items():
            source,destination = self.operation.root/name,target/name
            if _exists(source) or (identity is None and _exists(destination)):
                _fail('recovery_payload_changed','a payload root appeared in an unexpected destination')
            if identity is not None and (not _exists(destination) or not _same_inode(destination,identity)):
                _fail('recovery_payload_changed','restored payloads no longer retain their original directory identity')

    def _move_payloads(self, plan):
        target = plan.resources['control'].root
        for name,identity in self.state['payloads'].items():
            self._original_cold()
            self._cold_plan(plan).assert_cold()
            self._restored_roots(plan)
            source,destination = self.operation.root/name,target/name
            if identity is None:
                if _exists(source) or _exists(destination):
                    _fail('recovery_payload_changed','an originally absent payload directory appeared; it is preserved')
                continue
            if _exists(source):
                if not _same_inode(source,identity) or _exists(destination):
                    _fail('recovery_payload_changed','payload roots changed or both destinations are occupied')
                if target.stat().st_dev != identity['device']:
                    _fail('recovery_cross_device','payload recovery requires same-filesystem rename')
                self._save('moving',moves={**self.state['moves'],name:'intent'})
                self._original_cold()
                self._cold_plan(plan).assert_cold()
                self._restored_roots(plan)
                if not _same_inode(source,identity) or _exists(destination):
                    _fail('recovery_payload_changed','payload roots changed after the durable move intent')
                rename_noreplace(source,destination)
                backup.fsync_directory(source.parent)
                backup.fsync_directory(destination.parent)
            elif self.state['moves'].get(name) not in {'intent','moved'}:
                _fail('recovery_payload_changed','a missing payload has no durable move intent')
            if not _exists(destination) or not _same_inode(destination,identity):
                _fail('recovery_payload_changed','the moved payload does not retain its original inode')
            self._save('moving',moves={**self.state['moves'],name:'moved'})

    def prepare(self):
        """Restore state and relocate payloads, leaving every service stopped."""
        operation = self.operation
        with backup.operation_lock(operation.state['project']):
            if upgrade._read(operation.path) != operation.state:
                _fail('upgrade_state_changed','reload the recorded operation before recovering')
            if operation.state['phase'] not in {'recovering','restored_fenced'}:
                self._begin()
            self._validate()
            if self.state['stage'] not in PREPARING | {'restored_fenced'}:
                _fail('recovery_phase_invalid','resume the recorded restored-service opening; state restoration is no longer allowed')
            if self.state['stage'] == 'stopping':
                operation.prepared.docker.stop()
                self._original_cold()
                self._save('restoring')
            self._original_cold()
            if self.state['stage'] == 'restoring':
                self._new_attempt()
                plan = self._plan()
                self._volumes(plan,create=True)
                self._original_cold()
                snapshot,_manifest = self._snapshot()
                backup.restore(self._cold_plan(plan),snapshot)
                self._original_cold()
                marker = plan.resources['control'].root/upgrade.HARD
                info = operation._marker(marker)
                if info.st_nlink != 1:
                    _fail('recovery_fence_changed','the restored hard fence must be an independent private file')
                self._save('state_restored',restored_roots={key:upgrade._identity(resource.root) for key,resource in plan.resources.items()},
                    restored_fence_identity=upgrade._fence_identity(info))
            plan = self._plan()
            self._restored_roots(plan)
            if self.state['stage'] in {'state_restored','moving'}:
                self._move_payloads(plan)
                self._save('restored_fenced')
            self._original_cold()
            self._cold_plan(plan).assert_cold()
            self._restored_roots(plan)
            self._payloads_at_target(plan)
            return {'phase':'restored_fenced','project':operation.state['project'],'recovery_id':self.state['id'],
                'control_root':self.state['attempt']['control'],'compose_file':str(Path(self.state['attempt']['directory'])/'compose.json'),
                'failed_roots_retained':True,'payloads_copied':False,'services_started':False,
                'business_opened':False,'recovery_complete':False}

    def open(self, *, execute=preparation._command):
        from scripts.operator_recovery_open import Opening
        return Opening(self).open(execute=execute)
