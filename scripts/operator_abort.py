"""Cancel an unmutated upgrade or reopen its unchanged original resources."""
from __future__ import annotations

import copy
from pathlib import Path
import uuid

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_prepare as preparation
from scripts import operator_registry as registry
from scripts import operator_recovery as recovery
from scripts import operator_recovery_open as reopening
from scripts import operator_source as sources
from scripts import operator_upgrade as upgrade


EARLY_PHASES = frozenset({'images_prepared','fencing','fenced','stopping','cold','backing_up'})


class OriginalOpening(reopening.Opening):
    identity_command = 'identity-original'

    def failed_resources_cold(self):
        self.operation._roots()
        allowed = frozenset({'mysql'}) if self.state['stage'] == 'verifying' else frozenset()
        if allowed:
            self.operation.deployment.plan.assert_cold(allow_services=allowed)
        else:
            self.operation.deployment.plan.assert_cold(allow_project=True)

    def retire_added_containers(self, execute):
        # No candidate activation can precede these phases. Unknown additions
        # fail the strict original roster check instead of being removed.
        self.operation.deployment.plan.assert_cold()
        self.transaction._save('verifying',retired_containers=[],retired_done=[])

    def valid_identity(self, checked):
        return (checked.get('phase') == 'original_identity_verified'
            and {**checked,'phase':'preflight_passed'} == self.operation.state['preflight'])

    def result_details(self):
        return {'mode':'original_abort','upgrade_aborted':True,'database_restored':False,
            'original_resources_reused':True,'failed_roots_retained':False,'payloads_copied':False,
            'retained_snapshot_paths':self.state['retained_snapshot_paths']}


