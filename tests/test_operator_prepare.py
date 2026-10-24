"""Preparation preserves inputs and fails before a candidate can start."""
import copy
import json
import os
from pathlib import Path
import sys
import socket
import tempfile
from types import SimpleNamespace
import unittest

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_prepare as prepare


class MappingTests(unittest.TestCase):
    def test_host_records_cannot_be_exposed_through_a_persistent_mount(self):
        directory = Path('/srv/frontiercloud/deployment')
        for data in (directory,directory.parent):
            deployment = SimpleNamespace(docker=SimpleNamespace(directory=directory),
                plan=SimpleNamespace(resources={'control':SimpleNamespace(root=data)}))
            with self.assertRaises(mapping.DeploymentError) as caught:
                prepare.host_records_outside_data(deployment)
            self.assertEqual(caught.exception.code,'operator_records_overlap_data')
        deployment.plan.resources['control'].root = directory/'data'
        prepare.host_records_outside_data(deployment)

    def test_next_preparation_uses_original_source_mapping_not_generated_compose(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deployment = root/'deployment'
            source = root/'source'
            deployment.mkdir()
            source.mkdir()
            names = ('docker-compose.yaml','docker-compose.mysql.yaml')
            for name in names:
                (deployment/name).write_text('old owner input')
                (source/name).write_text('new canonical input')
            override = deployment/'owner-override.yaml'
            override.write_text('private owner override')
            previous = SimpleNamespace(source_files=[*(deployment/name for name in names),override],
                docker=SimpleNamespace(directory=deployment,files=[root/'generated-compose.json']))
            self.assertEqual(prepare._old_files(previous,source),[*(source/name for name in names),override])

    def test_existing_network_options_are_preserved(self):
        candidate = copy.deepcopy(self.old)
        self.old['networks'] = {'default': {'name': 'fixture_default', 'internal': True,
            'driver_opts': {'com.docker.network.bridge.enable_icc': 'true'}}}
        candidate['networks'] = {'default': {'name': 'fixture_default'}}
        result = prepare._shape(self.deployment, candidate)
        self.assertEqual(result['networks'], self.old['networks'])

    def setUp(self):
        self.old = {"name": "fixture", "services": {
            "web": {"image": "old", "environment": {"PRIVATE": "keep$${literal}"},
                    "cpus": 1.0, "ports": [{"target": 8000, "published": "18443", "host_ip": "127.0.0.1"}],
                    "read_only": True, "volumes": [{"type": "volume", "source": "old_key", "target": "/run/frontiercloud-secrets"}]},
            "redis": {"image": "redis", "volumes": [{"type": "volume", "source": "cache", "target": "/data"}]}},
            "volumes": {"old_key": {"name": "fixture_old_key"}, "cache": {"name": "fixture_cache"}}}
        self.deployment = SimpleNamespace(plan=SimpleNamespace(config=self.old, name="fixture"))

    def test_private_environment_endpoint_limits_and_original_volume_keys_survive(self):
        candidate = copy.deepcopy(self.old)
        candidate["services"]["web"].update(environment={"PRIVATE": "wrong", "NEW_DEFAULT": "added"}, cpus=4)
        candidate["services"]["web"]["ports"][0]["published"] = "443"
        candidate["services"]["certificate-init"] = {"image": "new", "volumes": [{"type": "volume", "source": "new_key", "target": "/run/frontiercloud-secrets"}]}
        candidate["volumes"]["new_key"] = {"name": "fixture_new_key"}
        result = prepare._shape(self.deployment, candidate)
        self.assertEqual(result["services"]["web"]["environment"], {"PRIVATE": "keep$${literal}", "NEW_DEFAULT": "added"})
        self.assertEqual(result["services"]["web"]["ports"], self.old["services"]["web"]["ports"])
        self.assertEqual(result["services"]["web"]["cpus"], 1)
        self.assertEqual(result["services"]["certificate-init"]["volumes"][0]["source"], "old_key")
        self.assertEqual(self.old["services"]["web"]["environment"], {"PRIVATE": "keep$${literal}"})

    def test_unknown_command_privilege_or_service_removal_is_not_silently_mapped(self):
        for key, value in (("command", ["new-program"]), ("user", "0:0"), ("read_only", False)):
            candidate = copy.deepcopy(self.old)
            candidate["services"]["web"][key] = value
            with self.assertRaises(mapping.DeploymentError) as caught:
                prepare._shape(self.deployment, candidate)
            self.assertEqual(caught.exception.code, "unsupported_service_override")
        candidate = copy.deepcopy(self.old)
        candidate["services"].pop("redis")
        with self.assertRaises(mapping.DeploymentError) as caught:
            prepare._shape(self.deployment, candidate)
        self.assertEqual(caught.exception.code, "candidate_removes_service")

    def test_a_preexisting_unadmitted_ephemeral_volume_is_never_cleared_or_reused(self):
        candidate = copy.deepcopy(self.old)
        candidate["volumes"]["traffic"] = {"name": "fixture_traffic"}
        candidate["services"]["web"]["volumes"].append({"type": "volume", "source": "traffic", "target": "/run/frontiercloud-traffic"})
        self.deployment.docker = SimpleNamespace(volume=lambda name: {"Name": name})
        with self.assertRaises(mapping.DeploymentError) as caught:
            prepare._new_volumes(self.deployment, candidate)
        self.assertEqual(caught.exception.code, "candidate_ephemeral_volume_exists")

    def test_new_ephemeral_names_do_not_collide_with_retained_failed_material(self):
        candidate = copy.deepcopy(self.old)
        candidate['volumes']['traffic'] = {'name':'fixture_traffic'}
        candidate['services']['web']['volumes'].append({'type':'volume','source':'traffic','target':'/run/frontiercloud-traffic'})
        old_names = {key:value['name'] for key,value in self.old['volumes'].items()}
        sentinel = {'Name':'fixture_traffic','unknown_state':'preserve'}
        self.deployment.docker = SimpleNamespace(volume=lambda name:sentinel if name == 'fixture_traffic' else None)
        prepare._fresh_ephemeral_names(self.deployment,candidate,'a'*32)
        added = prepare._new_volumes(self.deployment,candidate)
        self.assertEqual(len(added),1)
        self.assertTrue(next(iter(added)).startswith('fixture_operator_'))
        self.assertEqual(candidate['volumes']['old_key']['name'],old_names['old_key'])
        self.assertEqual(sentinel,{'Name':'fixture_traffic','unknown_state':'preserve'})

    def test_existing_ephemeral_namespace_is_preserved_and_external_new_volume_still_refuses(self):
        self.old['volumes']['traffic'] = {'name':'previous_restored_traffic'}
        self.old['services']['web']['volumes'].append({'type':'volume','source':'traffic','target':'/run/frontiercloud-traffic'})
        candidate = copy.deepcopy(self.old)
        prepare._fresh_ephemeral_names(self.deployment,candidate,'a'*32)
        self.assertEqual(candidate['volumes']['traffic']['name'],'previous_restored_traffic')
        candidate['volumes']['new_runtime'] = {'name':'external_unmanaged','external':True}
        candidate['services']['web']['volumes'].append({'type':'volume','source':'new_runtime','target':'/run/frontiercloud-certificate'})
        prepare._fresh_ephemeral_names(self.deployment,candidate,'a'*32)
        with self.assertRaises(mapping.DeploymentError):mapping.compare_candidate(self.deployment,candidate)

    def test_private_literal_serialization_roundtrip_does_not_change_keys_or_types(self):
        value = {"$KEY": ["one$two${THREE}$$four", 1, False, None]}
        self.assertEqual(backup.compose_values(backup.compose_values(value, encode=True)), value)
        self.assertIn("$KEY", backup.compose_values(value, encode=True))

    def test_same_published_socket_cannot_hide_behind_different_target_ports(self):
        self.deployment.docker = SimpleNamespace(containers=lambda: [])
        candidate = copy.deepcopy(self.old)
        candidate["services"]["redis"]["ports"] = [{"target": 6379, "published": "18443", "host_ip": "0.0.0.0"}]
        with self.assertRaises(mapping.DeploymentError) as caught:
            prepare._port_conflicts(self.deployment, candidate)
        self.assertEqual(caught.exception.code, "candidate_port_conflict")

    def test_a_non_docker_host_listener_also_refuses_preparation(self):
        self.old["services"]["web"].pop("ports")
        self.deployment.docker = SimpleNamespace(containers=lambda: [])
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            candidate = copy.deepcopy(self.old)
            candidate["services"]["redis"]["ports"] = [{"target": 6379, "published": str(listener.getsockname()[1]), "host_ip": "127.0.0.1"}]
            with self.assertRaises(mapping.DeploymentError) as caught:
                prepare._port_conflicts(self.deployment, candidate)
        self.assertEqual(caught.exception.code, "candidate_host_port_unavailable")


@unittest.skipUnless(sys.platform == "linux", "host process groups and private Linux artifacts")
class CommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.docker = SimpleNamespace(directory=self.output, environment=dict(os.environ))

    def test_exact_input_precedes_command_and_private_diagnostics_survive_failure(self):
        program = "from pathlib import Path;assert Path('fixture-input.json').exists();print('native failure detail');raise SystemExit(7)"
        arguments = [sys.executable, "-c", program]
        with self.assertRaises(mapping.DeploymentError) as caught:
            prepare._command(self.docker, arguments, self.output, "fixture")
        self.assertEqual(caught.exception.code, "fixture_failed")
        self.assertNotIn("native failure detail", str(caught.exception))
        self.assertEqual(json.loads((self.output / "fixture-input.json").read_text())["argv"], arguments)
        self.assertEqual(json.loads((self.output / "fixture-result.json").read_text())["exit_code"], 7)
        self.assertEqual((self.output / "fixture.log").stat().st_mode & 0o777, 0o600)
        self.assertIn("native failure detail", (self.output / "fixture.log").read_text())

    def test_timeout_joins_own_child_and_retains_the_attempt(self):
        with self.assertRaises(mapping.DeploymentError) as caught:
            prepare._command(self.docker, [sys.executable, "-c", "import time;time.sleep(120)"], self.output, "slow", timeout=.05)
        self.assertEqual(caught.exception.code, "slow_timeout")
        result = json.loads((self.output / "slow-result.json").read_text())
        self.assertTrue(result["timed_out"])
        self.assertNotEqual(result["exit_code"], 0)
