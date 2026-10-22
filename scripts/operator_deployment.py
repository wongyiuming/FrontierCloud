"""Read-only Linux-host discovery for the Python Compose operator.

This module is never imported by Web. Discovery cannot stop/start containers,
create volumes, install packages or change deployment files.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

from scripts import operator_backup as backup


LABEL = "com.docker.compose."
BASE_FILES = frozenset({"docker-compose.yaml", "docker-compose.storage.yaml"})
DOCKER_ENV = frozenset({"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                        "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "XDG_RUNTIME_DIR"})
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")


class DeploymentError(backup.BackupError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fail(code, message):
    raise DeploymentError(code, message)


def checksum(value):
    return hashlib.sha256(backup.json_bytes(value)).hexdigest()


def _container_identity(container):
    # Docker builds Mounts from a map: response order can change between two
    # inspect calls even while every mount and the container remain unchanged.
    return {key: container.get(key) for key in ("Id", "Image", "Config", "HostConfig", "RestartCount")} | {
        "mounts": sorted(container.get("Mounts") or [], key=lambda row: row.get("Destination", "")),
        "running": container.get("State", {}).get("Running")}


def _inputs(paths):
    result = []
    for path in paths:
        checked = backup.absolute(path)
        size, digest = backup.digest(checked)
        if size > 16 * 1024 * 1024:
            fail("input_too_large", "a Compose input exceeds 16 MiB")
        result.append({"path": str(checked), "bytes": size, "sha256": digest})
    return result


def _label_paths(value):
    if not isinstance(value, str) or not value or any(not part for part in value.split(",")):
        fail("deployment_labels_invalid", "Compose configuration labels are incomplete")
    paths = [backup.absolute(part) for part in value.split(",")]
    if len(paths) != len(set(paths)):
        fail("deployment_labels_invalid", "Compose input labels contain duplicate paths")
    return paths


def _role(config):
    services = config.get("services", {})
    unknown = set(services) - backup.KNOWN_SERVICES
    if unknown:
        fail("unsupported_services", "unsupported Compose services: " + ", ".join(sorted(unknown)))
    if not {"web", "redis"}.issubset(services):
        fail("unsupported_services", "the deployment must contain Web and Redis services")
    environment = services["web"].get("environment", {})
    storage = environment.get("DEPLOYMENT_MODE") == "only_stroge"
    if storage == ("nginx" in services):
        fail("deployment_role_mismatch", "business and Storage Compose inputs cannot be combined")
    engine = environment.get("DB_TYPE", "sqlite")
    if engine not in {"sqlite", "mysql"} or storage and engine != "sqlite":
        fail("unsupported_database", "this deployment database is unsupported")
    if engine == "mysql" and "mysql" not in services:
        fail("database_service_missing", "MySQL is selected but its service is inactive")
    if engine == "sqlite" and "mysql" in services:
        fail("database_service_mismatch", "an active MySQL service does not match the Web database")
    return "storage" if storage else "business", engine


def mounts(config, service, *, existing=True):
    """Normalized effective mappings; never create or resolve a missing volume."""
    result = {}
    volumes = config.get("volumes", {})
    project = config["name"]
    for mount in config["services"][service].get("volumes", []):
        target = mount.get("target")
        if not isinstance(target, str) or not target.startswith("/") or target in result:
            fail("unsupported_mount", "a service has invalid or duplicate mount targets")
        kind, source = mount.get("type"), mount.get("source")
        if kind == "bind":
            if target in backup.EPHEMERAL_TARGETS:
                fail("unsupported_mount", "a host bind cannot be assumed to be disposable temporary state")
            source = str(backup.absolute(source,exists=existing))
        elif kind == "volume":
            definition = volumes.get(source)
            if not isinstance(definition, dict) or definition.get("external") or definition.get("driver", "local") != "local" or definition.get("driver_opts"):
                fail("unsupported_volume", "external or non-local volumes require an explicit recovery mapping")
            source = definition.get("name", project + "_" + source)
            if not isinstance(source, str) or not backup.NAME.fullmatch(source):
                fail("unsupported_volume", "a volume has an invalid local name")
        else:
            fail("unsupported_mount", "anonymous and special mounts require an explicit recovery mapping")
        if target not in backup.PERSISTENT_TARGETS and target not in backup.EPHEMERAL_TARGETS and not (
                service == "redis" and target == "/data" or service == "mysql" and target == "/var/lib/mysql"):
            fail("unsupported_mount", "service " + service + " mount " + target + " has no declared recovery mapping")
        result[target] = {"type": kind, "source": source, "read_only": bool(mount.get("read_only", False))}
    return result


def _ports(definition):
    result = {}
    for port in definition.get("ports", []):
        if not isinstance(port, dict) or type(port.get("target")) is not int or not str(port.get("published", "")).isdigit():
            fail("unsupported_ports", "automatic mapping requires explicit fixed published ports")
        key = str(port["target"]) + "/" + port.get("protocol", "tcp")
        result.setdefault(key, []).append({"HostIp": port.get("host_ip", "0.0.0.0"), "HostPort": str(port["published"])})
    return {key: sorted(values, key=backup.json_bytes) for key, values in result.items()}


class ComposeDocker(backup.Docker):
    """Pinned actual project/inputs, with a non-interpolating host environment."""
    def __init__(self, directory, files, name, env_files, profiles=()):
        super().__init__(directory, files)
        self.name = name
        self.files, self.env_files = list(files), list(env_files)
        self.profiles = list(profiles)
        self.base += ["--project-name", name]
        for path in env_files:
            self.base += ["--env-file", str(path)]
        for profile in profiles:
            self.base += ["--profile", profile]
        self.environment = {key: value for key, value in os.environ.items() if key in DOCKER_ENV}
        self.actual_images = None
        self.audit_file = None

    def _audit(self, value):
        if self.audit_file is None:
            return
        path = backup.absolute(self.audit_file, exists=False)
        created = not path.exists()
        descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(descriptor, "ab") as stream:
            info = os.fstat(stream.fileno())
            if info.st_nlink != 1 or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                fail("operator_audit_unsafe", "operator input record must be a private regular file")
            stream.write(backup.json_bytes({"version": 1, "utc_epoch": time.time(), **value}) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if created:
            backup.fsync_directory(path.parent)

    def run(self, arguments, *, json_output=False):
        invocation = uuid.uuid4().hex
        self._audit({"event": "input", "id": invocation, "argv": arguments, "working_directory": str(self.directory)})
        try:
            result = subprocess.run(arguments, cwd=self.directory, env=self.environment,
                                    capture_output=True, timeout=45, check=False)
        except subprocess.TimeoutExpired:
            self._audit({"event": "result", "id": invocation, "timeout": True, "exit_code": None})
            fail("docker_timeout", "the local Docker or Compose command timed out")
        except OSError:
            self._audit({"event": "result", "id": invocation, "start_failed": True, "exit_code": None})
            fail("docker_unavailable", "the local Docker CLI could not be executed")
        self._audit({"event": "result", "id": invocation, "exit_code": result.returncode,
                     "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(), "stderr_sha256": hashlib.sha256(result.stderr).hexdigest()})
        if result.returncode:
            # Expanded config/daemon stderr may contain credentials. Keep the
            # failing operation and exit status, never guess a cause from stderr.
            operation = 'docker_command_failed'
            if "config" in arguments and "compose" in arguments:
                operation = 'compose_config_failed'
            elif arguments[:2] == ['docker','inspect']:
                operation = 'docker_container_inspect_failed'
            elif arguments[:3] == ['docker','image','inspect']:
                operation = 'docker_image_inspect_failed'
            elif arguments[:3] == ['docker','volume','inspect']:
                operation = 'docker_volume_inspect_failed'
            fail(operation, "local Docker command failed (exit " + str(result.returncode) + ")")
        if json_output:
            try:
                return json.loads(result.stdout)
            except ValueError:
                fail("docker_invalid_response", "local Docker returned invalid JSON")
        return result.stdout.decode().strip()

    def config(self):
        # No --profile '*': the inactive MySQL definition is not a deployment.
        return backup.compose_values(self.run(self.base + ["config", "--format", "json"], json_output=True))

    def containers(self):
        for _attempt in range(3):
            identifiers = self.run(['docker','ps','-aq','--no-trunc']).splitlines()
            if not identifiers:
                return []
            if any(not re.fullmatch('[0-9a-f]{64}',value) for value in identifiers):
                fail('docker_invalid_response','Docker returned an invalid container inventory')
            try:
                return self.run(['docker','inspect',*identifiers],json_output=True)
            except DeploymentError as error:
                if error.code != 'docker_container_inspect_failed':
                    raise
                current = self.run(['docker','ps','-aq','--no-trunc']).splitlines()
                if set(current) == set(identifiers):
                    raise  # A stable inventory cannot explain away a real error.
                # A one-off may disappear after ps. Repeat a complete inventory;
                # never accept a partial inspect result or ignore a live writer.
        fail('docker_inventory_changing','Docker container inventory keeps changing; retry observation')

    def image_id(self, name):
        if self.actual_images is not None:
            if name not in self.actual_images:
                fail("image_mapping_missing", "a persistent service has no observed container image")
            name = self.actual_images[name]
        return super().image_id(name)


@dataclass
class Deployment:
    docker: ComposeDocker
    plan: backup.Plan
    inputs: list
    observed: dict
    role: str
    source_files: list | None = None
    source_env_files: list | None = None
    source_inputs: list | None = None
    operator_phase: str | None = None

    @classmethod
    def discover(cls, directory, *, docker_factory=ComposeDocker, prepared=None):
        directory = backup.absolute(directory)
        probe = docker_factory(directory, [], "discovery", [])
        # Validate the local daemon before trusting any daemon filesystem paths.
        probe.local_root()
        containers = probe.containers()
        matches = []
        for container in containers:
            labels = container.get("Config", {}).get("Labels") or {}
            if labels.get(LABEL + "project.working_dir") == str(directory) and labels.get(LABEL + "service") == "web":
                matches.append(container)
        if not matches:
            fail("deployment_not_found", "no Compose Web deployment belongs to this directory")
        if len(matches) != 1:
            fail("deployment_ambiguous", "multiple Compose Web containers match this directory")
        labels = matches[0]["Config"]["Labels"]
        name = labels.get(LABEL + "project", "")
        if not backup.NAME.fullmatch(name):
            fail("deployment_labels_invalid", "the Compose project label is invalid")
        files = _label_paths(labels.get(LABEL + "project.config_files"))
        current_receipt = None
        if prepared is None and (files[0].parent != directory or files[0].name not in BASE_FILES):
            from scripts import operator_registry as registry
            from types import SimpleNamespace
            current_receipt, retained = registry.read(directory,name)
            prepared = SimpleNamespace(directory=Path(current_receipt['prepared_directory']),receipt=retained)
        managed = prepared is not None
        if managed:
            import stat
            receipt_path = backup.absolute(prepared.directory / 'prepared.json')
            info = receipt_path.lstat()
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                fail('prepared_receipt_unsafe', 'prepared observation needs the private host-owned receipt')
            receipt = backup.bounded_json(receipt_path)
            if (receipt != prepared.receipt or name != receipt.get('project') or files != [prepared.directory/'compose.json']
                    or backup.digest(files[0])[1] != receipt.get('compose_sha256')):
                fail('prepared_observation_mismatch', 'running Compose does not match the retained prepared package')
        elif files[0].parent != directory or files[0].name not in BASE_FILES or any(path.name in BASE_FILES for path in files[1:]):
            fail("unsupported_compose_base", "use one independent documented business or Storage Compose base")
        env_label = labels.get(LABEL + "project.environment_file", "")
        env_files = _label_paths(env_label) if env_label else []
        # Compose reads .env implicitly only if explicit --env-file was absent.
        implicit = directory / ".env"
        all_inputs = files + (env_files if env_files else [implicit] if implicit.exists() or implicit.is_symlink() else [])
        before = _inputs(all_inputs)
        web_environment = dict(value.split("=", 1) for value in matches[0]["Config"].get("Env", []) if "=" in value)
        observed_mysql = any((row.get("Config", {}).get("Labels") or {}).get(LABEL + "project") == name and (
            row.get("Config", {}).get("Labels") or {}).get(LABEL + "service") == "mysql" for row in containers)
        # Older documented base files also allowed the named mysql profile.
        # Recover that selector only from an existing MySQL service AND Web's
        # actual engine. A dormant definition never enables a new database.
        profiles = ["mysql"] if observed_mysql and web_environment.get("DB_TYPE") == "mysql" else []
        docker = docker_factory(directory, files, name, env_files, profiles)
        config = docker.config()
        if managed and checksum(config) != receipt.get('configuration_sha256'):
            fail('prepared_observation_mismatch', 'running configuration changed after preparation')
        if config.get("name") != name:
            fail("deployment_labels_invalid", "rendered Compose project differs from its running labels")
        role, engine = _role(config)
        expected_base = "docker-compose.storage.yaml" if role == "storage" else "docker-compose.yaml"
        if not managed and files[0].name != expected_base:
            fail("deployment_role_mismatch", "Compose base does not match the effective role")
        own = [row for row in containers if (row.get("Config", {}).get("Labels") or {}).get(LABEL + "project") == name]
        observed, images, image_configs = {}, {}, {}
        for container in own:
            item = container.get("Config", {})
            row = item.get("Labels") or {}
            service = row.get(LABEL + "service")
            if service not in config["services"] or service in observed or row.get(LABEL + "oneoff", "False").lower() != "false":
                fail("unsupported_project_container", "unknown, duplicate or one-shot project containers require reconciliation")
            if any(row.get(key, "") != labels.get(key, "") for key in (
                    LABEL + "project.working_dir", LABEL + "project.config_files", LABEL + "project.environment_file")):
                fail("deployment_labels_changed", "project containers do not share the same Compose inputs")
            definition = config["services"][service]
            expected_env = definition.get("environment", {})
            actual_env = dict(value.split("=", 1) for value in item.get("Env", []) if "=" in value)
            expected_mounts = mounts(config, service)
            actual_mounts = {}
            tmpfs = definition.get("tmpfs", [])
            ephemeral_tmpfs = {value.split(":", 1)[0] for value in tmpfs}
            for mount in container.get("Mounts", []):
                target = mount.get("Destination")
                if mount.get("Type") == "tmpfs" and target in ephemeral_tmpfs:
                    continue
                source = mount.get("Name") if mount.get("Type") == "volume" else mount.get("Source")
                if target in actual_mounts:
                    fail("actual_mounts_changed", "a container has duplicate actual mount targets")
                actual_mounts[target] = {"type": mount.get("Type"), "source": source, "read_only": not mount.get("RW", False)}
            if actual_mounts != expected_mounts:
                fail("actual_mounts_changed", "rendered and actual container mounts differ")
            actual_ports = container.get("HostConfig", {}).get("PortBindings") or {}
            if {key: sorted(values or [], key=backup.json_bytes) for key, values in actual_ports.items()} != _ports(definition):
                fail("actual_ports_changed", "rendered and actual published ports differ")
            image = container.get("Image", "")
            reference = definition.get("image", "")
            if not IMAGE_ID.fullmatch(image) or not reference or item.get("Image") != reference:
                fail("actual_image_changed", "rendered and actual service image references differ")
            if image not in image_configs:
                image_configs[image] = docker.run(["docker", "image", "inspect", image], json_output=True)[0]
            image_env = image_configs[image].get("Config", {}).get("Env") or []
            baseline_env = dict(value.split("=", 1) for value in image_env if "=" in value)
            if any(value is None for value in expected_env.values()) or actual_env != baseline_env | {key: str(value) for key, value in expected_env.items()}:
                fail("effective_environment_changed", "current private inputs do not reproduce the container environment")
            if reference in images and images[reference] != image:
                fail("actual_image_changed", "services sharing a reference run different image IDs")
            images[reference] = image
            observed[service] = {"container_id": container["Id"], "image_id": image,
                                 "running": container.get("State", {}).get("Running") is True}
        if set(observed) != set(config["services"]):
            fail("deployment_incomplete", "not every active Compose service has an observed container")
        docker.actual_images = images
        plan = backup.Plan.load(docker)
        if checksum(plan.config) != checksum(config):
            fail("deployment_inputs_changed", "Compose inputs changed during discovery")
        if plan.engine != engine:
            fail("database_service_mismatch", "backup mapping changed its effective database")
        plan.assert_cold(allow_project=True)
        # Also validate ownership of ephemeral named volumes. They are excluded
        # from snapshots, but an external volume cannot become disposable state.
        for service in config["services"]:
            for raw in config["services"][service].get("volumes", []):
                if raw.get("type") != "volume":
                    continue
                source = config["volumes"][raw["source"]].get("name", name + "_" + raw["source"])
                resource = backup.Resource("observation", "volume", source, volume_key=raw["source"])
                if plan.volume_root(resource) is None:
                    fail("volume_missing", "an observed named volume no longer exists")
        if before != _inputs(all_inputs) or checksum(config) != checksum(docker.config()):
            fail("deployment_inputs_changed", "Compose inputs changed during discovery")
        # A container replacement, even with the same config, invalidates this
        # discovery. The caller must repeat before any later mutation phase.
        current = {row["Id"]: row for row in docker.containers()
                   if (row.get("Config", {}).get("Labels") or {}).get(LABEL + "project") == name}
        if set(current) != {row["Id"] for row in own}:
            fail("deployment_containers_changed", "project containers changed during discovery")
        for container in own:
            if _container_identity(current[container["Id"]]) != _container_identity(container):
                fail("deployment_containers_changed", "project containers changed during discovery")
        plan.assert_cold(allow_project=True)
        if current_receipt:
            from scripts.operator_registry import root_identity
            if ({key:root_identity(resource.root) for key,resource in plan.resources.items()} != current_receipt['roots']
                    or {service:row['image_id'] for service,row in observed.items()}
                    != {service:row['id'] for service,row in receipt['images'].items()}):
                fail('operator_deployment_changed','observed resources or images differ from the host deployment receipt')
        source_files = [Path(name) for name in receipt['source_files']] if managed and 'source_files' in receipt else None
        source_env = [Path(name) for name in receipt['source_env_files']] if source_files else None
        source_inputs = receipt['source_inputs'] if source_files else None
        return cls(docker, plan, before, observed, role,source_files,source_env,source_inputs,
            current_receipt['phase'] if current_receipt else None)

    def summary(self):
        return {"version": 1, "state": "inspected", "project": self.plan.name,
                "role": self.role, "database": self.plan.engine,
                "profiles": self.docker.profiles,
                "input_sha256": checksum(self.inputs), "configuration_sha256": checksum(self.plan.config),
                "services": self.observed, "resources": sorted(self.plan.resources),
                "media_included": False, "deployment_changed": False,
                **({'operator_phase':self.operator_phase} if self.operator_phase else {})}


def compare_candidate(deployment, candidate):
    """Admission only, not permission to stop writers or activate a candidate."""
    old = deployment.plan.config
    if candidate.get("name") != old["name"] or _role(candidate) != (deployment.role, deployment.plan.engine):
        fail("candidate_identity_mismatch", "candidate changes the project, role or database engine")
    if set(old["services"]) - set(candidate["services"]):
        fail("candidate_removes_service", "service removal needs a separately approved mapping")
    additions = sorted(set(candidate["services"]) - set(old["services"]))
    existing_mounts = {target: value for name in old["services"] for target, value in mounts(old, name).items()}
    for name, definition in candidate["services"].items():
        mapped = mounts(candidate, name)
        if name in old["services"]:
            previous = mounts(old, name)
            if any(mapped.get(target) != value for target, value in previous.items()):
                fail("candidate_changes_mount", "candidate changes an existing mount or its access mode")
            old_env = old["services"][name].get("environment", {})
            new_env = definition.get("environment", {})
            if any(new_env.get(key) != value for key, value in old_env.items()):
                fail("candidate_changes_environment", "candidate does not preserve an effective private input")
            for key in ("container_name", "ports", "network_mode"):
                if definition.get(key) != old["services"][name].get(key):
                    fail("candidate_changes_endpoint", "candidate changes an existing service identity or published endpoint")
            for engine_service in ("mysql", "redis"):
                if engine_service in old["services"] and candidate["services"][engine_service].get("image") not in {
                        old["services"][engine_service].get("image"), deployment.observed.get(engine_service, {}).get("image_id")}:
                    fail("candidate_changes_database_image", "database/cache engine updates require separate compatibility proof")
        for target, value in mapped.items():
            if target in backup.EPHEMERAL_TARGETS:
                if target in existing_mounts:
                    item = existing_mounts[target]
                    if (value["type"], value["source"]) != (item["type"], item["source"]):
                        fail("candidate_ephemeral_mapping_invalid", "candidate changes an existing ephemeral namespace")
                else:
                    if value["type"] != "volume" or not value["source"].startswith(old["name"] + "_") or any(
                            value["source"] == item["source"] for item in existing_mounts.values()):
                        fail("candidate_ephemeral_mapping_invalid", "a new ephemeral mount must be an independent project-local named volume")
                continue
            # Access can intentionally differ between readers and writers; the
            # underlying persistent namespace must already be in the snapshot.
            item = existing_mounts.get(target)
            if item is None or (value["type"], value["source"]) != (item["type"], item["source"]):
                fail("candidate_adds_persistent_resource", "a new persistent resource requires a recovery mapping")
    return {"state": "mapping_admitted", "added_services": additions,
            "configuration_sha256": checksum(candidate), "deployment_changed": False}
