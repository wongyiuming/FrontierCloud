"""Prepare exact source/images and Compose inputs before any business fencing."""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import uuid

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_source as sources
from scripts import operator_registry as registry


APP_SERVICES = frozenset({"web", "secrets-init", "media-init", "certificate-init", "certificate-agent"})
ENDPOINT_FIELDS = ("container_name", "ports", "network_mode", "networks", "hostname")
FIXED_FIELDS = ("command", "entrypoint", "user", "read_only", "cap_drop", "cap_add", "security_opt", "sysctls", "tmpfs")


def _fail(code, message):
    mapping.fail(code, message)


def _private_directory(path):
    path = backup.absolute(path, exists=False)
    if path.exists() or not path.parent.is_dir():
        _fail("prepare_destination_exists", "preparation requires a new private directory")
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    backup.fsync_directory(path.parent)
    return path


def _store(path, value):
    backup.private_write(path, backup.json_bytes(value))
    backup.fsync_directory(path.parent)


def _same_deployment(expected, actual):
    return expected.summary() == actual.summary() and expected.inputs == actual.inputs


def host_records_outside_data(deployment):
    directory = deployment.docker.directory
    if any(resource.root and (resource.root == directory or resource.root in directory.parents)
           for resource in deployment.plan.resources.values()):
        _fail('operator_records_overlap_data','host operation records cannot be placed inside a mounted persistent data root')


def _old_files(deployment, source):
    files = []
    for number, path in enumerate(deployment.source_files or deployment.docker.files):
        if number == 0 or path == deployment.docker.directory / "docker-compose.mysql.yaml":
            path = source / path.name
        files.append(backup.absolute(path))
    return files


def _shape(deployment, candidate):
    """Preserve operator inputs while admitting only known native additions."""
    old = deployment.plan.config
    result = copy.deepcopy(candidate)
    if result.get("name") != old["name"]:
        _fail("candidate_identity_mismatch", "candidate changed the Compose project")
    for name, previous in old["services"].items():
        current = result.get("services", {}).get(name)
        if current is None:
            _fail("candidate_removes_service", "candidate removes existing service " + name)
        for key in FIXED_FIELDS:
            if previous.get(key) != current.get(key):
                _fail("unsupported_service_override", "service " + name + " requires an explicit mapping for " + key)
        environment = {**current.get("environment", {}), **previous.get("environment", {})}
        if environment:
            current["environment"] = environment
        else:
            current.pop("environment", None)
        for key in ENDPOINT_FIELDS:
            if key in previous:
                current[key] = copy.deepcopy(previous[key])
            else:
                current.pop(key, None)
        for key in ("cpus", "mem_limit", "mem_reservation", "pids_limit", "restart", "stop_grace_period", "logging"):
            if key in previous:
                current[key] = copy.deepcopy(previous[key])
        before = {row["target"]: row for row in previous.get("volumes", [])}
        after = {row["target"]: row for row in current.get("volumes", [])}
        for target, value in before.items():
            after[target] = copy.deepcopy(value)
        if after:
            current["volumes"] = list(after.values())
        else:
            current.pop("volumes", None)
    # Keep logical volume keys as well as actual names: Compose volume ownership
    # labels must continue to agree with cold recovery's original mapping.
    known = {target: row for name in old["services"] for target, row in mapping.mounts(old, name).items()}
    old_raw = {target: row for name in old["services"] for row in old["services"][name].get("volumes", []) for target in [row["target"]]}
    for name, definition in result["services"].items():
        for row in definition.get("volumes", []):
            if row["target"] in known and name not in old["services"]:
                previous = old_raw[row["target"]]
                row["type"], row["source"] = previous["type"], previous["source"]
    for key, value in old.get("volumes", {}).items():
        result.setdefault("volumes", {})[key] = copy.deepcopy(value)
    if set(result.get('networks', {})) - set(old.get('networks', {})):
        _fail('candidate_adds_network', 'a new network needs an explicit endpoint/recovery mapping')
    if old.get('networks'):
        result['networks'] = copy.deepcopy(old['networks'])
    return result


