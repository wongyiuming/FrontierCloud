"""Owned fence crash reconciliation and cold snapshots; no activation claim."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_upgrade as upgrade
from tests.test_operator_backup import FakeDocker, populate


class HelperLifetimeTests(unittest.TestCase):
    def fixture(self):
        token = uuid.uuid4().hex
        owner = {'name':'fc-op-helper-'+token,'token':token,'project':'fixture','image':'sha256:'+'a'*64}
        container = {'Name':'/'+owner['name'],'Image':owner['image'],
            'Config':{'Labels':{upgrade.HELPER_LABEL:token,mapping.LABEL+'project':'fixture',mapping.LABEL+'oneoff':'True'}},
            'HostConfig':{'AutoRemove':True},'State':{'Running':True}}
        calls = []
        present = [True]
        def run(arguments,**_options):
            calls.append(arguments)
            if arguments[1] == 'ps':
                return 'b'*64 if present[0] else ''
            if arguments[1] == 'inspect':
                return [container]
            if arguments[1] == 'stop':
                self.assertEqual(arguments[-1],'b'*64)
                present[0] = False
                return arguments[-1]
            raise AssertionError(arguments)
        return owner,container,calls,SimpleNamespace(run=run)

    def test_owned_running_helper_is_stopped_and_joined_without_force_removal(self):
        owner,_container,calls,docker = self.fixture()
        upgrade._join_helper(docker,owner)
        self.assertEqual(sum(row[1] == 'stop' for row in calls),1)
        self.assertFalse(any('rm' in row for row in calls))
        self.assertEqual(calls[-1][1],'ps')

    def test_foreign_or_replaced_helper_is_never_stopped(self):
        for change in ('image','token','auto_remove','oneoff'):
            with self.subTest(change=change):
                owner,container,calls,docker = self.fixture()
                if change == 'image': container['Image'] = 'sha256:'+'c'*64
                if change == 'token': container['Config']['Labels'][upgrade.HELPER_LABEL] = 'foreign'
                if change == 'auto_remove': container['HostConfig']['AutoRemove'] = False
                if change == 'oneoff': container['Config']['Labels'][mapping.LABEL+'oneoff'] = 'False'
                with self.assertRaises(mapping.DeploymentError) as caught:
                    upgrade._join_helper(docker,owner)
                self.assertEqual(caught.exception.code,'helper_identity_changed')
                self.assertFalse(any(row[1] == 'stop' for row in calls))

    def test_stopped_helper_must_finish_removal_before_more_work(self):
        owner,container,calls,docker = self.fixture()
        container['State']['Running'] = False
        with self.assertRaises(mapping.DeploymentError) as caught:
            upgrade._join_helper(docker,owner,timeout=0)
        self.assertEqual(caught.exception.code,'helper_cleanup_pending')
        self.assertFalse(any(row[1] == 'stop' for row in calls))

    def test_auto_removal_between_lookup_and_inspect_is_safe(self):
        owner,_container,_calls,_docker = self.fixture()
        lookups = []
        def run(arguments,**_options):
            if arguments[1] == 'ps':
                lookups.append(True)
                return 'b'*64 if len(lookups) == 1 else ''
            raise backup.BackupError('already removed')
        upgrade._join_helper(SimpleNamespace(run=run),owner)
        self.assertEqual(len(lookups),2)

    @unittest.skipUnless(sys.platform == 'linux','private host ownership is Linux-only')
    def test_resume_only_joins_records_bound_to_this_operation_and_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempt = root/'attempt'
            attempt.mkdir()
            owner,_container,_calls,docker = self.fixture()
            owner.update(version=1,operation=uuid.uuid4().hex)
            prepared = SimpleNamespace(directory=root,docker=docker,receipt={'images':{'web':{'id':owner['image']}}})
            operation = SimpleNamespace(prepared=prepared,state={'id':owner['operation'],'project':owner['project']})
            path = attempt/'helper-owner.json'
            backup.private_write(path,backup.json_bytes(owner))
            with patch.object(upgrade,'_join_helper') as join:
                upgrade.Upgrade._reconcile_helpers(operation)
                join.assert_called_once_with(docker,owner)
            for key,value in [('operation','another-operation'),('image','sha256:'+'f'*64),('token','invalid')]:
                replacement = dict(owner,**{key:value})
                path.write_bytes(backup.json_bytes(replacement))
                with patch.object(upgrade,'_join_helper') as join, self.assertRaises(mapping.DeploymentError) as caught:
                    upgrade.Upgrade._reconcile_helpers(operation)
                self.assertEqual(caught.exception.code,'helper_identity_changed')
                join.assert_not_called()


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0, "root-only host fence ownership and Linux snapshots")
class ColdTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        docker = FakeDocker(self.base, "source")
        self.plan = backup.Plan.load(docker)
        populate(self.plan)
        self.deployment = SimpleNamespace(plan=self.plan, docker=SimpleNamespace(directory=docker.directory))
        self.output = self.base / "prepared"
        self.output.mkdir(mode=0o700)
        self.identifier = uuid.uuid4().hex
        self.receipt = {"id": self.identifier}
        backup.private_write(self.output / "prepared.json", backup.json_bytes(self.receipt))
        self.prepared = SimpleNamespace(directory=self.output, receipt=self.receipt)
        state = upgrade._sealed({"version": 1, "revision": 1, "phase": "images_prepared", "id": self.identifier,
            "project": self.plan.name, "project_directory": str(docker.directory),
            "prepared_sha256": backup.digest(self.output / "prepared.json")[1],
            "roots": {key: upgrade._identity(value.root) for key, value in self.plan.resources.items()},
            "retained_snapshots": [], "retained_fence_material": [], "acceptance_only": True})
        backup.private_write(self.output / "upgrade-state.json", backup.json_bytes(state))
        self.operation = upgrade.Upgrade(self.deployment, self.prepared, state)
        preflight = patch.object(upgrade.Upgrade, '_live_preflight', return_value={
            'node_id': 'a'*32, 'keys': {'node_vault_sha256': 'b'*64}, 'role': 'Standalone',
            'endpoint': '', 'public_key_sha256': 'c'*64, 'generation': 4})
        self.preflight = preflight.start()
        self.addCleanup(preflight.stop)
        reconcile = patch.object(upgrade.Upgrade,'_reconcile_helpers')
        reconcile.start()
        self.addCleanup(reconcile.stop)
        join = patch.object(upgrade,'_join_helper')
        join.start()
        self.addCleanup(join.stop)
        publish = patch.object(upgrade.Upgrade,'_publish_current',return_value='fixture-host-receipt')
        publish.start()
        self.addCleanup(publish.stop)
        self.root = self.plan.resources["control"].root

    def resume(self):
        return upgrade.Upgrade.resume(self.deployment, self.prepared)

    def test_atomic_owned_fence_has_one_link_and_never_reads_media(self):
        opened = os.open
        def guard(path, *args, **kwargs):
            value = Path(path)
            if self.root / "media" in value.parents or self.root / "recordings" in value.parents:
                raise AssertionError("fence publication read payload")
            return opened(path, *args, **kwargs)
        with patch.object(os, "open", guard):
            self.operation._fence()
        marker = self.root / upgrade.HARD
        self.assertEqual(marker.read_bytes(), self.operation._content())
        self.assertEqual((marker.stat().st_nlink, marker.stat().st_uid, marker.stat().st_mode & 0o777), (1, 10001, 0o600))
        self.assertEqual(self.resume().state["phase"], "fenced")
        self.assertFalse((self.root / self.operation.state["fence_temp"]).exists())

    def test_failed_live_preflight_never_publishes_a_fence_or_stops_old_services(self):
        self.preflight.side_effect = mapping.DeploymentError('unfinished_transactions', 'normal recovery required')
        with self.assertRaises(mapping.DeploymentError) as caught:
            self.operation.capture()
        self.assertEqual(caught.exception.code, 'unfinished_transactions')
        self.assertFalse((self.root/upgrade.HARD).exists())
        self.assertFalse(self.plan.docker.stopped)
        self.assertEqual(self.operation.state['phase'], 'images_prepared')

    def test_process_death_after_link_reconciles_the_same_owned_inode(self):
        link = os.link
        def lost(*args, **kwargs):
            link(*args, **kwargs)
            raise RuntimeError("process lost after publication")
        with patch.object(os, "link", lost), self.assertRaisesRegex(RuntimeError, "process lost"):
            self.operation._fence()
        marker = self.root / upgrade.HARD
        inode = marker.stat().st_ino
        self.assertEqual(marker.stat().st_nlink, 2)
        current = self.resume()
        current._fence()
        self.assertEqual((marker.stat().st_ino, marker.stat().st_nlink), (inode, 1))
        current._owned_fence()

    def test_interrupted_temporary_material_is_retained_without_overwriting(self):
        name = ".frontiercloud-fence-" + self.identifier + "-interrupted"
        self.operation._save("fencing", fence_temp=name)
        partial = self.root / name
        backup.private_write(partial, b'{"version":')
        current = self.resume()
        current._fence()
        self.assertEqual(partial.read_bytes(), b'{"version":')
        self.assertEqual(current.state["retained_fence_material"], [str(partial)])
        self.assertNotEqual(current.state["fence_temp"], name)
        current._owned_fence()

    def test_manual_or_foreign_operator_fence_is_never_claimed(self):
        for name in (upgrade.MANUAL, upgrade.HARD):
            marker = self.root / name
            backup.private_write(marker, b"another operator")
            with self.assertRaises(mapping.DeploymentError) as caught:
                self.operation._fence()
            self.assertEqual(caught.exception.code, "maintenance_already_owned")
            self.assertEqual(marker.read_bytes(), b"another operator")
            marker.unlink()

    def test_replaced_fence_and_changed_root_cannot_authorize_more_work(self):
        self.operation._fence()
        marker = self.root / upgrade.HARD
        marker.rename(self.root / "retained-old-fence")
        backup.private_write(marker, self.operation._content())
        os.chown(marker, 10001, 10001)
        with self.assertRaises(mapping.DeploymentError) as caught:
            self.operation._owned_fence()
        self.assertEqual(caught.exception.code, "maintenance_identity_changed")
        self.assertTrue(marker.exists())
        os.chmod(self.root, 0o750)
        with self.assertRaises(mapping.DeploymentError) as caught:
            self.resume()
        self.assertEqual(caught.exception.code, "upgrade_root_changed")

    def test_cold_capture_is_verified_state_only_and_duplicate_does_not_recopy(self):
        result = self.operation.capture()
        self.assertTrue(self.plan.docker.stopped)
        self.assertEqual(result["phase"], "backed_up")
        self.assertFalse(result["media_included"])
        snapshot = Path(result["snapshot"])
        manifest = backup.verify(snapshot)
        self.assertFalse(any(name == "media" or name.startswith("media/") or name == "recordings" or name.startswith("recordings/")
                             for name in manifest["resources"]["control"]["inventory"]))
        with patch.object(backup, "backup", side_effect=AssertionError("duplicate recopied state")):
            self.assertEqual(self.resume().capture(), result)
        self.assertEqual((self.root / "media/music.mp3").read_bytes(), b"not copied media")

    def test_incomplete_backup_is_preserved_and_resume_captures_a_new_snapshot(self):
        def interrupted(plan, output):
            output.mkdir(mode=0o700)
            backup.private_write(output / "partial", b"incomplete state bytes")
            raise OSError("process lost during backup")
        with patch.object(backup, "backup", interrupted), self.assertRaises(OSError):
            self.operation.capture()
        previous = self.operation.state["snapshot"]
        current = self.resume()
        result = current.capture()
        self.assertEqual((self.output / previous / "partial").read_bytes(), b"incomplete state bytes")
        self.assertIn(previous, current.state["retained_snapshots"])
        self.assertNotEqual(Path(result["snapshot"]).name, previous)

    def test_lost_completion_record_reuses_the_same_verified_snapshot(self):
        save = self.operation._save
        def lost(phase, **values):
            if phase == "backed_up":
                raise RuntimeError("process lost before completion record")
            return save(phase, **values)
        with patch.object(self.operation, "_save", lost), self.assertRaises(RuntimeError):
            self.operation.capture()
        previous = self.operation.state["snapshot"]
        self.assertTrue((self.output / previous / "manifest.json").exists())
        with patch.object(backup, "backup", side_effect=AssertionError("verified snapshot copied again")):
            current = self.resume()
            self.assertEqual(Path(current.capture()["snapshot"]).name, previous)

    def test_corrupt_completed_snapshot_cannot_be_replaced_with_a_green_claim(self):
        result = self.operation.capture()
        archive = Path(result["snapshot"]) / "control.tar"
        with archive.open("ab") as target:
            target.write(b"corrupt")
        with self.assertRaisesRegex(backup.BackupError, "checksum"):
            self.resume().capture()
        self.assertTrue((self.root / upgrade.HARD).exists())
        self.assertTrue(self.plan.docker.stopped)

    def test_old_revision_cannot_overwrite_a_newer_state(self):
        stale = self.resume()
        self.operation._save("fencing", fence_temp=".frontiercloud-fence-" + self.identifier)
        with self.assertRaises(mapping.DeploymentError) as caught:
            stale._save("fencing", fence_temp="wrong")
        self.assertEqual(caught.exception.code, "upgrade_state_changed")

    def database_fixture(self, **receipt_values):
        self.prepared.config = copy.deepcopy(self.plan.config)
        self.deployment.inputs = []
        self.deployment.docker.env_files = []
        stage = self.base/'stage'
        stage.mkdir()
        for filename in ('compose.json', 'old-compose.json'):
            backup.private_write(self.output/filename, backup.json_bytes(self.prepared.config))
        self.receipt.update(configuration_sha256=mapping.checksum(self.prepared.config), stage_directory=str(stage),
            old_deployment={'input_sha256': mapping.checksum([])},
            source_stage_sha256='fixture-source-hash',
            compose_sha256=backup.digest(self.output/'compose.json')[1],
            old_compose_sha256=backup.digest(self.output/'old-compose.json')[1])
        self.receipt.update(receipt_values)
        replacement = self.output/'updated-prepared.json'
        backup.private_write(replacement, backup.json_bytes(self.receipt))
        os.replace(replacement, self.output/'prepared.json')
        self.operation._save('images_prepared', prepared_sha256=backup.digest(self.output/'prepared.json')[1])
        self.operation.capture()
        return lambda _stage: {'stage_sha256': 'fixture-source-hash'}

    def test_database_baseline_is_durable_before_ddl_and_lost_reply_resumes(self):
        verify = self.database_fixture()
        inspection = {'version': 1, 'database': 'sqlite', 'generation': 4, 'node_id': 'a'*32,
            'keys': {'node_vault_sha256': 'b'*64}, 'tables': {'node_identity': {}, 'frontiercloud_schema': {}},
            'role': 'Standalone', 'endpoint': '', 'public_key_sha256': 'c'*64}
        def execute(_docker, _arguments, output, step, **_options):
            if step == 'database-inspect':
                value = inspection
            else:
                self.assertEqual(upgrade._read(self.operation.path)['phase'], 'database_preparing')
                baseline = Path(self.operation.state['database_baseline'])
                self.assertEqual(json.loads(baseline.read_text()), inspection)
                self.assertEqual((baseline.stat().st_uid, baseline.stat().st_mode & 0o777), (10001, 0o600))
                value = {'phase': 'database_prepared', 'from_generation': 4, 'generation': 7, 'node_id': 'a'*32,
                    'baseline_sha256': backup.digest(baseline)[1], 'original_state_preserved': True,
                    'business_started': False, 'original_tables': 2}
            backup.private_write(output/(step+'.log'), b'Container fixture Creating\n' + backup.json_bytes(value) + b'\nContainer fixture Removed\n')
        original = self.operation._save
        def lost(phase, **values):
            if phase == 'database_prepared':
                raise RuntimeError('host lost after successful helper')
            original(phase, **values)
        with patch.object(self.operation, '_save', lost), self.assertRaisesRegex(RuntimeError, 'host lost'):
            self.operation.prepare_database(verify=verify, execute=execute)
        self.assertEqual(self.operation.state['phase'], 'database_preparing')
        baseline_hash = self.operation.state['database_baseline_sha256']
        current = self.resume()
        result = current.prepare_database(verify=verify, execute=execute)
        self.assertEqual(result['generation'], 7)
        self.assertEqual(current.state['database_baseline_sha256'], baseline_hash)
        self.assertTrue((self.root/upgrade.HARD).exists())

    def test_candidate_change_after_capture_refuses_before_database_helper(self):
        verify = self.database_fixture()
        with (self.output/'compose.json').open('ab') as stream:
            stream.write(b' ')
        def refuse(*_arguments, **_options):
            raise AssertionError('changed inputs executed a helper')
        with self.assertRaises(mapping.DeploymentError) as caught:
            self.operation.prepare_database(verify=verify, execute=refuse)
        self.assertEqual(caught.exception.code, 'prepared_compose_changed')
        self.assertEqual(self.operation.state['phase'], 'backed_up')

    def test_new_private_inputs_after_capture_refuse_before_database_helper(self):
        verify = self.database_fixture()
        (self.deployment.docker.directory/'.env').write_text('CHANGED=owner-new-input\n')
        def refuse(*_arguments, **_options):
            raise AssertionError('changed inputs executed a helper')
        with self.assertRaises(mapping.DeploymentError) as caught:
            self.operation.prepare_database(verify=verify, execute=refuse)
        self.assertEqual(caught.exception.code, 'deployment_inputs_changed')
        self.assertTrue((self.root/upgrade.HARD).exists())

    def activation_fixture(self):
        verify = self.database_fixture(images={'web':{'id':'sha256:'+'a'*64}},provenance={'revision':'f'*40})
        self.prepared.docker = SimpleNamespace(base=['compose','fixture'])
        directory = self.output/'inspection'
        directory.mkdir(mode=0o700)
        baseline = directory/'baseline.json'
        backup.private_write(baseline,b'{"original":"rows"}')
        baseline_hash = backup.digest(baseline)[1]
        self.operation._save('database_prepared',
            database_baseline=str(baseline),database_baseline_sha256=baseline_hash,database_node_id='a'*32)
        current = SimpleNamespace(plan=self.plan,observed={'web':{'image_id':'sha256:'+'a'*64}},
            summary=lambda:{'same':'target'})
        result = {'phase':'fenced_verified','node_id':'a'*32,'generation':7,'baseline_sha256':baseline_hash,
            'original_state_preserved':True,'business_opened':False}
        return verify,current,result

    def test_activation_starts_complete_package_and_duplicate_only_reverifies(self):
        verify,current,result = self.activation_fixture()
        calls = []
        def execute(_docker,arguments,_output,step,**_options):
            self.assertEqual(upgrade._read(self.operation.path)['phase'],'activating')
            calls.append((arguments,step))
        with patch.object(mapping.Deployment,'discover',return_value=current), \
                patch.object(backup.Plan,'load',return_value=self.plan), \
                patch.object(self.operation,'_database_helper',return_value=result) as helper:
            first = self.operation.activate_fenced(verify=verify,execute=execute)
            second = self.operation.activate_fenced(verify=verify,execute=execute)
        self.assertEqual(first,second)
        self.assertEqual(len(calls),1)
        self.assertIn('--force-recreate',calls[0][0])
        self.assertEqual(calls[0][0][-1],'120')
        self.assertEqual(helper.call_count,2)
        self.assertEqual(first['phase'],'active_fenced')
        self.assertFalse(first['upgrade_complete'])
        self.operation._owned_fence()

    def test_lost_activation_reply_reconciles_and_never_opens_business(self):
        verify,current,result = self.activation_fixture()
        calls = []
        def execute(*_args,**_options):
            calls.append('start')
        save = self.operation._save
        def lost(phase,**values):
            if phase == 'active_fenced':
                raise RuntimeError('host lost final activation reply')
            return save(phase,**values)
        with patch.object(mapping.Deployment,'discover',return_value=current), \
                patch.object(self.operation,'_database_helper',return_value=result), \
                patch.object(self.operation,'_save',lost), self.assertRaisesRegex(RuntimeError,'lost final'):
            self.operation.activate_fenced(verify=verify,execute=execute)
        self.assertEqual(self.operation.state['phase'],'activating')
        current_operation = self.resume()
        with patch.object(mapping.Deployment,'discover',return_value=current), \
                patch.object(backup.Plan,'load',return_value=self.plan), \
                patch.object(current_operation,'_database_helper',return_value=result):
            current_operation.activate_fenced(verify=verify,execute=execute)
        self.assertEqual(len(calls),2)
        current_operation._owned_fence()

    def test_changed_original_rows_during_target_start_retain_owned_fence(self):
        verify,current,result = self.activation_fixture()
        result['original_state_preserved'] = False
        with patch.object(mapping.Deployment,'discover',return_value=current), \
                patch.object(self.operation,'_database_helper',return_value=result), \
                self.assertRaises(mapping.DeploymentError) as caught:
            self.operation.activate_fenced(verify=verify,execute=lambda *_args,**_options:None)
        self.assertEqual(caught.exception.code,'activation_verification_failed')
        self.assertEqual(self.operation.state['phase'],'activating')
        self.operation._owned_fence()

    def opening_fixture(self):
        verify,current,_result = self.activation_fixture()
        current.operator_phase = 'opening'
        self.operation._save('active_fenced',activation={'generation':7})
        def probe(fenced,_execute):
            self.assertEqual((self.root/upgrade.HARD).exists(),fenced)
            return {'phase':'endpoint_fenced' if fenced else 'endpoint_open','generation':7,
                'node_id':'a'*32,'business_opened':not fenced,'media_read_verified':False}
        return verify,current,probe

    def test_opening_joins_web_before_unlink_and_duplicate_preserves_new_writes_and_manual_fence(self):
        verify,current,probe = self.opening_fixture()
        calls = []
        def execute(_docker,arguments,_output,step,**_options):
            calls.append(step)
            if step == 'business-stop':
                self.operation._owned_fence()
            else:
                self.assertEqual(upgrade._read(self.operation.path)['phase'],'opening')
                self.assertTrue(upgrade._read(self.operation.path)['business_may_have_written'])
                self.assertFalse((self.root/upgrade.HARD).exists())
                (self.root/'media/new-after-open.bin').write_bytes(b'new business data')
                self.assertNotIn('--force-recreate',arguments)
        with patch.object(mapping.Deployment,'discover',return_value=current),patch.object(backup.Plan,'load',return_value=self.plan), \
                patch.object(self.operation,'_probe',side_effect=probe):
            result = self.operation.open(verify=verify,execute=execute)
        self.assertTrue(result['upgrade_complete'])
        self.assertEqual(self.operation.state['phase'],'opened')
        marker = self.root/upgrade.MANUAL
        backup.private_write(marker,b'owner manual maintenance')
        with patch.object(self.operation,'_prepared_inputs',side_effect=AssertionError('completed operation reentered upgrade')), \
                patch.object(backup,'restore',side_effect=AssertionError('new writes were restored over')):
            self.assertEqual(self.operation.open(verify=verify,execute=execute),result)
        self.assertEqual((self.root/'media/new-after-open.bin').read_bytes(),b'new business data')
        self.assertEqual(marker.read_bytes(),b'owner manual maintenance')
        self.assertEqual(calls.count('business-start'),1)

    def test_lost_opening_completion_resumes_without_overwriting_post_open_data(self):
        verify,current,probe = self.opening_fixture()
        payload = self.root/'media/post-open.bin'
        def execute(_docker,_arguments,_output,step,**_options):
            if step == 'business-start':
                if payload.exists():self.assertEqual(payload.read_bytes(),b'retained new bytes')
                else:payload.write_bytes(b'retained new bytes')
        save = self.operation._save
        def lost(phase,**values):
            if phase == 'opened':raise RuntimeError('lost opening completion')
            return save(phase,**values)
        with patch.object(mapping.Deployment,'discover',return_value=current),patch.object(backup.Plan,'load',return_value=self.plan), \
                patch.object(self.operation,'_probe',side_effect=probe),patch.object(self.operation,'_save',lost), \
                self.assertRaisesRegex(RuntimeError,'lost opening completion'):
            self.operation.open(verify=verify,execute=execute)
        self.assertEqual(self.operation.state['phase'],'opening')
        self.assertFalse((self.root/upgrade.HARD).exists())
        resumed = self.resume()
        with patch.object(mapping.Deployment,'discover',return_value=current),patch.object(resumed,'_probe',side_effect=probe):
            result = resumed.open(verify=verify,execute=execute)
        self.assertEqual(result['phase'],'opened')
        self.assertEqual(payload.read_bytes(),b'retained new bytes')

    def test_endpoint_refusal_keeps_original_owned_fence_and_never_stops_business(self):
        verify,_current,_probe = self.opening_fixture()
        with patch.object(self.operation,'_probe',side_effect=mapping.DeploymentError('probe_certificate_untrusted','refused')), \
                self.assertRaises(mapping.DeploymentError) as caught:
            self.operation.open(verify=verify,execute=lambda *_args,**_options:self.fail('refused probe executed a command'))
        self.assertEqual(caught.exception.code,'probe_certificate_untrusted')
        self.assertEqual(self.operation.state['phase'],'active_fenced')
        self.operation._owned_fence()
