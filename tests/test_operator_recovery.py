"""Controlled old-state restore keeps failed roots and never copies payloads."""
from contextlib import closing
import copy
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_recovery as recovery
from scripts import operator_upgrade as upgrade
from tests.test_operator_backup import FakeDocker, populate


@unittest.skipUnless(sys.platform == 'linux' and os.geteuid() == 0,'root-only Linux recovery ownership')
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.docker = FakeDocker(self.base,'source')
        self.docker.config_value['services'].pop('mysql')
        for number,(name,value) in enumerate(self.docker.images.items(),1):
            value['id'] = 'sha256:'+str(number)*64
        plan = backup.Plan.load(self.docker)
        populate(plan)
        self.original = plan.resources['control'].root
        self.payload_inode = (self.original/'media/music.mp3').stat().st_ino
        self.output = self.base/'prepared'
        self.output.mkdir(mode=0o700)
        self.identifier = uuid.uuid4().hex
        self.receipt = {'id':self.identifier,'old_deployment':{'services':{
            name:{'image_id':self.docker.images[definition['image']]['id']}
            for name,definition in plan.config['services'].items()}}}
        backup.private_write(self.output/'prepared.json',backup.json_bytes(self.receipt))
        self.prepared = SimpleNamespace(directory=self.output,receipt=self.receipt,docker=self.docker,
            config=copy.deepcopy(plan.config))
        self.prepared.config['services']['media-init'] = {'image':self.docker.images['web']['id']}
        self.deployment = SimpleNamespace(plan=plan,docker=self.docker)
        state = upgrade._sealed({'version':1,'revision':1,'phase':'images_prepared','id':self.identifier,
            'project':plan.name,'project_directory':str(self.docker.directory),
            'prepared_sha256':backup.digest(self.output/'prepared.json')[1],
            'roots':{key:upgrade._identity(resource.root) for key,resource in plan.resources.items()},
            'retained_snapshots':[],'retained_fence_material':[],'acceptance_only':True})
        backup.private_write(self.output/'upgrade-state.json',backup.json_bytes(state))
        self.operation = upgrade.Upgrade(self.deployment,self.prepared,state)
        for name,options in [('_live_preflight',{'return_value':{}}),('_reconcile_helpers',{}),('_prepared_inputs',{})]:
            mocked = patch.object(upgrade.Upgrade,name,**options)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.operation.capture()
        self.operation._save('database_prepared')
        with closing(sqlite3.connect(self.original/'frontiercloud.db')) as connection:
            connection.execute('UPDATE schema_version SET generation=7')
            connection.execute("UPDATE parent SET name='failed candidate'")
            connection.commit()
        owner = self
        class Docker:
            def __init__(self,directory,files,name,env_files):
                self.directory,self.files,self.name,self.env_files = directory,files,name,env_files
                self.config_value = backup.compose_values(backup.bounded_json(files[0]))
            def config(self):return copy.deepcopy(self.config_value)
            def local_root(self):return owner.docker.root
            def containers(self):return owner.docker.containers()
            def volume(self,name):return owner.docker.volume(name)
            def image_id(self,name):
                return next(value for value in owner.docker.images.values() if value['id'] == name)
            def run(self,arguments):
                owner.assertEqual(arguments[:3],['docker','volume','create'])
                labels = dict(arguments[index+1].split('=',1) for index,value in enumerate(arguments) if value == '--label')
                name = arguments[-1]
                root = owner.docker.root/'volumes'/name/'_data'
                root.mkdir(parents=True)
                owner.docker.volumes[name] = {'Name':name,'Driver':'local','Options':None,
                    'Mountpoint':str(root),'Labels':labels}
        self.factory = Docker

    def run_recovery(self):
        self.operation = upgrade.Upgrade.resume(self.deployment,self.prepared)
        return recovery.Recovery(self.operation,docker_factory=self.factory).prepare()

    def generation(self,root):
        with closing(sqlite3.connect(root/'frontiercloud.db')) as connection:
            return connection.execute('SELECT generation FROM schema_version').fetchone()[0]

    def test_restored_state_and_payload_inodes_are_separate_from_failed_control_and_volumes(self):
        original_volumes = set(self.docker.volumes)
        result = self.run_recovery()
        target = Path(result['control_root'])
        self.assertEqual(self.generation(target),4)
        self.assertEqual(self.generation(self.original),7)
        self.assertEqual((target/'media/music.mp3').read_bytes(),b'not copied media')
        self.assertEqual((target/'media/music.mp3').stat().st_ino,self.payload_inode)
        self.assertFalse((self.original/'media').exists())
        self.assertTrue(original_volumes < set(self.docker.volumes))
        self.assertTrue(all(Path(self.docker.volumes[name]['Mountpoint']).exists() for name in original_volumes))
        self.assertTrue((self.original/upgrade.HARD).exists())
        self.assertTrue((target/upgrade.HARD).exists())
        self.assertFalse(result['services_started'])
        self.assertFalse(result['recovery_complete'])
        attempts = copy.deepcopy(self.operation.state['recovery'])
        self.assertEqual(self.run_recovery(),result)
        self.assertEqual(self.operation.state['recovery'],attempts)
        opened = {'phase':'restored_opened','recovery_complete':True}
        with patch.object(upgrade.Upgrade,'load',return_value=self.operation),patch.object(recovery.Recovery,'open',return_value=opened) as opening:
            self.assertEqual(upgrade.Upgrade.advance(self.docker.directory),opened)
        opening.assert_called_once()

    def test_host_death_after_rename_reconciles_intent_without_recopy_or_new_restore(self):
        rename = recovery.rename_noreplace
        def lost(*arguments):
            rename(*arguments)
            raise RuntimeError('host died after rename')
        with patch.object(recovery,'rename_noreplace',side_effect=lost),self.assertRaisesRegex(RuntimeError,'host died'):
            self.run_recovery()
        state = self.operation.state['recovery']
        self.assertEqual(state['stage'],'moving')
        self.assertIn('intent',state['moves'].values())
        attempt = state['attempt']['id']
        result = self.run_recovery()
        self.assertEqual(self.operation.state['recovery']['attempt']['id'],attempt)
        self.assertEqual(self.operation.state['recovery']['retained_attempts'],[])
        self.assertEqual((Path(result['control_root'])/'media/music.mp3').stat().st_ino,self.payload_inode)

    def test_interrupted_state_restore_is_retained_and_retried_only_into_new_empty_destinations(self):
        restore = backup.restore
        def lost(*arguments):
            restore(*arguments)
            raise RuntimeError('lost restore completion')
        with patch.object(backup,'restore',side_effect=lost),self.assertRaisesRegex(RuntimeError,'lost restore'):
            self.run_recovery()
        previous = copy.deepcopy(self.operation.state['recovery']['attempt'])
        self.assertTrue((self.original/'media/music.mp3').exists())
        result = self.run_recovery()
        self.assertEqual(self.operation.state['recovery']['retained_attempts'],[previous])
        self.assertNotEqual(result['control_root'],previous['control'])
        self.assertEqual(self.generation(Path(previous['control'])),4)
        self.assertEqual(self.generation(Path(result['control_root'])),4)

    def test_unexpected_payload_or_manual_fence_is_never_overwritten(self):
        with patch.object(recovery.Recovery,'_move_payloads',side_effect=RuntimeError('before moves')):
            with self.assertRaisesRegex(RuntimeError,'before moves'):self.run_recovery()
        target = Path(self.operation.state['recovery']['attempt']['control'])
        (target/'media').mkdir()
        sentinel = target/'media/foreign'
        sentinel.write_bytes(b'keep')
        with self.assertRaises(mapping.DeploymentError) as caught:self.run_recovery()
        self.assertEqual(caught.exception.code,'recovery_payload_changed')
        self.assertEqual(sentinel.read_bytes(),b'keep')
        self.assertEqual((self.original/'media/music.mp3').stat().st_ino,self.payload_inode)
        backup.private_write(self.original/upgrade.MANUAL,b'manual')
        with self.assertRaises(mapping.DeploymentError) as caught:self.run_recovery()
        self.assertEqual(caught.exception.code,'manual_maintenance_active')
        self.assertEqual((self.original/upgrade.MANUAL).read_bytes(),b'manual')

    def test_post_open_restore_is_refused_before_stopping_or_creating_anything(self):
        self.operation._save('opening',business_may_have_written=True)
        before = set(self.docker.volumes)
        with patch.object(self.docker,'stop') as stop,self.assertRaises(mapping.DeploymentError) as caught:
            self.run_recovery()
        self.assertEqual(caught.exception.code,'recovery_after_open_refused')
        stop.assert_not_called()
        self.assertEqual(set(self.docker.volumes),before)
        self.assertTrue((self.original/'media/music.mp3').exists())

    def test_no_replace_rename_preserves_a_competing_empty_directory(self):
        source,destination = self.base/'source-payload',self.base/'foreign-target'
        source.mkdir()
        destination.mkdir()
        before = destination.stat().st_ino
        with self.assertRaises(FileExistsError):recovery.rename_noreplace(source,destination)
        self.assertEqual(destination.stat().st_ino,before)
        self.assertTrue(source.exists())

    def test_changed_restored_root_or_volume_owner_blocks_duplicate_without_removing_material(self):
        result = self.run_recovery()
        transaction = recovery.Recovery(self.operation,docker_factory=self.factory)
        plan = transaction._plan()
        resource = plan.resources['secrets']
        value = self.docker.volumes[resource.source]
        value['Labels'][recovery.LABEL] = 'foreign'
        with self.assertRaises(mapping.DeploymentError) as caught:self.run_recovery()
        self.assertEqual(caught.exception.code,'recovery_volume_changed')
        self.assertTrue(resource.root.exists())
        target = Path(result['control_root'])
        self.assertEqual((target/'media/music.mp3').stat().st_ino,self.payload_inode)

    def test_running_declared_or_foreign_writers_refuse_before_payload_moves(self):
        for own in (True,False):
            with self.subTest(own=own):
                container = {'Id':'a'*64,'Config':{'Labels':{
                    mapping.LABEL+'project':'source' if own else 'another-project',
                    mapping.LABEL+'service':'media-init'}},'State':{'Running':True},
                    'Mounts':[{'Type':'bind','Source':str(self.original),'Destination':'/app/data'}]}
                self.docker.containers_value = [container]
                with patch.object(self.docker,'stop'),self.assertRaises(backup.BackupError):self.run_recovery()
                self.assertTrue((self.original/'media/music.mp3').exists())
                self.assertEqual(self.operation.state['recovery']['stage'],'stopping')
                self.docker.containers_value = []

    def test_bind_mount_inside_control_is_rejected_even_on_the_same_device(self):
        inventory = self.base/'mountinfo'
        path = self.original/'media'
        inventory.write_text('100 20 8:1 / '+str(path)+' rw,relatime - ext4 /dev/test rw\n')
        with self.assertRaises(mapping.DeploymentError) as caught:
            recovery.no_nested_mounts(self.original,inventory=inventory)
        self.assertEqual(caught.exception.code,'recovery_nested_mount')