def _fresh_ephemeral_names(deployment, config, identifier):
    previous = {row['source'] for name in deployment.plan.config['services']
        for row in mapping.mounts(deployment.plan.config,name).values() if row['type'] == 'volume'}
    keys = {row['source'] for definition in config['services'].values() for row in definition.get('volumes',[])
        if row.get('type') == 'volume' and row.get('target') in backup.EPHEMERAL_TARGETS
        and config['volumes'][row['source']].get('name',config['name']+'_'+row['source']) not in previous}
    for key in keys:
        definition = config['volumes'][key]
        definition['name'] = config['name']+'_operator_'+identifier[:16]+'_'+mapping.checksum(key)[:12]
        definition.setdefault('labels',{})['io.frontiercloud.operator.ephemeral'] = identifier


def _new_volumes(deployment, config):
    old_names = {row["source"] for name in deployment.plan.config["services"]
                 for row in mapping.mounts(deployment.plan.config, name).values() if row["type"] == "volume"}
    added = {}
    for name, definition in config["services"].items():
        for row in definition.get("volumes", []):
            if row.get("type") != "volume":
                continue
            actual = config["volumes"][row["source"]].get("name", config["name"] + "_" + row["source"])
            if actual in old_names:
                continue
            if row["target"] not in backup.EPHEMERAL_TARGETS:
                _fail("candidate_adds_persistent_resource", "new persistent state requires a recovery mapping")
            if deployment.docker.volume(actual) is not None:
                _fail("candidate_ephemeral_volume_exists", "a new temporary volume name already contains unadmitted state")
            added[actual] = row["target"]
    return added


def _port_conflicts(deployment, config):
    candidate = []
    for name, definition in config["services"].items():
        for port, values in mapping._ports(definition).items():
            candidate.extend((name, port, value["HostIp"], value["HostPort"]) for value in values)
    def clash(left, right):
        return left[1] == right[1] and left[3] == right[3] and (left[2] == right[2] or left[2] in {"0.0.0.0", "::", ""} or right[2] in {"0.0.0.0", "::", ""})
    # Different target ports can still claim the same published socket.
    def socket_clash(left, right):
        return left[1].rsplit("/", 1)[1] == right[1].rsplit("/", 1)[1] and clash((left[0], "socket", *left[2:]), (right[0], "socket", *right[2:]))
    for index, value in enumerate(candidate):
        if any(socket_clash(value, other) for other in candidate[index + 1:]):
            _fail("candidate_port_conflict", "candidate services claim the same published socket")
    for container in deployment.docker.containers():
        labels = container.get("Config", {}).get("Labels") or {}
        if not container.get("State", {}).get("Running") or labels.get(mapping.LABEL + "project") == deployment.plan.name:
            continue
        for port, values in (container.get("HostConfig", {}).get("PortBindings") or {}).items():
            for value in values or []:
                foreign = ("foreign", port, value.get("HostIp", "0.0.0.0"), value["HostPort"])
                if any(socket_clash(item, foreign) for item in candidate):
                    _fail("candidate_port_conflict", "another running container owns a candidate published socket")
    owned = []
    for name, definition in deployment.plan.config["services"].items():
        if not getattr(deployment, "observed", {}).get(name, {}).get("running"):
            continue
        for port, values in mapping._ports(definition).items():
            owned.extend((name, port, value["HostIp"], value["HostPort"]) for value in values)
    for item in candidate:
        if any(socket_clash(item, old) for old in owned):
            continue
        protocol = item[1].rsplit("/", 1)[1]
        if protocol not in {"tcp", "udp"}:
            _fail("unsupported_ports", "only explicit TCP/UDP published ports are supported")
        family = socket.AF_INET6 if ":" in item[2] else socket.AF_INET
        kind = socket.SOCK_STREAM if protocol == "tcp" else socket.SOCK_DGRAM
        # Do not set SO_REUSEADDR/PORT: a listening host service outside Docker
        # must refuse preparation too. No listener is installed by this probe.
        try:
            with socket.socket(family, kind) as probe:
                probe.bind((item[2] or "0.0.0.0", int(item[3])))
        except OSError:
            _fail("candidate_host_port_unavailable", "a new published socket is unavailable on the local host")


