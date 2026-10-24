"""Owned fencing, state capture, database preparation and fenced activation.

Starting the prepared package is distinct from opening business traffic.
Opening is a one-way boundary. Recovery never downgrades a live database.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_release as releases
from scripts import operator_source as sources
from scripts import operator_prepare as preparation
from scripts import operator_registry as registry


PHASES = {"images_prepared", "fencing", "fenced", "stopping", "cold", "backing_up", "backed_up",
          "database_inspecting", "database_preparing", "database_prepared", 'activating', 'active_fenced',
          'opening_stopping','opening_stopped','opening','opened','recovering','restored_fenced',
          'restored_verifying','restored_verified','restored_opening','restored_opened','aborted'}
HARD = ".frontiercloud-native-maintenance"
MANUAL = ".frontiercloud-maintenance"
HELPER_LABEL = 'io.frontiercloud.operator.helper'


def _fail(code, message):
    mapping.fail(code, message)


def _identity(path):
    path = backup.absolute(path)
    value = path.lstat()
    if not stat.S_ISDIR(value.st_mode):
        _fail("upgrade_root_changed", "persistent state is not a real directory")
    return {"path": str(path), "device": value.st_dev, "inode": value.st_ino,
            "uid": value.st_uid, "gid": value.st_gid, "mode": stat.S_IMODE(value.st_mode)}


def _sealed(value):
    body = {key: item for key, item in value.items() if key != "state_sha256"}
    return body | {"state_sha256": hashlib.sha256(backup.json_bytes(body)).hexdigest()}


def _fence_identity(value):
    return {"device": value.st_dev, "inode": value.st_ino, "uid": value.st_uid, "gid": value.st_gid,
            "mode": stat.S_IMODE(value.st_mode), "size": value.st_size, "mtime_ns": value.st_mtime_ns, "ctime_ns": value.st_ctime_ns}


def _private(path):
    info = backup.absolute(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
        _fail("unsafe_upgrade_state", "operator state must be a private regular file owned by this host operator")


def _read(path):
    _private(path)
    value = backup.bounded_json(path)
    if not isinstance(value, dict) or value.get("version") != 1 or value.get("phase") not in PHASES or _sealed(value) != value:
        _fail("upgrade_state_invalid", "upgrade state is incomplete or has changed")
    return value


def _join_helper(docker, owner, *, timeout=20):
    """Stop only this recorded one-off, then join its daemon-side removal."""
    def find():
        values = docker.run(['docker','ps','-aq','--filter','name=^/'+owner['name']+'$']).splitlines()
        if len(values) > 1:
            _fail('helper_identity_changed','helper name does not identify one container')
        return values
    found = find()
    if not found:
        return
    identifier = found[0]
    try:
        value = docker.run(['docker','inspect',identifier],json_output=True)[0]
    except backup.BackupError:
        if not find():
            return  # --rm may finish between ps and inspect.
        raise
    labels = value.get('Config',{}).get('Labels') or {}
    if (value.get('Name') != '/'+owner['name'] or value.get('Image') != owner['image']
            or labels.get(HELPER_LABEL) != owner['token']
            or labels.get(mapping.LABEL+'project') != owner['project']
            or labels.get(mapping.LABEL+'oneoff','').lower() != 'true'
            or value.get('HostConfig',{}).get('AutoRemove') is not True):
        _fail('helper_identity_changed','an unrecognized helper is preserved; no container is stopped')
    if value.get('State',{}).get('Running'):
        try:
            docker.run(['docker','stop','--timeout','10',identifier])
        except backup.BackupError:
            if find():
                raise
    deadline = time.monotonic()+timeout
    while find():
        if time.monotonic() >= deadline:
            _fail('helper_cleanup_pending','the owned helper has not finished daemon-side removal')
        time.sleep(.1)


class Upgrade:
    def __init__(self, deployment, prepared, state):
        self.deployment, self.prepared, self.state = deployment, prepared, state
        self.path = prepared.directory / "upgrade-state.json"
        self.root = deployment.plan.resources["control"].root
        deployment.plan.docker.audit_file = prepared.directory / "docker-inputs.jsonl"

    @classmethod
    def begin(cls, deployment, prepared, *, verify=sources.verify_stage, public_verify=releases.select_latest, acceptance=False):
        """Called under the same project lock used by existing backup operators."""
        with backup.operation_lock(deployment.plan.name):
            preparation.host_records_outside_data(deployment)
            _private(prepared.directory / "prepared.json")
            receipt = backup.bounded_json(prepared.directory / "prepared.json")
            if receipt != prepared.receipt or receipt.get("phase") != "images_prepared" or receipt.get("project") != deployment.plan.name:
                _fail("prepared_receipt_changed", "the prepared source/images receipt changed")
            if receipt.get("acceptance_only") and not acceptance:
                _fail("source_not_published", "unpublished native fixtures cannot authorize a production upgrade")
            if not receipt.get("acceptance_only") and public_verify() != receipt["provenance"]:
                _fail("release_proof_changed", "the selected public source proof changed before fencing")
            checked = verify(Path(receipt["stage_directory"]))
            if checked["stage_sha256"] != receipt["source_stage_sha256"]:
                _fail("source_stage_changed", "candidate source changed before fencing")
            for filename, key in (("compose.json", "compose_sha256"), ("old-compose.json", "old_compose_sha256")):
                if backup.digest(prepared.directory / filename)[1] != receipt[key]:
                    _fail("prepared_compose_changed", "prepared Compose material changed")
            current = mapping.Deployment.discover(deployment.docker.directory)
            if current.summary() != receipt["old_deployment"] or current.inputs != deployment.inputs:
                _fail("deployment_inputs_changed", "the original deployment changed before fencing")
            for name, expected in receipt["images"].items():
                image = deployment.docker.run(["docker", "image", "inspect", expected["id"]], json_output=True)[0]
                if {"id": image["Id"], "os": image["Os"], "architecture": image["Architecture"]} != expected:
                    _fail("prepared_image_changed", "a prepared image identity/platform changed")
            root = deployment.plan.resources["control"].root
            if any((root / name).exists() or (root / name).is_symlink() for name in (HARD, MANUAL)):
                _fail("maintenance_already_owned", "an existing manual/operator fence cannot be claimed by this upgrade")
            previous_operation = registry.operation(deployment.docker.directory)
            previous_operation_sha = None
            if previous_operation:
                if previous_operation['project'] != deployment.plan.name:
                    _fail('upgrade_identity_changed','an operation reference belongs to another Compose project')
                previous_state = _read(Path(previous_operation['prepared_directory'])/'upgrade-state.json')
                if any(previous_state.get(key) != previous_operation.get(key) for key in ('id','project','project_directory','prepared_sha256')):
                    _fail('upgrade_identity_changed','the previous operation journal no longer matches its reference')
                completed = (previous_state.get('abort_result') if previous_state['phase'] == 'aborted'
                    else previous_state.get('open_result') if previous_state['phase'] == 'opened'
                    else previous_state.get('recovery',{}).get('open_result') if previous_state['phase'] == 'restored_opened' else None)
                if (not isinstance(completed,dict) or completed.get('phase') != previous_state['phase']
                        or completed.get('abort_complete' if previous_state['phase'] == 'aborted' else 'upgrade_complete' if previous_state['phase'] == 'opened' else 'recovery_complete') is not True
                        or completed.get('project') != deployment.plan.name):
                    _fail('upgrade_in_progress','resume or recover the existing operation before preparing another upgrade')
                previous_operation_sha = backup.digest(deployment.docker.directory/registry.OPERATION)[1]
            previous_registry_sha = registry.current_hash(deployment.docker.directory,deployment.plan.name)
            state = _sealed({"version": 1, "revision": 1, "phase": "images_prepared", "id": receipt["id"],
                "project": deployment.plan.name, "project_directory": str(deployment.docker.directory),
                "prepared_sha256": backup.digest(prepared.directory / "prepared.json")[1],
                "roots": {key: _identity(value.root) for key, value in deployment.plan.resources.items()},
                "old_deployment": receipt["old_deployment"], "acceptance_only": receipt["acceptance_only"],
                'previous_registry_sha256':previous_registry_sha,
                'previous_registry':registry.private_json(deployment.docker.directory/registry.CURRENT) if previous_registry_sha else None,
                'previous_operation':previous_operation,'previous_operation_sha256':previous_operation_sha,
                "retained_snapshots": [], "retained_fence_material": []})
            operation = cls(deployment, prepared, state)
            state = _sealed({**state, 'preflight': operation._live_preflight()})
            backup.private_write(prepared.directory / "upgrade-state.json", backup.json_bytes(state))
            backup.fsync_directory(prepared.directory)
            registry.publish_operation(deployment.docker.directory,{
                'version':1,'phase':'upgrade_operation','id':receipt['id'],'project':deployment.plan.name,
                'project_directory':str(deployment.docker.directory),'prepared_directory':str(prepared.directory),
                'prepared_sha256':state['prepared_sha256'],
            },previous_sha256=previous_operation_sha)
            return cls(deployment, prepared, state)

    @classmethod
    def resume(cls, deployment, prepared):
        state = _read(prepared.directory / "upgrade-state.json")
        if state["project"] != deployment.plan.name or state["id"] != prepared.receipt["id"] or state["project_directory"] != str(deployment.docker.directory):
            _fail("upgrade_identity_changed", "upgrade state belongs to another prepared deployment")
        if state["prepared_sha256"] != backup.digest(prepared.directory / "prepared.json")[1]:
            _fail("prepared_receipt_changed", "the prepared image/source receipt changed after upgrade started")
        value = cls(deployment, prepared, state)
        value._reconcile_helpers()
        value._roots()
        return value

    @classmethod
    def load(cls, directory, *, verify=sources.verify_stage, acceptance=False, check_images=True):
        """Recover an indexed transaction without discovering partial live labels."""
        directory = backup.absolute(directory)
        reference = registry.operation(directory)
        if reference is None:
            _fail('upgrade_not_found','this directory has no recorded host upgrade operation')
        with backup.operation_lock(reference['project']):
            if registry.operation(directory) != reference:
                _fail('upgrade_state_changed','the operation reference changed during recovery')
            prepared = preparation.Prepared.load(reference['prepared_directory'],verify=verify,
                acceptance=acceptance,check_images=check_images)
            state = _read(prepared.directory/'upgrade-state.json')
            receipt = prepared.receipt
            if (state.get('id') != reference['id'] or state.get('prepared_sha256') != reference['prepared_sha256']
                    or state.get('project_directory') != str(directory) or state.get('project') != reference['project']):
                _fail('upgrade_identity_changed','the persisted transaction differs from its host operation reference')
            if mapping.checksum(receipt['old_inputs']) != receipt['old_deployment']['input_sha256']:
                _fail('deployment_inputs_changed','the retained original input inventory changed')
            old = mapping.ComposeDocker(directory,[prepared.directory/'old-compose.json'],reference['project'],
                [Path(path) for path in receipt['old_env_files']],receipt['old_profiles'])
            old.audit_file = prepared.directory/'resume-original-inputs.jsonl'
            plan = backup.Plan.load(old)
            if (plan.name != reference['project'] or plan.engine != receipt['old_deployment']['database']
                    or set(plan.resources) != set(state['roots'])):
                _fail('upgrade_identity_changed','the retained original Compose no longer maps the original resources')
            for service,definition in plan.config['services'].items():
                if definition.get('image') != receipt['old_deployment']['services'][service]['image_id']:
                    _fail('upgrade_identity_changed','the original recovery Compose changed an image identity')
            deployment = mapping.Deployment(old,plan,receipt['old_inputs'],receipt['old_deployment']['services'],
                receipt['old_deployment']['role'],[Path(path) for path in receipt['source_files']],
                [Path(path) for path in receipt['source_env_files']],receipt['source_inputs'])
            preparation.host_records_outside_data(deployment)
            return cls.resume(deployment,prepared)

    @classmethod
    def advance(cls, directory, *, verify=sources.verify_stage, acceptance=False, execute=preparation._command):
        """Resume the same indexed operation, reloading between persisted phases."""
        while True:
            operation = cls.load(directory,verify=verify,acceptance=acceptance)
            phase = operation.state['phase']
            if phase == 'aborted':
                return copy.deepcopy(operation.state['abort_result'])
            if phase in {'images_prepared','fencing','fenced','stopping','cold','backing_up'}:
                operation.capture()
            elif phase in {'backed_up','database_inspecting','database_preparing'}:
                operation.prepare_database(verify=verify,execute=execute)
            elif phase in {'database_prepared','activating'}:
                operation.activate_fenced(verify=verify,execute=execute)
            elif phase in {'active_fenced','opening_stopping','opening_stopped','opening','opened'}:
                return operation.open(verify=verify,execute=execute)
            elif phase in {'recovering','restored_fenced','restored_verifying','restored_verified','restored_opening','restored_opened'}:
                from scripts.operator_recovery import load_recovery, PREPARING
                recovery = load_recovery(operation)
                if operation.state['recovery']['stage'] in PREPARING:
                    recovery.prepare()
                return recovery.open(execute=execute)
            else:
                _fail('upgrade_recovery_in_progress','this operation selected controlled recovery; forward upgrade is refused')

    def _roots(self):
        for key, value in self.deployment.plan.resources.items():
            if _identity(value.root) != self.state["roots"].get(key):
                _fail("upgrade_root_changed", "persistent root identity changed; retained material is not discarded")

    def _save(self, phase, **values):
        if phase not in PHASES:
            _fail("upgrade_phase_invalid", "unknown upgrade phase")
        actual = _read(self.path)
        if actual != self.state:
            _fail("upgrade_state_changed", "another operator revision changed this upgrade")
        result = _sealed({**self.state, **copy.deepcopy(values), "phase": phase, "revision": self.state["revision"] + 1})
        temporary = self.path.parent / (".upgrade-state-" + uuid.uuid4().hex + ".tmp")
        backup.private_write(temporary, backup.json_bytes(result))
        os.replace(temporary, self.path)
        backup.fsync_directory(self.path.parent)
        self.state = result

    def _content(self):
        return backup.json_bytes({"version": 1, "owner": "frontiercloud-python-operator", "id": self.state["id"],
            "project": self.state["project"], "prepared_sha256": self.state["prepared_sha256"]}) + b"\n"

    def _marker(self, path):
        info = backup.absolute(path).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink not in {1, 2} or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid not in {0, 10001}:
            _fail("maintenance_identity_changed", "operator fence has unknown type, owner, permissions or links")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            content = stream.read(4097)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or content != self._content():
            _fail("maintenance_identity_changed", "operator fence content/identity differs; it is preserved")
        return info

    def _fence(self):
        self._roots()
        marker = self.root / HARD
        if self.state["phase"] == "images_prepared":
            if any((self.root / name).exists() or (self.root / name).is_symlink() for name in (HARD, MANUAL)):
                _fail("maintenance_already_owned", "another maintenance fence appeared before upgrade fencing")
            self._save("fencing", fence_temp=".frontiercloud-fence-" + self.state["id"] + "-" + uuid.uuid4().hex)
        temporary = self.root / self.state["fence_temp"]
        if marker.exists() or marker.is_symlink():
            current = self._marker(marker)
            if temporary.exists() or temporary.is_symlink():
                other = self._marker(temporary)
                if (other.st_dev, other.st_ino) != (current.st_dev, current.st_ino) or current.st_nlink != 2:
                    _fail("maintenance_identity_changed", "operator fence material does not identify its own publication")
                temporary.unlink()
            elif current.st_nlink != 1:
                _fail("maintenance_identity_changed", "operator fence has an unknown extra hard link")
        else:
            if temporary.exists() or temporary.is_symlink():
                try:
                    self._marker(temporary)
                except mapping.DeploymentError:
                    # Preserve interrupted material, never truncate/overwrite it.
                    retained = self.state["retained_fence_material"] + [str(temporary)]
                    if len(retained) > 3:
                        _fail("maintenance_material_incomplete", "repeated incomplete fence material requires inspection")
                    self._save("fencing", retained_fence_material=retained,
                               fence_temp=".frontiercloud-fence-" + self.state["id"] + "-" + uuid.uuid4().hex)
                    temporary = self.root / self.state["fence_temp"]
            if not temporary.exists():
                backup.private_write(temporary, self._content())
            info = self._marker(temporary)
            if info.st_nlink != 1:
                _fail("maintenance_identity_changed", "unpublished fence material has an unexpected link")
            os.chown(temporary, 10001, 10001, follow_symlinks=False)
            descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.link(temporary, marker, follow_symlinks=False)
            backup.fsync_directory(self.root)
            if (self._marker(temporary).st_ino, self._marker(marker).st_ino) != (info.st_ino, info.st_ino):
                _fail("maintenance_identity_changed", "operator fence changed while being published")
            temporary.unlink()
        backup.fsync_directory(self.root)
        current = self._marker(marker)
        self._save("fenced", fence_identity=_fence_identity(current),
                   manual_after_fencing=(self.root / MANUAL).exists())

    def _owned_fence(self):
        self._roots()
        current = self._marker(self.root / HARD)
        if current.st_nlink != 1 or self.state.get("fence_identity") != _fence_identity(current):
            _fail("maintenance_identity_changed", "the current fence is not this upgrade's published inode")

    def capture(self):
        """Join all old writers and verify a state-only snapshot, remaining cold."""
        with backup.operation_lock(self.state["project"]):
            actual = _read(self.path)
            if actual != self.state:
                _fail("upgrade_state_changed", "reload the current persisted upgrade before continuing")
            marker = self.root / HARD
            if self.state['phase'] in {'images_prepared', 'fencing'} and not (marker.exists() or marker.is_symlink()):
                self._save(self.state['phase'], preflight=self._live_preflight())
            if self.state["phase"] in {"images_prepared", "fencing"}:
                self._fence()
            self._owned_fence()
            if self.state["phase"] == "fenced":
                self._save("stopping")
            if self.state["phase"] == "stopping":
                self.deployment.plan.docker.stop()
                self.deployment.plan.assert_cold()
                self._owned_fence()
                self._save("cold")
            self.deployment.plan.assert_cold()
            if self.state["phase"] == "cold":
                self._save("backing_up", snapshot="snapshot-" + uuid.uuid4().hex)
            if self.state["phase"] == "backing_up":
                snapshot = self.prepared.directory / self.state["snapshot"]
                if snapshot.exists() and not (snapshot / "manifest.json").exists():
                    retained = self.state["retained_snapshots"] + [self.state["snapshot"]]
                    if len(retained) > 3:
                        _fail("upgrade_snapshot_incomplete", "repeated incomplete snapshots remain retained for inspection")
                    self._save("backing_up", retained_snapshots=retained, snapshot="snapshot-" + uuid.uuid4().hex)
                    snapshot = self.prepared.directory / self.state["snapshot"]
                if not snapshot.exists():
                    backup.backup(self.deployment.plan, snapshot)
                manifest = backup.verify(snapshot)
                if manifest["project"] != self.state["project"] or manifest["database_engine"] != self.deployment.plan.engine or manifest["images"] != self.deployment.plan.images:
                    _fail("upgrade_snapshot_binding_failed", "cold snapshot does not bind the selected original deployment")
                self._owned_fence()
                self._save("backed_up", snapshot_manifest_sha256=backup.digest(snapshot / "manifest.json")[1])
            if self.state["phase"] != "backed_up":
                _fail("upgrade_phase_invalid", "cold capture cannot execute this phase")
            snapshot = self.prepared.directory / self.state["snapshot"]
            manifest = backup.verify(snapshot)
            if backup.digest(snapshot / "manifest.json")[1] != self.state["snapshot_manifest_sha256"]:
                _fail("upgrade_snapshot_changed", "verified snapshot changed after its completion record")
            return {"phase": "backed_up", "project": self.state["project"], "snapshot": str(snapshot),
                    "database": manifest["database_engine"], "media_included": False, "services_started": False,
                    "maintenance_owned": True, "upgrade_complete": False}

    def _prepared_inputs(self, verify, *, snapshot=True):
        self._reconcile_helpers()
        _private(self.prepared.directory / "prepared.json")
        if backup.digest(self.prepared.directory / "prepared.json")[1] != self.state["prepared_sha256"]:
            _fail("prepared_receipt_changed", "prepared source/images receipt changed after capture")
        receipt = backup.bounded_json(self.prepared.directory / "prepared.json")
        if receipt != self.prepared.receipt or mapping.checksum(self.prepared.config) != receipt["configuration_sha256"]:
            _fail("prepared_configuration_changed", "prepared configuration differs from the retained receipt")
        inputs = self.deployment.inputs
        if (mapping.checksum(inputs) != receipt["old_deployment"]["input_sha256"]
                or mapping._inputs([Path(row["path"]) for row in inputs]) != inputs):
            _fail("deployment_inputs_changed", "original Compose/private inputs changed after capture")
        implicit = self.deployment.docker.directory / ".env"
        if (not self.deployment.docker.env_files and str(implicit) not in {row["path"] for row in inputs}
                and (implicit.exists() or implicit.is_symlink())):
            _fail("deployment_inputs_changed", "a new implicit .env appeared after capture")
        for filename, key in (("compose.json", "compose_sha256"), ("old-compose.json", "old_compose_sha256")):
            if backup.digest(self.prepared.directory / filename)[1] != receipt[key]:
                _fail("prepared_compose_changed", "prepared Compose inputs changed after capture")
        if verify(Path(receipt["stage_directory"]))["stage_sha256"] != receipt["source_stage_sha256"]:
            _fail("source_stage_changed", "candidate source changed after capture")
        if snapshot:
            directory = self.prepared.directory / self.state["snapshot"]
            backup.verify(directory)
            if backup.digest(directory / "manifest.json")[1] != self.state["snapshot_manifest_sha256"]:
                _fail("upgrade_snapshot_changed", "the retained cold recovery snapshot changed")

    def _reconcile_helpers(self):
        for path in sorted(self.prepared.directory.glob('*/helper-owner.json')):
            _private(path)
            owner = backup.bounded_json(path)
            try:
                valid_token = uuid.UUID(hex=owner['token']).hex == owner['token']
            except (KeyError, TypeError, ValueError, AttributeError):
                valid_token = False
            if (not isinstance(owner,dict) or not valid_token or owner.get('version') != 1 or owner.get('operation') != self.state['id']
                    or owner.get('project') != self.state['project'] or owner.get('name') != 'fc-op-helper-'+owner['token']
                    or owner.get('image') != self.prepared.receipt['images']['web']['id']):
                _fail('helper_identity_changed','retained helper ownership record is invalid')
            _join_helper(self.prepared.docker,owner)

    def _database_helper(self, command, attempt, *, baseline=None, execute=preparation._command):
        config = copy.deepcopy(self.prepared.config)
        web = config["services"]["web"]
        web["user"] = "10001:10001"
        if command in {'preflight','identity-original','verify','probe-fenced','probe-open','probe-restored'}:
            previous = self.deployment.plan.config
            web['volumes'] = copy.deepcopy(previous['services']['web'].get('volumes', []))
            for mount in web['volumes']:
                mount['read_only'] = True
            used = {row['source'] for row in web['volumes'] if row['type'] == 'volume'}
            web['read_only'] = True
            for key in ('ports', 'container_name', 'depends_on', 'restart', 'healthcheck'):
                web.pop(key, None)
            config = {'name': self.state['project'], 'services': {'web': web},
                'volumes': {key: copy.deepcopy(previous['volumes'][key]) for key in used}}
            if 'networks' in previous:
                config['networks'] = copy.deepcopy(previous['networks'])
        if baseline:
            web.setdefault("volumes", []).append({"type": "bind", "source": str(baseline),
                "target": "/run/frontiercloud-upgrade-baseline.json", "read_only": True})
        path = attempt / "helper-compose.json"
        backup.private_write(path, backup.json_bytes(backup.compose_values(config, encode=True)))
        docker = mapping.ComposeDocker(self.deployment.docker.directory, [path], self.state["project"], [])
        docker.audit_file = attempt/'docker-inputs.jsonl'
        token = uuid.uuid4().hex
        owner = {'version':1,'operation':self.state['id'],'project':self.state['project'],'token':token,
            'name':'fc-op-helper-'+token,'image':web['image']}
        backup.private_write(attempt/'helper-owner.json',backup.json_bytes(owner))
        backup.fsync_directory(attempt)
        arguments = docker.base + ["run", "--rm", "--name", owner['name'], '--label', HELPER_LABEL+'='+token,
            "--no-deps", "-T", "--pull", "never", "--entrypoint", "python", "web",
            "-m", "app.services.offline_upgrade", command]
        if command not in {'preflight','identity-original'}:
            arguments += ['--fence-sha256', hashlib.sha256(self._content()).hexdigest()]
        if baseline:
            arguments += ["--node-id", self.state["database_node_id"], "--baseline", "/run/frontiercloud-upgrade-baseline.json",
                          "--baseline-sha256", self.state["database_baseline_sha256"]]
        caught = None
        try:
            execute(docker, arguments, attempt, "database-" + command)
        except mapping.DeploymentError as error:
            caught = error
        finally:
            _join_helper(docker,owner)
        log = attempt / ("database-" + command + ".log")
        try:
            if log.stat().st_size > 16 * 1024 * 1024:
                _fail("database_helper_output_invalid", "database helper exceeded its bounded result size")
            results = []
            for line in log.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict) and (value.get("version") == 1 or value.get("phase") in {'database_prepared', 'fenced_verified'}
                        or set(value) == {"error", "detail"}):
                    results.append(value)
            if len(results) != 1:
                if caught and not results:
                    raise caught
                _fail("database_helper_output_invalid", "database helper returned an absent or ambiguous result")
            result = results[0]
        except (OSError, ValueError, IndexError):
            if caught:
                raise caught
            _fail("database_helper_output_invalid", "database helper returned no complete result; private diagnostics retained")
        if not isinstance(result, dict):
            _fail("database_helper_output_invalid", "database helper returned an invalid result")
        if caught:
            # The one-shot helper owns these bounded error classifications;
            # keep the raw subprocess diagnostics in the private attempt log.
            allowed = {"unfinished_transactions", "unfinished_publication", "original_state_changed", "media_key_invalid",
                "identity_key_invalid", "node_identity_changed", "maintenance_owner_changed", "baseline_changed",
                "database_uninitialized", "schema_generation_unsupported", "original_table_missing", "original_column_missing",
                "schema_marker_invalid", "migration_history_ahead", "node_identity_invalid", "database_missing",
                "database_path_unsafe", "unsafe_input", "unsupported_database_value", "schema_prepare_incomplete", "database_not_ready",
                'configuration_invalid', 'configuration_role_mismatch', 'database_authentication_failed', 'database_access_denied',
                'database_lock_timeout', 'database_deadlock', 'database_unreachable', 'database_disconnected', 'database_driver_failed'}
            allowed |= {'probe_response_too_large','probe_certificate_expired','probe_certificate_hostname',
                'probe_certificate_untrusted','probe_certificate_invalid','probe_tls_failed','probe_dns_failed',
                'probe_timeout','probe_connection_refused','probe_connection_failed','probe_response_invalid',
                'probe_not_ready','probe_identity_unavailable','probe_identity_mismatch','probe_business_gate_mismatch',
                'maintenance_not_open'}
            if result.get("error") in allowed:
                _fail(result["error"], "offline database preparation refused; inspect the retained private attempt log")
            raise caught
        if "error" in result:
            _fail("database_helper_output_invalid", "database helper reported failure without a failed exit status")
        return result

    def _live_preflight(self):
        """No writer fence, new persistent/temporary volume or exposed socket."""
        self._reconcile_helpers()
        current = mapping.Deployment.discover(self.deployment.docker.directory)
        if current.summary() != self.state['old_deployment'] or current.inputs != self.deployment.inputs:
            _fail('deployment_inputs_changed', 'old deployment changed before the read-only preflight')
        attempt = self.prepared.directory / ('preflight-' + uuid.uuid4().hex)
        attempt.mkdir(mode=0o700)
        backup.fsync_directory(attempt.parent)
        result = self._database_helper('preflight', attempt)
        if (result.get('phase') != 'preflight_passed' or result.get('readonly') is not True
                or result.get('database') != self.deployment.plan.engine or not result.get('node_id') or not result.get('keys')):
            _fail('preflight_output_invalid', 'read-only preflight did not prove the existing deployment')
        previous = self.state.get('preflight')
        if previous and result != previous:
            _fail('preflight_identity_changed', 'database generation, identity or keys changed before fencing')
        current = mapping.Deployment.discover(self.deployment.docker.directory)
        if current.summary() != self.state['old_deployment'] or current.inputs != self.deployment.inputs:
            _fail('deployment_inputs_changed', 'old deployment changed during the read-only preflight')
        return result

    def prepare_database(self, *, verify=sources.verify_stage, execute=preparation._command):
        """Use only the pinned candidate helper, retaining the owned hard fence.

        MySQL may run alone; no business Web, Redis or agent is started. The
        baseline is durable before the first DDL command, including on retries.
        """
        with backup.operation_lock(self.state["project"]):
            if _read(self.path) != self.state:
                _fail("upgrade_state_changed", "reload the current persisted upgrade before continuing")
            self._owned_fence()
            if self.state["phase"] not in {"backed_up", "database_inspecting", "database_preparing", "database_prepared"}:
                _fail("upgrade_phase_invalid", "database preparation requires a completed cold snapshot")
            self._prepared_inputs(verify)
            allowed = frozenset({"mysql"}) if self.deployment.plan.engine == "mysql" else frozenset()
            self.deployment.plan.assert_cold(allow_services=allowed)
            if self.state["phase"] == "database_prepared":
                return copy.deepcopy(self.state["database_result"])
            attempt = self.prepared.directory / ("database-attempt-" + uuid.uuid4().hex)
            attempt.mkdir(mode=0o700)
            backup.fsync_directory(attempt.parent)
            if allowed:
                docker = mapping.ComposeDocker(self.deployment.docker.directory,
                    [self.prepared.directory / "old-compose.json"], self.state["project"], [])
                execute(docker, docker.base + ["up", "-d", "--wait", "--wait-timeout", "90", "--no-deps",
                    "--no-build", "--pull", "never", "mysql"], attempt, "database-start", timeout=120)
            if self.state["phase"] == "backed_up":
                self._save("database_inspecting")
            if self.state["phase"] == "database_inspecting":
                result = self._database_helper("inspect", attempt, execute=execute)
                if (result.get("version") != 1 or result.get("database") != self.deployment.plan.engine
                        or not isinstance(result.get("tables"), dict) or not result.get("node_id") or not result.get("keys")):
                    _fail("database_helper_output_invalid", "cold inspection did not bind the original database and identity")
                if any(result.get(key) != self.state['preflight'].get(key)
                        for key in ('node_id', 'role', 'endpoint', 'keys', 'public_key_sha256', 'generation')):
                    _fail('preflight_identity_changed', 'cold database identity/keys/generation differ from the live preflight')
                baseline = attempt / "baseline.json"
                backup.private_write(baseline, backup.json_bytes(result))
                os.chown(baseline, 10001, 10001, follow_symlinks=False)
                backup.fsync_directory(attempt)
                self._save("database_preparing", database_baseline=str(baseline),
                    database_baseline_sha256=backup.digest(baseline)[1], database_node_id=result["node_id"],
                    database_from_generation=result["generation"])
                # Keep separate complete input/result records for both helpers.
                attempt = self.prepared.directory / ("database-attempt-" + uuid.uuid4().hex)
                attempt.mkdir(mode=0o700)
                backup.fsync_directory(attempt.parent)
            baseline = backup.absolute(self.state["database_baseline"])
            if baseline.parent.parent != self.prepared.directory or backup.digest(baseline)[1] != self.state["database_baseline_sha256"]:
                _fail("baseline_changed", "original database baseline changed; no schema preparation is permitted")
            self._owned_fence()
            self.deployment.plan.assert_cold(allow_services=allowed)
            result = self._database_helper("prepare", attempt, baseline=baseline, execute=execute)
            if (result.get("phase") != "database_prepared" or result.get("node_id") != self.state["database_node_id"]
                    or result.get("baseline_sha256") != self.state["database_baseline_sha256"]
                    or result.get("from_generation") != self.state["database_from_generation"]
                    or result.get("original_state_preserved") is not True or result.get("business_started") is not False):
                _fail("database_helper_output_invalid", "database preparation result does not match its durable original baseline")
            self._owned_fence()
            self.deployment.plan.assert_cold(allow_services=allowed)
            self._save("database_prepared", database_result=result)
            return copy.deepcopy(result)

    def activate_fenced(self, *, verify=sources.verify_stage, execute=preparation._command):
        """Start the complete pinned target package; business remains fenced."""
        with backup.operation_lock(self.state['project']):
            if _read(self.path) != self.state:
                _fail('upgrade_state_changed', 'reload the current persisted upgrade before activating')
            if self.state['phase'] not in {'database_prepared', 'activating', 'active_fenced'}:
                _fail('upgrade_phase_invalid', 'target activation requires the prepared database')
            self._owned_fence()
            self._prepared_inputs(verify)
            if self.state['phase'] == 'database_prepared':
                allowed = frozenset({'mysql'}) if self.deployment.plan.engine == 'mysql' else frozenset()
                self.deployment.plan.assert_cold(allow_services=allowed)
                self._save('activating')
            else:
                # Reconciliation may find a partly started target. Preserve
                # the original roots and refuse foreign mounted writers.
                target = backup.Plan.load(self.prepared.docker)
                target.assert_cold(allow_project=True)
            attempt = self.prepared.directory / ('activation-'+uuid.uuid4().hex)
            attempt.mkdir(mode=0o700)
            backup.fsync_directory(attempt.parent)
            if self.state['phase'] != 'active_fenced':
                execute(self.prepared.docker, self.prepared.docker.base+['up','-d','--force-recreate','--no-build','--pull','never',
                    '--wait','--wait-timeout','120'], attempt, 'target-start', timeout=150)
            current = mapping.Deployment.discover(self.deployment.docker.directory, prepared=self.prepared)
            if set(current.plan.resources) != set(self.state['roots']):
                _fail('activation_resources_changed', 'activation changed persistent resource topology')
            for key, resource in current.plan.resources.items():
                if _identity(resource.root) != self.state['roots'][key]:
                    _fail('activation_resources_changed', 'activation changed an original persistent root')
            for service, expected in self.prepared.receipt['images'].items():
                if current.observed[service]['image_id'] != expected['id']:
                    _fail('activation_image_changed', 'running target image differs from the pinned package')
            baseline = backup.absolute(self.state['database_baseline'])
            if backup.digest(baseline)[1] != self.state['database_baseline_sha256']:
                _fail('baseline_changed', 'the original database baseline changed before activation verification')
            result = self._database_helper('verify', attempt, baseline=baseline, execute=execute)
            if (result.get('phase') != 'fenced_verified' or result.get('node_id') != self.state['database_node_id']
                    or result.get('baseline_sha256') != self.state['database_baseline_sha256']
                    or result.get('original_state_preserved') is not True or result.get('business_opened') is not False):
                _fail('activation_verification_failed', 'live target verification did not match the retained original state')
            self._owned_fence()
            observed = mapping.Deployment.discover(self.deployment.docker.directory, prepared=self.prepared)
            if observed.summary() != current.summary():
                _fail('activation_changed', 'target deployment changed during live verification')
            summary = {**result, 'phase':'active_fenced', 'project':self.state['project'],
                'services':observed.observed, 'business_opened':False, 'upgrade_complete':False}
            registered = self._publish_current('active_fenced',result['generation'])
            self._save('active_fenced', activation=summary,registry_sha256=registered)
            return copy.deepcopy(summary)

    def _publish_current(self, phase, generation):
        return registry.publish(self.deployment.docker.directory,{
            'version':1,'id':self.state['id'],'phase':phase,'project':self.state['project'],
            'project_directory':self.state['project_directory'],'prepared_directory':str(self.prepared.directory),
            'prepared_sha256':self.state['prepared_sha256'],'node_id':self.state['database_node_id'],
            'generation':generation,'roots':self.state['roots'],
        },previous_sha256=self.state.get('registry_sha256',self.state.get('previous_registry_sha256')))

    def _probe(self, fenced, execute):
        baseline = backup.absolute(self.state['database_baseline'])
        if backup.digest(baseline)[1] != self.state['database_baseline_sha256']:
            _fail('baseline_changed','the retained baseline changed before endpoint verification')
        attempt = self.prepared.directory/('endpoint-'+uuid.uuid4().hex)
        attempt.mkdir(mode=0o700)
        backup.fsync_directory(attempt.parent)
        proof = self._database_helper('probe-fenced' if fenced else 'probe-open',attempt,baseline=baseline,execute=execute)
        tls = str(self.prepared.config['services']['web'].get('environment',{}).get('TLS_ENABLED',False)).lower() in {'true','1','yes','on','y'}
        if (proof.get('phase') != ('endpoint_fenced' if fenced else 'endpoint_open')
                or proof.get('node_id') != self.state['database_node_id']
                or proof.get('generation') != self.state['activation']['generation']
                or proof.get('baseline_sha256') != self.state['database_baseline_sha256']
                or proof.get('business_opened') is not (not fenced) or proof.get('readiness_status') != 200
                or proof.get('tls_enabled') is not tls
                or fenced and proof.get('original_state_verified') is not True
                or tls and (proof.get('certificate_verified') is not True or proof.get('signed_identity_verified') is not True)):
            _fail('endpoint_verification_failed','the endpoint proof does not match this prepared deployment')
        return proof

    def open(self, *, verify=sources.verify_stage, execute=preparation._command):
        """Join fenced Web before unlink; never restore old state after this boundary."""
        with backup.operation_lock(self.state['project']):
            if _read(self.path) != self.state:
                _fail('upgrade_state_changed','reload the persisted upgrade before opening')
            if self.state['phase'] == 'opened':
                return copy.deepcopy(self.state['open_result'])
            if self.state['phase'] not in {'active_fenced','opening_stopping','opening_stopped','opening'}:
                _fail('upgrade_phase_invalid','opening requires verified fenced activation')
            self._prepared_inputs(verify)
            self._roots()
            if (self.root/MANUAL).exists() or (self.root/MANUAL).is_symlink():
                _fail('manual_maintenance_active','manual maintenance is preserved; opening is refused')
            attempt = self.prepared.directory/('opening-'+uuid.uuid4().hex)
            attempt.mkdir(mode=0o700)
            backup.fsync_directory(attempt.parent)
            services = ['web']+(['nginx'] if 'nginx' in self.prepared.config['services'] else [])
            allowed = frozenset(self.prepared.config['services'])-frozenset(services)
            if self.state['phase'] == 'active_fenced':
                self._owned_fence()
                proof = self._probe(True,execute)
                self._owned_fence()
                self._save('opening_stopping',fenced_endpoint=proof)
            if self.state['phase'] == 'opening_stopping':
                self._owned_fence()
                execute(self.prepared.docker,self.prepared.docker.base+['stop','--timeout','30',*services],
                    attempt,'business-stop',timeout=90)
                backup.Plan.load(self.prepared.docker).assert_cold(allow_services=allowed)
                self._owned_fence()
                self._save('opening_stopped')
            if self.state['phase'] == 'opening_stopped':
                self._owned_fence()
                registered = self._publish_current('opening',self.state['activation']['generation'])
                # Persist before unlink. Even a lost reply can no longer
                # authorize overwriting any possibly newer business writes.
                self._save('opening',registry_sha256=registered,business_may_have_written=True)
            marker = self.root/HARD
            if marker.exists() or marker.is_symlink():
                self._owned_fence()
                # A resumed operation may find its fenced Web restarted by
                # Docker. Join it again before the one-way unlink boundary.
                stop = self.prepared.directory/('opening-stop-'+uuid.uuid4().hex)
                stop.mkdir(mode=0o700)
                execute(self.prepared.docker,self.prepared.docker.base+['stop','--timeout','30',*services],
                    stop,'business-stop',timeout=90)
                backup.Plan.load(self.prepared.docker).assert_cold(allow_services=allowed)
                self._owned_fence()
                if (self.root/MANUAL).exists() or (self.root/MANUAL).is_symlink():
                    _fail('manual_maintenance_active','manual maintenance appeared; the owned fence remains')
                marker.unlink()
                backup.fsync_directory(self.root)
            execute(self.prepared.docker,self.prepared.docker.base+['up','-d','--no-deps','--no-build','--pull','never',
                '--wait','--wait-timeout','120',*services],attempt,'business-start',timeout=150)
            current = mapping.Deployment.discover(self.deployment.docker.directory)
            if (current.operator_phase not in {'opening','ready'}
                    or mapping.checksum(current.plan.config) != self.prepared.receipt['configuration_sha256']):
                _fail('opened_deployment_changed','the started deployment differs from its opening receipt')
            proof = self._probe(False,execute)
            self._roots()
            if mapping.Deployment.discover(self.deployment.docker.directory).summary() != current.summary():
                _fail('opened_deployment_changed','the deployment changed during endpoint verification')
            registered = self._publish_current('ready',proof['generation'])
            result = {**proof,'phase':'opened','project':self.state['project'],
                'revision':self.prepared.receipt['provenance']['revision'],'acceptance_only':self.state['acceptance_only'],
                'services':current.observed,'upgrade_complete':True}
            self._save('opened',registry_sha256=registered,open_result=result)
            return copy.deepcopy(result)
