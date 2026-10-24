"""Actual mapping mismatches must refuse without executing deployment commands."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import operator_backup as backup
from scripts import operator_deployment as operator
from scripts import operator_registry as registry
from tests.test_operator_backup import FakeDocker


@unittest.skipUnless(sys.platform == "linux", "Linux-host Compose mapping")
class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.fixture = FakeDocker(self.base, "deployment")
        self.fixture.create_volume("certificate_runtime")
        self.directory = self.fixture.directory
        self.config = self.fixture.config_value
        self.config["services"].pop("mysql")
        self.config["services"]["nginx"] = {"image": "nginx", "volumes": [
            {"type": "bind", "source": str(self.fixture.data), "target": "/app/data", "read_only": True}]}
        self.file = self.directory / "docker-compose.yaml"
        self.file.write_text(json.dumps(self.config))
        self.env = self.directory / ".env"
        self.env.write_text("SECRET=private-test-value\n")
        self.ids = {name: "sha256:" + hashlib.sha256(name.encode()).hexdigest() for name in self.config["services"]}
        self.calls = []
        self.containers = self.build_containers()
        owner = self

        class Docker:
            def __init__(self, directory, files, name, env_files, profiles=()):
                self.directory, self.files, self.name, self.env_files = directory, files, name, env_files
                self.profiles = list(profiles)
                self.actual_images = None

            def local_root(self):
                return owner.fixture.local_root()

            def config(self):
                return copy.deepcopy(owner.config)

            def containers(self):
                return copy.deepcopy(owner.containers)

            def volume(self, name):
                return owner.fixture.volume(name)

            def image_id(self, name):
                return {"id": self.actual_images[name], "architecture": "amd64", "os": "linux"}

            def run(self, arguments, **kwargs):
                owner.calls.append(arguments)
                if arguments[:3] == ["docker", "image", "inspect"]:
                    return [{"Config": {"Env": []}}]
                raise AssertionError("discovery tried a deployment command")

        self.factory = Docker

    def build_containers(self, *, storage=False):
        result = []
        for service, definition in self.config["services"].items():
            result.append({"Id": hashlib.sha256(service.encode()).hexdigest(), "Image": self.ids[service],
                           "Config": {"Image": definition["image"], "Env": [key + "=" + str(value) for key, value in definition.get("environment", {}).items()],
                                      "Labels": {operator.LABEL + "project": "deployment", operator.LABEL + "service": service,
                                                 operator.LABEL + "oneoff": "False", operator.LABEL + "project.working_dir": str(self.directory),
                                                 operator.LABEL + "project.config_files": str(self.directory / ("docker-compose.storage.yaml" if storage else "docker-compose.yaml"))}},
                           "Mounts": [{"Destination": target, "Type": value["type"], "RW": not value["read_only"],
                                       "Name" if value["type"] == "volume" else "Source": value["source"]}
                                      for target, value in operator.mounts(self.config, service).items()],
                           "HostConfig": {"PortBindings": {}}, "State": {"Running": True}})
        return result

    def discover(self):
        return operator.Deployment.discover(self.directory, docker_factory=self.factory)

    def refused(self, code):
        with self.assertRaises(operator.DeploymentError) as caught:
            self.discover()
        self.assertEqual(caught.exception.code, code)

    def test_exact_mapping_records_actual_image_ids_without_changes_or_secrets(self):
        before = {path: backup.digest(path) for path in (self.file, self.env)}
        result = self.discover()
        self.assertEqual(result.plan.engine, "sqlite")
        self.assertNotIn("mysql", result.plan.resources)
        self.assertEqual(result.plan.images["web"]["id"], self.ids["web"])
        self.assertEqual({path: backup.digest(path) for path in before}, before)
        self.assertNotIn("private-test-value", json.dumps(result.summary()))
        self.assertFalse(result.summary()["deployment_changed"])

    def test_independent_storage_base_keeps_separate_volume_keys(self):
        self.config["services"].pop("nginx")
        self.config["services"]["web"]["environment"]["DEPLOYMENT_MODE"] = "only_stroge"
        self.file = self.directory / "docker-compose.storage.yaml"
        self.file.write_text(json.dumps(self.config))
        self.containers = self.build_containers(storage=True)
        self.assertEqual(self.discover().role, "storage")

    def managed_fixture(self):
        directory = self.base/'prepared'
        directory.mkdir(mode=0o700)
        compose = directory/'compose.json'
        backup.private_write(compose,backup.json_bytes(self.config))
        receipt = {'project':'deployment','compose_sha256':backup.digest(compose)[1],
            'configuration_sha256':operator.checksum(self.config)}
        backup.private_write(directory/'prepared.json',backup.json_bytes(receipt))
        for row in self.containers:
            row['Config']['Labels'][operator.LABEL+'project.config_files'] = str(compose)
        return SimpleNamespace(directory=directory,receipt=receipt)

    def test_prepared_observation_requires_exact_private_receipt_and_config(self):
        prepared = self.managed_fixture()
        self.assertEqual(operator.Deployment.discover(self.directory,docker_factory=self.factory,
            prepared=prepared).plan.engine,'sqlite')
        self.refused('operator_receipt_missing')
        self.config['services']['web']['environment']['OWNER_INPUT'] = 'changed'
        with self.assertRaises(operator.DeploymentError) as caught:
            operator.Deployment.discover(self.directory,docker_factory=self.factory,prepared=prepared)
        self.assertEqual(caught.exception.code,'prepared_observation_mismatch')

    def test_prepared_observation_refuses_changed_compose_or_world_readable_receipt(self):
        prepared = self.managed_fixture()
        compose = prepared.directory/'compose.json'
        compose.write_bytes(compose.read_bytes()+b' ')
        with self.assertRaises(operator.DeploymentError) as caught:
            operator.Deployment.discover(self.directory,docker_factory=self.factory,prepared=prepared)
        self.assertEqual(caught.exception.code,'prepared_observation_mismatch')
        (prepared.directory/'prepared.json').chmod(0o644)
        with self.assertRaises(operator.DeploymentError) as caught:
            operator.Deployment.discover(self.directory,docker_factory=self.factory,prepared=prepared)
        self.assertEqual(caught.exception.code,'prepared_receipt_unsafe')

    def test_default_discovery_recognizes_the_bound_generated_deployment(self):
        original = self.discover()
        prepared = self.managed_fixture()
        identifier = 'a'*32
        prepared.receipt.update(version=1,id=identifier,phase='images_prepared',
            source_files=[str(self.file)],source_env_files=[],source_inputs=operator._inputs([self.file,self.env]),
            images={name:{'id':value} for name,value in self.ids.items()})
        (prepared.directory/'prepared.json').write_bytes(backup.json_bytes(prepared.receipt))
        registry.publish(self.directory,{'version':1,'id':identifier,'phase':'active_fenced','node_id':'b'*32,
            'generation':7,'project':'deployment','project_directory':str(self.directory),
            'prepared_directory':str(prepared.directory),'prepared_sha256':backup.digest(prepared.directory/'prepared.json')[1],
            'roots':{key:registry.root_identity(row.root) for key,row in original.plan.resources.items()}},previous_sha256=None)
        current = self.discover()
        self.assertEqual(current.operator_phase,'active_fenced')
        self.assertEqual(current.source_files,[self.file])
        self.assertEqual(current.source_env_files,[])
        self.assertEqual(current.source_inputs,operator._inputs([self.file,self.env]))
        self.env.write_text('SECRET=owner-change\n')
        self.refused('operator_source_inputs_changed')

    def test_observed_mysql_engine_recovers_profile_without_changing_sqlite(self):
        self.assertEqual(self.discover().docker.profiles, [])
        self.config["services"]["web"]["environment"]["DB_TYPE"] = "mysql"
        self.config["services"]["mysql"] = {"image": "mysql", "volumes": [
            {"type": "volume", "source": "mysql_data", "target": "/var/lib/mysql"}]}
        self.fixture.create_volume("mysql_data")
        self.ids["mysql"] = "sha256:" + hashlib.sha256(b"mysql").hexdigest()
        self.containers = self.build_containers()
        current = self.discover()
        self.assertEqual((current.plan.engine, current.docker.profiles), ("mysql", ["mysql"]))

    def test_no_matching_and_ambiguous_deployments(self):
        original = copy.deepcopy(self.containers)
        self.containers = []
        self.refused("deployment_not_found")
        self.containers = original + [copy.deepcopy(original[0])]
        self.refused("deployment_ambiguous")

    def test_effective_shell_override_must_be_reproducible(self):
        self.containers[0]["Config"]["Env"].append("SECRET_FROM_OLD_SHELL=must-not-disappear")
        self.refused("effective_environment_changed")

    def test_changed_configured_environment_is_refused_without_secret_output(self):
        self.config["services"]["web"]["environment"]["ADMIN_SECRET"] = "do-not-print"
        with self.assertRaises(operator.DeploymentError) as caught:
            self.discover()
        self.assertEqual(caught.exception.code, "effective_environment_changed")
        self.assertNotIn("do-not-print", str(caught.exception))

    def test_unknown_or_oneoff_project_container_cannot_be_ignored(self):
        row = copy.deepcopy(self.containers[0])
        row["Config"]["Labels"][operator.LABEL + "service"] = "custom"
        self.containers.append(row)
        self.refused("unsupported_project_container")
        self.containers.pop()
        self.containers[0]["Config"]["Labels"][operator.LABEL + "oneoff"] = "True"
        self.refused("unsupported_project_container")

    def test_partial_project_inputs_and_missing_service_refuse(self):
        self.containers[-1]["Config"]["Labels"][operator.LABEL + "project.config_files"] += "," + str(self.env)
        self.refused("deployment_labels_changed")
        self.containers.pop()
        self.refused("deployment_incomplete")

    def test_changed_actual_mount_and_published_port_refuse(self):
        original = copy.deepcopy(self.containers)
        self.containers[0]["Mounts"][0]["RW"] = False
        self.refused("actual_mounts_changed")
        self.containers = original
        self.containers[0]["HostConfig"]["PortBindings"] = {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "9000"}]}
        self.refused("actual_ports_changed")

    def test_actual_container_image_reference_must_match_current_inputs(self):
        self.containers[0]["Config"]["Image"] = "another-image"
        self.refused("actual_image_changed")

    def test_changed_inputs_during_inspection_refuse(self):
        original = self.factory.config
        counter = 0
        def changed(docker):
            nonlocal counter
            counter += 1
            if counter == 2:
                self.env.write_text("SECRET=changed\n")
            return original(docker)
        with patch.object(self.factory, "config", changed):
            self.refused("deployment_inputs_changed")

    def test_container_health_timestamp_is_not_a_configuration_change(self):
        original = self.factory.containers
        counter = 0
        def health(docker):
            nonlocal counter
            counter += 1
            rows = original(docker)
            for row in rows:
                row["State"]["Health"] = {"Log": [{"End": counter}]}
            return rows
        with patch.object(self.factory, "containers", health):
            self.assertEqual(self.discover().role, "business")

    def test_container_restart_during_inspection_refuse(self):
        original = self.factory.containers
        counter = 0
        def restarted(docker):
            nonlocal counter
            counter += 1
            rows = original(docker)
            if counter >= 3:
                rows[0]["RestartCount"] = 1
            return rows
        with patch.object(self.factory, "containers", restarted):
            self.refused("deployment_containers_changed")

    def test_docker_mount_response_order_is_not_a_deployment_change(self):
        original = self.factory.containers
        counter = 0
        def reordered(docker):
            nonlocal counter
            counter += 1
            rows = original(docker)
            if counter % 2:
                for row in rows:
                    row["Mounts"].reverse()
            return rows
        with patch.object(self.factory, "containers", reordered):
            self.assertEqual(self.discover().role, "business")

    def test_foreign_readonly_mount_is_preserved_and_blocks_cold_recovery_mapping(self):
        self.containers.append({"Config": {"Labels": {}}, "State": {"Running": True}, "Mounts": [{"Source": str(self.fixture.data), "RW": False}]})
        with self.assertRaisesRegex(backup.BackupError, "another active container"):
            self.discover()

    def test_symlink_private_input_cannot_be_followed(self):
        self.env.unlink()
        self.env.symlink_to(self.file)
        with self.assertRaisesRegex(backup.BackupError, "symbolic links"):
            self.discover()

    def test_candidate_preserves_private_inputs_and_admits_only_declared_traffic_volume(self):
        current = self.discover()
        candidate = copy.deepcopy(self.config)
        candidate["volumes"]["traffic_runtime"] = {"name": "deployment_traffic_runtime"}
        candidate["services"]["web"]["volumes"].append({"type": "volume", "source": "traffic_runtime", "target": "/run/frontiercloud-traffic", "read_only": True})
        self.assertEqual(operator.compare_candidate(current, candidate)["state"], "mapping_admitted")
        candidate["volumes"]["traffic_runtime"]["name"] = "deployment_runtime_secrets"
        with self.assertRaises(operator.DeploymentError) as caught:
            operator.compare_candidate(current, candidate)
        self.assertEqual(caught.exception.code, "candidate_ephemeral_mapping_invalid")

    def test_candidate_service_addition_only_reuses_known_resource_classes(self):
        current = self.discover()
        candidate = copy.deepcopy(self.config)
        candidate["services"]["certificate-init"] = copy.deepcopy(candidate["services"]["certificate-agent"])
        self.assertEqual(operator.compare_candidate(current, candidate)["added_services"], ["certificate-init"])
        candidate["services"]["certificate-init"]["volumes"][0]["source"] = "runtime_secrets"
        with self.assertRaises(operator.DeploymentError) as caught:
            operator.compare_candidate(current, candidate)
        self.assertEqual(caught.exception.code, "candidate_adds_persistent_resource")

    def test_candidate_cannot_change_engine_mount_or_private_input(self):
        current = self.discover()
        for code, mutate in (
                ("candidate_identity_mismatch", lambda cfg: cfg.update(name="new-node")),
                ("candidate_changes_mount", lambda cfg: cfg["services"]["web"]["volumes"][0].update(source=str(self.base))),
                ("candidate_changes_environment", lambda cfg: cfg["services"]["web"]["environment"].update(DB_TYPE="mysql")),
                ("candidate_changes_database_image", lambda cfg: cfg["services"]["redis"].update(image="new-redis"))):
            candidate = copy.deepcopy(self.config)
            mutate(candidate)
            with self.assertRaises(operator.DeploymentError) as caught:
                operator.compare_candidate(current, candidate)
            # An engine change can fail even earlier, at its topology guard.
            self.assertIn(caught.exception.code, {code, "database_service_missing"})

    def test_docker_config_does_not_inherit_ambient_app_or_compose_overrides(self):
        with patch.dict(os.environ, {"SECRET_FROM_HOST": "hidden", "COMPOSE_PROFILES": "mysql"}):
            docker = operator.ComposeDocker(self.directory, [self.file], "deployment", [])
        self.assertNotIn("SECRET_FROM_HOST", docker.environment)
        self.assertNotIn("COMPOSE_PROFILES", docker.environment)
        self.assertIn("--project-name", docker.base)

    def test_vanished_unrelated_oneoff_repeats_the_complete_inventory(self):
        docker = operator.ComposeDocker(self.directory,[self.file],'deployment',[])
        first,vanished = 'a'*64,'b'*64
        row = {'Id':first,'Config':{'Labels':{}}}
        failure = operator.DeploymentError('docker_container_inspect_failed','inspect failed')
        with patch.object(docker,'run',side_effect=[first+'\n'+vanished,failure,first,first,[row]]) as run:
            self.assertEqual(docker.containers(),[row])
        self.assertEqual(run.call_args_list[-1].args[0],['docker','inspect',first])
        self.assertTrue(all(call.args[0][1] in {'ps','inspect'} for call in run.call_args_list))

    def test_stable_inventory_failure_and_continuous_churn_are_not_ignored(self):
        docker = operator.ComposeDocker(self.directory,[self.file],'deployment',[])
        failure = operator.DeploymentError('docker_container_inspect_failed','inspect denied')
        with patch.object(docker,'run',side_effect=['a'*64,failure,'a'*64]),self.assertRaises(operator.DeploymentError) as caught:
            docker.containers()
        self.assertEqual(caught.exception.code,'docker_container_inspect_failed')
        counter = 0
        def changing(arguments,**_options):
            nonlocal counter
            if arguments[1] == 'ps':
                counter += 1
                return f'{counter:064x}'
            raise failure
        with patch.object(docker,'run',side_effect=changing),self.assertRaises(operator.DeploymentError) as caught:
            docker.containers()
        self.assertEqual(caught.exception.code,'docker_inventory_changing')
        self.assertEqual(counter,6)