def _command(docker, arguments, output, step, *, timeout=900):
    """Bounded child lifetime; full private diagnostics, exact argv, no secrets in status."""
    log = output / (step + ".log")
    _store(output / (step + "-input.json"), {"argv": arguments, "working_directory": str(docker.directory),
        "phase": "starting", "started_at": int(time.time()), "diagnostic_file": log.name})
    descriptor = os.open(log, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    started = int(time.time())
    with os.fdopen(descriptor, "wb") as stream:
        process = subprocess.Popen(arguments, cwd=docker.directory, env=docker.environment,
                                   stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        timed_out = False
        try:
            result = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                result = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                result = process.wait(timeout=5)
        stream.flush()
        os.fsync(stream.fileno())
    _store(output / (step + "-result.json"), {"argv": arguments, "working_directory": str(docker.directory),
        "started_at": started, "finished_at": int(time.time()), "exit_code": result, "timed_out": timed_out,
        "diagnostic_file": log.name, "diagnostic_sha256": backup.digest(log)[1]})
    if timed_out or result:
        _fail(step.replace("-", "_") + ("_timeout" if timed_out else "_failed"), step.replace("-", " ") + " failed; private diagnostics retained")


@dataclass
class Prepared:
    directory: Path
    docker: mapping.ComposeDocker
    config: dict
    receipt: dict

    @classmethod
    def load(cls, directory, *, verify=sources.verify_stage, acceptance=False, check_images=True, docker_factory=mapping.ComposeDocker):
        directory = backup.absolute(directory)
        info = directory.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            _fail('prepared_directory_unsafe','prepared state requires its private host-owned directory')
        receipt = registry.private_json(directory/'prepared.json')
        if (not isinstance(receipt,dict) or receipt.get('version') != 1 or receipt.get('phase') != 'images_prepared'
                or not isinstance(receipt.get('project'),str) or not backup.NAME.fullmatch(receipt['project'])
                or not isinstance(receipt.get('id'),str) or len(receipt['id']) != 32
                or any(character not in '0123456789abcdef' for character in receipt['id'])
                or not isinstance(receipt.get('provenance'),dict)
                or not all(key in receipt for key in ('project_directory','old_inputs','old_env_files','old_profiles'))):
            _fail('prepared_receipt_invalid','prepared source/image state is incomplete')
        public = receipt.get('provenance',{}).get('public_provenance_verified') is True
        if receipt.get('acceptance_only') is not (not public) or not public and not acceptance:
            _fail('source_not_published','local acceptance material cannot authorize a production resume')
        stage = backup.absolute(receipt['stage_directory'])
        if stage.stat().st_uid != os.geteuid() or stage.stat().st_mode & 0o077:
            _fail('source_stage_unsafe','resumed source requires its private host-owned staging directory')
        checked = verify(stage)
        if checked['stage_sha256'] != receipt['source_stage_sha256'] or checked['provenance'] != receipt['provenance']:
            _fail('source_stage_changed','retained source proof changed before resume')
        project_directory = backup.absolute(receipt['project_directory'])
        if backup.absolute(receipt['source_directory']) != stage/'source':
            _fail('source_stage_changed','the prepared source directory no longer belongs to its verified stage')
        for filename,key in [('compose.json','compose_sha256'),('old-compose.json','old_compose_sha256')]:
            registry.private_json(directory/filename)
            if backup.digest(directory/filename)[1] != receipt[key]:
                _fail('prepared_compose_changed','retained Compose changed before resume')
        docker = docker_factory(project_directory,[directory/'compose.json'],receipt['project'],[])
        docker.audit_file = directory/'resume-inputs.jsonl'
        docker.local_root()
        config = docker.config()
        if mapping.checksum(config) != receipt['configuration_sha256'] or config.get('name') != receipt['project']:
            _fail('prepared_configuration_changed','retained Compose no longer renders the prepared configuration')
        if mapping._role(config) != (receipt['old_deployment']['role'],receipt['old_deployment']['database']):
            _fail('prepared_configuration_changed','resumed Compose changed its original role or database')
        if set(config['services']) != set(receipt['images']):
            _fail('prepared_image_changed','prepared service image inventory is incomplete')
        for name,definition in config['services'].items():
            expected = receipt['images'][name]
            if (not isinstance(expected,dict) or not isinstance(expected.get('id'),str) or not mapping.IMAGE_ID.fullmatch(expected['id'])
                    or definition.get('image') != expected['id'] or definition.get('build') or definition.get('pull_policy') != 'never'):
                _fail('prepared_image_changed','resumed services must use only their pinned prepared images')
            if not check_images:
                continue
            image = docker.run(['docker','image','inspect',expected['id']],json_output=True)[0]
            if {'id':image['Id'],'os':image['Os'],'architecture':image['Architecture']} != expected:
                _fail('prepared_image_changed','a prepared image identity or platform changed')
            if name in APP_SERVICES | {'nginx'}:
                labels = image.get('Config',{}).get('Labels') or {}
                if (labels.get('io.frontiercloud.operator.id') != receipt['id']
                        or labels.get('io.frontiercloud.operator.source') != receipt['source_stage_sha256']):
                    _fail('candidate_image_proof_mismatch','a resumed image lost its operation/source binding')
        return cls(directory,docker,config,receipt)


def prepare(deployment, stage, output, *, verify=sources.verify_stage, acceptance=False, execute=_command):
    """Prepare only: no stop/up/run/create, no volume or business-data writes."""
    stage = backup.absolute(stage)
    host_records_outside_data(deployment)
    verified = verify(stage)
    if not isinstance(verified, dict) or not isinstance(verified.get("provenance"), dict):
        _fail("source_proof_missing", "candidate source proof is missing")
    provenance = verified["provenance"]
    if provenance.get("public_provenance_verified") is not True and not acceptance:
        _fail("source_not_published", "a production upgrade needs verified published Python source")
    source = backup.absolute(stage / "source")
    current = mapping.Deployment.discover(deployment.docker.directory)
    if not _same_deployment(deployment, current):
        _fail("deployment_inputs_changed", "the deployment changed since discovery")
    output = backup.absolute(output, exists=False)
    if any(root.root and (root.root == output or root.root in output.parents or output in root.root.parents)
           for root in deployment.plan.resources.values()):
        _fail("prepare_overlaps_data", "preparation cannot overlap persistent deployment data")
    files = _old_files(deployment, source)
    source_env = deployment.source_env_files if deployment.source_env_files is not None else deployment.docker.env_files
    renderer = mapping.ComposeDocker(deployment.docker.directory, files, deployment.plan.name,
                                     source_env, deployment.docker.profiles)
    config = _shape(deployment, renderer.config())
    for name, row in config["services"].items():
        if name in deployment.observed and name not in APP_SERVICES | {"nginx"}:
            row["image"] = deployment.observed[name]["image_id"]
        row.pop("profiles", None)
    # The rendered engine image may be a moved tag. Activation uses the exact
    # existing ID, without pulling/re-resolving any database/cache engine.
    identifier = uuid.uuid4().hex
    _fresh_ephemeral_names(deployment,config,identifier)
    admission = mapping.compare_candidate(deployment,config)
    added_volumes = _new_volumes(deployment, config)
    _port_conflicts(deployment, config)
    output = _private_directory(output)
    prefix = "frontiercloud-python-op-" + hashlib.sha256((str(deployment.docker.directory) + "\0" + deployment.plan.name).encode()).hexdigest()[:12] + "-" + identifier[:12]
    builders = []
    for name, row in config["services"].items():
        if name in APP_SERVICES | {"nginx"}:
            flavor = "nginx" if name == "nginx" else "web"
            row["image"] = prefix + "-" + flavor
            row.pop("build", None)
            if name in {"web", "nginx"}:
                row["build"] = {"context": str(source), "dockerfile": "nginx/Dockerfile" if flavor == "nginx" else "Dockerfile",
                    "labels": {"io.frontiercloud.operator.id": identifier, "io.frontiercloud.operator.source": verified["stage_sha256"],
                               "org.opencontainers.image.revision": provenance["revision"]}}
                builders.append(name)
    old_config = copy.deepcopy(deployment.plan.config)
    for name, definition in old_config["services"].items():
        definition["image"] = deployment.observed[name]["image_id"]
        definition.pop("build", None)
        definition["pull_policy"] = "never"
        definition.pop("profiles", None)
    _store(output / "old-compose.json", backup.compose_values(old_config, encode=True))
    _store(output / "build-compose.json", backup.compose_values(config, encode=True))
    build = mapping.ComposeDocker(deployment.docker.directory, [output / "build-compose.json"], deployment.plan.name, [])
    if build.config() != config:
        _fail("compose_roundtrip_changed", "prepared Compose changed literal inputs while being rendered")
    _store(output / "prepare-intent.json", {"version": 1, "id": identifier, "phase": "preparing", "old_deployment": deployment.summary(),
        "source_stage_sha256": verified["stage_sha256"], "new_ephemeral_volumes": added_volumes,
        "acceptance_only": provenance.get("public_provenance_verified") is not True})
    execute(build, build.base + ["build", *builders], output, "image-build")
    images = {}
    expected_platform = deployment.plan.images["web"]
    for name, definition in config["services"].items():
        reference = definition["image"]
        if name not in deployment.observed and name not in APP_SERVICES | {"nginx"}:
            present = build.run(["docker", "image", "ls", "--no-trunc", "--quiet", reference])
            if not present:
                execute(build, ["docker", "pull", reference], output, "image-pull-" + name)
        value = build.run(["docker", "image", "inspect", reference], json_output=True)[0]
        if value.get("Os") != expected_platform["os"] or value.get("Architecture") != expected_platform["architecture"]:
            _fail("candidate_platform_mismatch", "prepared images do not match the current Linux architecture")
        if name in APP_SERVICES | {"nginx"}:
            labels = value.get("Config", {}).get("Labels") or {}
            if labels.get("io.frontiercloud.operator.id") != identifier or labels.get("io.frontiercloud.operator.source") != verified["stage_sha256"]:
                _fail("candidate_image_proof_mismatch", "a prepared application image lacks its exact source binding")
        images[name] = {"id": value["Id"], "os": value["Os"], "architecture": value["Architecture"]}
        definition["image"] = value["Id"]
        definition.pop("build", None)
        definition["pull_policy"] = "never"
    if verify(stage) != verified or not _same_deployment(deployment, mapping.Deployment.discover(deployment.docker.directory)):
        _fail("prepare_inputs_changed", "source or deployment changed while images were prepared")
    mapping.compare_candidate(deployment, config)
    _store(output / "compose.json", backup.compose_values(config, encode=True))
    target = mapping.ComposeDocker(deployment.docker.directory, [output / "compose.json"], deployment.plan.name, [])
    if target.config() != config:
        _fail("compose_roundtrip_changed", "activation Compose changed a literal input")
    receipt = {"version": 1, "id": identifier, "phase": "images_prepared", "project": deployment.plan.name,
        'project_directory':str(deployment.docker.directory),'old_inputs':deployment.inputs,
        'old_env_files':[str(path) for path in deployment.docker.env_files],'old_profiles':deployment.docker.profiles,
        "old_deployment": deployment.summary(), "source_stage_sha256": verified["stage_sha256"], "provenance": provenance,
        "stage_directory": str(stage), "source_directory": str(source),
        "images": images, "new_ephemeral_volumes": added_volumes, "added_services": admission["added_services"],
        "compose_sha256": backup.digest(output / "compose.json")[1], "old_compose_sha256": backup.digest(output / "old-compose.json")[1],
        "configuration_sha256": mapping.checksum(config),
        'source_files':[str(path) for path in deployment.source_files or deployment.docker.files],
        'source_env_files':[str(path) for path in source_env],
        'source_inputs':deployment.source_inputs if deployment.source_inputs is not None else deployment.inputs,
        "acceptance_only": provenance.get("public_provenance_verified") is not True,
        "business_stopped": False, "deployment_changed": False}
    _store(output / "prepared.json", receipt)
    return Prepared(output, target, config, receipt)