class OriginalAbort(recovery.Recovery):
    def _snapshot(self):
        if self.state.get('mode') != 'original_abort' or self.state.get('from_phase') not in EARLY_PHASES:
            mapping.fail('abort_database_may_have_changed','only an upgrade that never reached database preparation can reuse original state')

    def _cold_plan(self, plan):
        return plan

    def _original_cold(self):
        self.operation._reconcile_helpers()
        self.operation._owned_fence()
        if recovery._exists(self.operation.root/upgrade.MANUAL):
            mapping.fail('manual_maintenance_active','manual maintenance is preserved while cancelling this upgrade')
        self.operation.deployment.plan.assert_cold()

    def _volumes(self, plan, *, create=False):
        if create:
            mapping.fail('abort_resource_creation_refused','original-state abort never allocates replacement volumes')
        used = {mount['source'] for definition in plan.config['services'].values()
            for mount in definition.get('volumes',[]) if mount.get('type') == 'volume'}
        for key in used:
            name = plan.config['volumes'][key].get('name',plan.name+'_'+key)
            if plan.volume_root(backup.Resource('observation','volume',name,volume_key=key)) is None:
                mapping.fail('abort_volume_missing','an original volume is missing; it is not recreated')

    def _restored_roots(self, plan, *, fenced=True):
        self._volumes(plan)
        if {key:upgrade._identity(resource.root) for key,resource in plan.resources.items()} != self.state['restored_roots']:
            mapping.fail('abort_root_changed','an original persistent root changed; no state is replaced')
        marker = self.operation.root/upgrade.HARD
        if fenced:
            value = self.operation._marker(marker)
            if value.st_nlink != 1 or upgrade._fence_identity(value) != self.state['restored_fence_identity']:
                mapping.fail('maintenance_identity_changed','the original owned maintenance fence changed')
        elif recovery._exists(marker):
            mapping.fail('maintenance_identity_changed','a hard fence appeared after original service opening; it is preserved')
        if recovery._exists(self.operation.root/upgrade.MANUAL):
            mapping.fail('manual_maintenance_active','manual maintenance is preserved while reopening original services')

    def _payloads_at_target(self, plan):
        for name,identity in self.state['payloads'].items():
            path = self.operation.root/name
            if identity is None:
                if recovery._exists(path):
                    mapping.fail('abort_payload_changed','an original payload namespace appeared during the cold abort')
            elif not recovery._exists(path) or upgrade._identity(path) != identity:
                mapping.fail('abort_payload_changed','an original payload root changed; it is preserved')

    def _plan(self):
        directory = Path(self.state['attempt']['directory'])
        registry.private_json(directory/'compose.json')
        if backup.digest(directory/'compose.json')[1] != self.state['attempt']['compose_sha256']:
            mapping.fail('abort_compose_changed','the pinned original Compose input changed')
        docker = self.factory(self.operation.deployment.docker.directory,[directory/'compose.json'],self.operation.state['project'],[])
        docker.audit_file = directory/'docker-inputs.jsonl'
        config = docker.config()
        if config != recovery.original_config(self.operation) or mapping.checksum(config) != self.state['attempt']['configuration_sha256']:
            mapping.fail('abort_compose_changed','original-state abort cannot remap private inputs, mounts or service images')
        plan = backup.Plan.load(docker)
        if set(plan.resources) != set(self.operation.state['roots']) or plan.images != self.operation.deployment.plan.images:
            mapping.fail('abort_mapping_changed','original resources or image identities changed')
        return plan

    def _original_containers(self):
        expected = self.operation.prepared.receipt['old_deployment']['services']
        rows = [row for row in self.operation.deployment.docker.containers()
            if (row.get('Config',{}).get('Labels') or {}).get(mapping.LABEL+'project') == self.operation.state['project']]
        if {row['Id'] for row in rows} != {row['container_id'] for row in expected.values()}:
            mapping.fail('abort_original_containers_changed','the pre-snapshot original container roster changed; unknown containers are preserved')
        for row in rows:
            labels = row.get('Config',{}).get('Labels') or {}
            service = labels.get(mapping.LABEL+'service')
            if (service not in expected or row['Id'] != expected[service]['container_id']
                    or row.get('Image') != expected[service]['image_id']
                    or labels.get(mapping.LABEL+'project.working_dir') != self.operation.state['project_directory']
                    or labels.get(mapping.LABEL+'oneoff','False').lower() != 'false'):
                mapping.fail('abort_original_containers_changed','an original service identity changed; no container is stopped')

    def _select(self):
        operation = self.operation
        if operation.state.get('business_may_have_written') or operation.state['phase'] not in EARLY_PHASES:
            mapping.fail('abort_database_may_have_changed','use verified snapshot recovery after the database preparation boundary')
        operation._prepared_inputs(sources.verify_stage,snapshot=False)
        operation._roots()
        self._original_containers()
        if registry.current_hash(operation.deployment.docker.directory,operation.state['project']) != operation.state.get('previous_registry_sha256'):
            mapping.fail('abort_deployment_receipt_changed','another deployment receipt appeared; it is preserved')
        marker = operation.root/upgrade.HARD
        if operation.state['phase'] in {'images_prepared','fencing'} and not recovery._exists(marker):
            result = {'phase':'aborted','project':operation.state['project'],'abort_complete':True,
                'deployment_changed':False,'services_restarted':False,'database_restored':False,
                'database_preparation_started':False,'business_read_verified':False,
                'manual_maintenance_preserved':recovery._exists(operation.root/upgrade.MANUAL)}
            operation._save('aborted',abort_result=result,business_may_have_written=True)
            return result
        if operation.state['phase'] == 'fencing':
            operation._fence()
        operation._owned_fence()
        operation.deployment.plan.assert_cold(allow_project=True)
        identifier = uuid.uuid4().hex
        directory = operation.prepared.directory/('original-abort-'+identifier)
        directory.mkdir(mode=0o700)
        backup.fsync_directory(directory.parent)
        config = recovery.original_config(operation)
        preparation._store(directory/'compose.json',backup.compose_values(config,encode=True))
        paths = list(operation.state.get('retained_snapshots',[]))
        if operation.state.get('snapshot'):
            paths.append(operation.state['snapshot'])
        state = {'version':1,'id':identifier,'mode':'original_abort','from_phase':operation.state['phase'],'stage':'stopping',
            'parent':upgrade._identity(operation.root.parent),'payloads':{name:upgrade._identity(operation.root/name)
                if recovery._exists(operation.root/name) else None for name in sorted(backup.EXCLUDED_DATA)},
            'restored_roots':copy.deepcopy(operation.state['roots']),'restored_fence_identity':operation.state['fence_identity'],
            'retained_snapshot_paths':[str(operation.prepared.directory/name) for name in sorted(set(paths))],
            'retained_attempts':[],'moves':{},'attempt':{'directory':str(directory),'control':str(operation.root),
                'compose_sha256':backup.digest(directory/'compose.json')[1],'configuration_sha256':mapping.checksum(config)}}
        operation._save('recovering',recovery=state)
        return None

    def prepare(self):
        operation = self.operation
        with backup.operation_lock(operation.state['project']):
            if upgrade._read(operation.path) != operation.state:
                mapping.fail('upgrade_state_changed','reload the recorded operation before cancelling')
            self._validate()
            if self.state['stage'] not in {'stopping','restored_fenced'}:
                mapping.fail('abort_phase_invalid','resume original-service opening; original data cannot be replaced')
            if recovery._exists(operation.root/upgrade.MANUAL):
                mapping.fail('manual_maintenance_active','the selected abort preserves manual maintenance')
            if self.state['stage'] == 'stopping':
                operation.deployment.docker.stop()
                self._original_cold()
                self._save('restored_fenced')
            plan = self._plan()
            self._original_cold()
            self._restored_roots(plan)
            self._payloads_at_target(plan)
            return {'phase':'original_fenced','mode':'original_abort','database_restored':False,'services_started':False}

    def open(self, *, execute=preparation._command):
        return OriginalOpening(self).open(execute=execute)

    def abort(self, *, execute=preparation._command):
        operation = self.operation
        with backup.operation_lock(operation.state['project']):
            if upgrade._read(operation.path) != operation.state:
                mapping.fail('upgrade_state_changed','reload the current operation before cancelling')
            if operation.state['phase'] == 'aborted':
                return copy.deepcopy(operation.state['abort_result'])
            if operation.state.get('recovery',{}).get('mode') != 'original_abort':
                result = self._select()
                if result is not None:return result
        if self.state['stage'] in {'stopping','restored_fenced'}:
            self.prepare()
        return self.open(execute=execute)


def recover(directory, *, acceptance=False, execute=preparation._command):
    operation = upgrade.Upgrade.load(directory,acceptance=acceptance,check_images=False)
    if operation.state['phase'] == 'aborted':
        return copy.deepcopy(operation.state['abort_result'])
    if operation.state['phase'] in EARLY_PHASES or operation.state.get('recovery',{}).get('mode') == 'original_abort':
        return OriginalAbort(operation).abort(execute=execute)
    transaction = recovery.load_recovery(operation)
    if operation.state.get('recovery',{}).get('stage') not in {'restored_fenced','verifying','verified','opening','opened'}:
        transaction.prepare()
    return transaction.open(execute=execute)
