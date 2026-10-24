"""Disposable Docker-host layout acceptance, not old-application upgrade proof.

The Web/edge stand-ins sleep: these tests exercise real Compose labels, images,
mounts, .env precedence and refusals. They do not claim business/TLS readiness.
"""
import copy
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid

from scripts import operator_backup as backup
from scripts import operator_deployment as operator


@unittest.skipUnless(os.environ.get("FC_OPERATOR_DISCOVERY_RUNTIME") == "1", "explicit isolated Docker-host discovery acceptance")
class NativeDiscoveryTests(unittest.TestCase):
    def isolated_subnet(self):
        names = self.run_command(['docker','network','ls','--format','{{.ID}}']).splitlines()
        networks = json.loads(self.run_command(['docker','network','inspect',*names])) if names else []
        occupied = [ipaddress.ip_network(row['Subnet']) for item in networks
            for row in ((item.get('IPAM') or {}).get('Config') or []) if row.get('Subnet')]
        for index in range(250,100,-1):
            candidate = ipaddress.ip_network(f'10.248.{index}.0/24')
            if not any(candidate.overlaps(other) for other in occupied if other.version == 4):
                return str(candidate)
        self.fail('No free isolated operator fixture subnet')

    def run_command(self, command):
        result = subprocess.run(command, capture_output=True, timeout=120, check=False)
        evidence = os.environ.get("FC_OPERATOR_DISCOVERY_EVIDENCE")
        if evidence:
            path = backup.absolute(Path(evidence), exists=False)
            event = {"format": "frontiercloud-python-native-operator-input", "version": 1,
                     "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "test": self.id(),
                     "argv": list(command), "working_directory": str(Path.cwd()), "exit_code": result.returncode,
                     "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(), "stderr_sha256": hashlib.sha256(result.stderr).hexdigest()}
            if len(command) > 1 and Path(command[1]).name == "frontiercloud_operator.py" and result.returncode == 0:
                event["operator_output"] = json.loads(result.stdout)
            descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "ab") as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(backup.json_bytes(event) + b"\n")
                target.flush()
                os.fsync(target.fileno())
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace")[-1500:])
        return result.stdout.decode().strip()

    def prepare(self, *, storage=False, mysql=False, explicit_env=False, mysql_profile=False, inactive_mysql=False, literal_input=False, owned_application_image=False, business_mode='', application_tls=False):
        temporary = tempfile.TemporaryDirectory(prefix="fc-op-discovery-")
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        data = directory / ("custom$control" if literal_input else "custom-control")
        data.mkdir()
        project = "fc-op-discovery-" + uuid.uuid4().hex[:12]
        self.projects.append(project)
        marker = data / "identity-kept.bin"
        marker.write_bytes(b"\x00\xffdo-not-change-owner-state")
        source = os.environ.get("FC_OPERATOR_DISCOVERY_IMAGE", "frontiercloud-python-archive:db-test-tools")
        if owned_application_image:
            alias = project + "-application:old"
            self.run_command(["docker", "tag", source, alias])
            self.addCleanup(self.run_command, ["docker", "image", "rm", alias])
            source = alias
        common = {"image": source, "command": ["python", "-c", "import time;time.sleep(3600)"],
                  "read_only": True, "cpus": 1, "mem_limit": "64m", "environment": {"PYTHONDONTWRITEBYTECODE": "1"}}
        config = {"name": project, "services": {
            "web": dict(common, environment={"DB_TYPE": "mysql" if mysql else "sqlite", "SQLITE_PATH": "/app/data/frontiercloud.db",
                                              "DEPLOYMENT_MODE": "only_stroge" if storage else business_mode,
                                              "ADMIN_KEY": "${OPERATOR_FIXTURE_KEY:?missing fixture input}"},
                        volumes=[str(data).replace("$", "$$") + ":/app/data", "runtime_secrets:/run/frontiercloud-secrets"]),
            "redis": {"image": "redis:7.4.11-alpine", "cpus": .25, "mem_limit": "64m", "volumes": ["redis_data:/data"]},
            "certificate-agent": dict(common, volumes=["certificate_state:/var/lib/frontiercloud-certificate",
                                                         "certificate_published:/run/frontiercloud-tls", "certificate_runtime:/run/frontiercloud-certificate"])
        }, "volumes": {name: {} for name in ("runtime_secrets", "redis_data", "certificate_state", "certificate_published", "certificate_runtime")},
            "networks": {"default": {"internal": True,"ipam":{"config":[{"subnet":self.isolated_subnet()}]}}}}
        if not storage:
            config["services"]["nginx"] = dict(common, volumes=[str(data).replace("$", "$$") + ":/app/data:ro"])
        if application_tls:
            config['services']['web']['mem_limit'] = '512m'
            config['services']['web']['environment'].update(TLS_ENABLED='true', SERVER_NAME='operator.4399.com')
        if mysql or inactive_mysql:
            config["services"]["mysql"] = {"image": "mysql:8.4.11", "cpus": 1, "mem_limit": "512m",
                                            "environment": {"MYSQL_ALLOW_EMPTY_PASSWORD": "yes"},
                                            "volumes": ["mysql_data:/var/lib/mysql", "mysql_socket:/var/run/mysqld"]}
            config["volumes"].update(mysql_data={}, mysql_socket={})
            if mysql_profile or inactive_mysql:
                config["services"]["mysql"]["profiles"] = ["mysql"]
        file = directory / ("docker-compose.storage.yaml" if storage else "docker-compose.yaml")
        file.write_text(json.dumps(config))
        env_file = directory / ("operator-private.env" if explicit_env else ".env")
        env_file.write_text("OPERATOR_FIXTURE_KEY='literal$DOLLAR${BRACES}$$tail'\n" if literal_input else "OPERATOR_FIXTURE_KEY=private-fixture-not-an-owner-key\n")
        command = ["docker", "compose", "--project-directory", str(directory), "-f", str(file), "--project-name", project]
        if mysql_profile:
            command += ["--profile", "mysql"]
        if explicit_env:
            command += ["--env-file", str(env_file)]
            # The explicit file takes precedence; an unrelated .env must not
            # silently be adopted during discovery.
            (directory / ".env").write_text("OPERATOR_FIXTURE_KEY=wrong-private-input\n")
        self.addCleanup(self.run_command, command + ["down", "--volumes", "--timeout", "10"])
        self.run_command(command + ["up", "-d", "--no-build", "--pull", "never"])
        return directory, project, config, file, marker

    def setUp(self):
        self.projects = []

    def test_business_sqlite_custom_root_and_unchanged_live_containers(self):
        directory, project, _, _, marker = self.prepare()
        before = backup.digest(marker)
        first = operator.Deployment.discover(directory)
        second = operator.Deployment.discover(directory)
        self.assertEqual(first.plan.name, project)
        self.assertEqual(first.plan.engine, "sqlite")
        self.assertEqual(first.observed, second.observed)
        self.assertEqual(before, backup.digest(marker))
        self.assertTrue(all(row["running"] for row in first.observed.values()))
        self.assertNotIn("private-fixture", json.dumps(first.summary()))
        # Run the actual stdlib-only host entry, not an in-container API shim.
        entry = Path(__file__).resolve().parents[1] / "scripts/frontiercloud_operator.py"
        actual_input = ["python3", str(entry), "inspect", "--project-directory", str(directory)]
        result = json.loads(self.run_command(actual_input))
        self.assertEqual((result["state"], result["project"]), ("inspected", project))
        self.assertFalse(result["deployment_changed"])

    def test_independent_storage_and_explicit_env_file_precedence(self):
        directory, project, _, _, _ = self.prepare(storage=True, explicit_env=True)
        result = operator.Deployment.discover(directory)
        self.assertEqual((result.plan.name, result.role, result.plan.engine), (project, "storage", "sqlite"))
        self.assertEqual([Path(row["path"]).name for row in result.inputs], ["docker-compose.storage.yaml", "operator-private.env"])

    def test_actual_mysql_mount_and_exact_running_image_identity(self):
        directory, _, _, _, marker = self.prepare(mysql=True)
        result = operator.Deployment.discover(directory)
        self.assertEqual(result.plan.engine, "mysql")
        self.assertIn("mysql", result.plan.resources)
        self.assertEqual(result.plan.images["mysql"]["id"], result.observed["mysql"]["image_id"])
        self.assertEqual(marker.read_bytes(), b"\x00\xffdo-not-change-owner-state")

    def test_mysql_profile_is_recovered_only_for_observed_mysql_deployment(self):
        directory, _, _, _, _ = self.prepare(mysql=True, mysql_profile=True)
        result = operator.Deployment.discover(directory)
        self.assertEqual((result.plan.engine, result.docker.profiles), ("mysql", ["mysql"]))
        sqlite_directory, _, _, _, _ = self.prepare(inactive_mysql=True)
        result = operator.Deployment.discover(sqlite_directory)
        self.assertEqual((result.plan.engine, result.docker.profiles), ("sqlite", []))
        self.assertNotIn("mysql", result.observed)
        self.assertNotIn("mysql", result.plan.resources)

    def test_literal_private_values_and_dollar_data_paths_survive_resolved_compose(self):
        directory, project, _, _, marker = self.prepare(literal_input=True)
        current = operator.Deployment.discover(directory)
        self.assertEqual(current.plan.config["services"]["web"]["environment"]["ADMIN_KEY"], "literal$DOLLAR${BRACES}$$tail")
        self.assertEqual(current.plan.resources["control"].root, marker.parent)
        file = directory / "literal-resolved.json"
        file.write_bytes(backup.json_bytes(backup.compose_values(current.plan.config, encode=True)))
        rendered = operator.ComposeDocker(directory, [file], project, []).config()
        self.assertEqual(rendered, current.plan.config)

    def test_changed_private_input_unknown_service_and_mount_refuse_before_any_stop(self):
        directory, _, config, file, marker = self.prepare()
        initial = operator.Deployment.discover(directory).observed
        before = backup.digest(marker)
        variants = []
        changed = copy.deepcopy(config)
        changed["services"]["web"]["environment"]["ADMIN_KEY"] = "new-secret-must-not-appear"
        variants.append((changed, "effective_environment_changed"))
        changed = copy.deepcopy(config)
        changed["services"]["custom"] = copy.deepcopy(config["services"]["web"])
        variants.append((changed, "unsupported_services"))
        changed = copy.deepcopy(config)
        other = directory / "wrong-root"
        other.mkdir()
        changed["services"]["web"]["volumes"][0] = str(other) + ":/app/data"
        variants.append((changed, "actual_mounts_changed"))
        for candidate, code in variants:
            file.write_text(json.dumps(candidate))
            with self.assertRaises(operator.DeploymentError) as caught:
                operator.Deployment.discover(directory)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("new-secret", str(caught.exception))
        file.write_text(json.dumps(config))
        self.assertEqual(initial, operator.Deployment.discover(directory).observed)
        self.assertEqual(before, backup.digest(marker))

    def test_foreign_container_using_data_blocks_and_remains_untouched(self):
        directory, project, _, _, marker = self.prepare()
        name = project + "-foreign"
        self.run_command(["docker", "run", "-d", "--name", name, "--network", "none", "--cpus", ".25", "--memory", "64m",
                          "--mount", "type=bind,source=" + str(marker.parent) + ",target=/foreign,readonly",
                          os.environ.get("FC_OPERATOR_DISCOVERY_IMAGE", "frontiercloud-python-archive:db-test-tools"), "python", "-c", "import time;time.sleep(3600)"])
        self.addCleanup(self.run_command, ["docker", "rm", "-f", name])
        with self.assertRaisesRegex(backup.BackupError, "another active container"):
            operator.Deployment.discover(directory)
        self.assertEqual(self.run_command(["docker", "inspect", "--format", "{{.State.Running}}", name]), "true")

    def test_candidate_new_service_and_traffic_volume_admission_do_not_create_them(self):
        directory, project, _, _, marker = self.prepare()
        current = operator.Deployment.discover(directory)
        candidate = copy.deepcopy(current.plan.config)
        candidate["services"]["certificate-init"] = copy.deepcopy(candidate["services"]["certificate-agent"])
        candidate["volumes"]["traffic_runtime"] = {"name": project + "_traffic_runtime"}
        candidate["services"]["web"]["volumes"].append({"type": "volume", "source": "traffic_runtime", "target": "/run/frontiercloud-traffic", "read_only": True})
        before = backup.digest(marker)
        self.assertEqual(operator.compare_candidate(current, candidate)["added_services"], ["certificate-init"])
        names = self.run_command(["docker", "volume", "ls", "--format", "{{.Name}}"]).splitlines()
        self.assertNotIn(project + "_traffic_runtime", names)
        self.assertEqual(current.observed, operator.Deployment.discover(directory).observed)
        self.assertEqual(before, backup.digest(marker))


if __name__ == "__main__":
    unittest.main(verbosity=2)
