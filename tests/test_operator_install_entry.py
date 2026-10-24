"""Installation bootstrap keeps published-source and immutable retry boundaries."""
import hashlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_entry as entry
from scripts import operator_install_entry as install
from scripts import operator_install_start as startup
from scripts import operator_registry as registry
from tests import test_operator_handoff as fixtures


@unittest.skipUnless(sys.platform=='linux','private host source handoff')
class InstallEntryTests(unittest.TestCase):
    def setUp(self):
        fixture=fixtures.HandoffTests('runTest');fixture.setUp();self.fixture=fixture
        self.addCleanup(fixture.doCleanups)
        self.stage=fixture.root/'install-stage';fixture.create_stage(self.stage,install=True)
        self.checked=entry._stage(self.stage)

    def indexed(self, *, phase='future_install_phase',storage=True):
        fixture=self.fixture;folder=fixture.root/'installed-package';folder.mkdir(mode=0o700)
        receipt={'version':1,'phase':'install_package_prepared','id':'b'*32,'project':'fixture',
            'project_directory':str(fixture.directory),'stage_directory':str(self.stage),
            'source_stage_sha256':self.checked['stage_sha256'],'provenance':self.checked['provenance'],
            'fresh_resources':{'role':'storage' if storage else 'business'}}
        backup.private_write(folder/'install-package.json',backup.json_bytes(receipt))
        digest=backup.digest(folder/'install-package.json')[1]
        state={'version':1,'revision':1,'phase':phase,'id':receipt['id'],'project':'fixture',
            'project_directory':str(fixture.directory),'directory_identity':registry.root_identity(fixture.directory),
            'package_sha256':digest,'business_may_have_written':phase=='ready'}
        if phase=='ready':
            state['identity']={'node_id':'c'*32,'generation':7}
            state['install_result']={'phase':'ready','project':'fixture','node_id':'c'*32,'generation':7,'installation_complete':True}
        backup.private_write(folder/'install-state.json',backup.json_bytes(registry.sealed(state)))
        reference=registry.sealed({'version':1,'phase':'installation_operation','id':receipt['id'],'project':'fixture',
            'project_directory':str(fixture.directory),'package_directory':str(folder),'package_sha256':digest})
        backup.private_write(fixture.directory/install.packages.INSTALLATION,backup.json_bytes(reference))
        return reference,receipt,state

    def request(self):return registry.private_json(Path(self.fixture.calls[-1]['arguments'][-1]))

    def continuation(self, *, intent='new_install',command='install'):
        fixture=self.fixture;parent=fixture.root/'continuation';parent.mkdir(mode=0o700)
        fixture.cache.mkdir(mode=0o700)
        cache=fixture.cache/hashlib.sha256(str(fixture.directory).encode()).hexdigest();cache.mkdir(mode=0o700)
        descriptor=entry._lock(cache/'entry.lock')
        reference=fixture.directory/install.packages.INSTALLATION
        value=registry.sealed({'version':1,'phase':'host_handoff','project_directory':str(fixture.directory),
            'directory_identity':registry.root_identity(fixture.directory),'stage_directory':str(self.stage),
            'source_stage_sha256':self.checked['stage_sha256'],'command':command,'intent':intent,'storage':True,
            'previous_operation_sha256':None,'previous_installation_sha256':backup.digest(reference)[1] if reference.exists() else None,
            'host_root':str(fixture.cache),'lock_path':str(cache/'entry.lock'),'lock_fd':descriptor})
        path=parent/'request.json';backup.private_write(path,backup.json_bytes(value))
        return path,descriptor

    def test_first_install_selects_source_without_live_discovery_or_user_material(self):
        fixture=self.fixture
        with fixture.patches(),patch.object(entry.sources,'stage',side_effect=lambda path,**_:fixture.create_stage(path,install=True)),\
                patch.object(mapping.Deployment,'discover') as discovery:
            with self.assertRaises(fixtures.Executed):entry.start(fixture.directory,command='install',host_root=fixture.cache)
        discovery.assert_not_called();request=self.request()
        self.assertEqual((request['command'],request['intent'],request['storage']),('install','new_install',False))
        self.assertIsNone(request['previous_installation_sha256']);self.assertIsNone(request['old_deployment'])
        self.assertNotIn('acceptance',request);self.assertNotIn('node_id',request)

    def test_old_operator_protocol_cannot_substitute_for_install_support(self):
        fixture=self.fixture
        with fixture.patches(),self.assertRaises(mapping.DeploymentError) as caught:
            entry.start(fixture.directory,command='install',host_root=fixture.cache)
        self.assertEqual(caught.exception.code,'source_install_missing');self.assertEqual(fixture.calls,[])
        self.assertFalse((fixture.directory/install.packages.INSTALLATION).exists())

    def test_pending_install_uses_same_source_and_recognizes_storage_without_repeating_flag(self):
        fixture=self.fixture;self.indexed()
        with fixture.patches(),patch.object(entry.releases,'select_latest') as latest,\
                patch.object(mapping.Deployment,'discover') as discovery,patch.object(entry.sources,'stage') as stage:
            with self.assertRaises(fixtures.Executed):entry.start(fixture.directory,command='install',host_root=fixture.cache)
        latest.assert_not_called();discovery.assert_not_called();stage.assert_not_called()
        request=self.request();self.assertEqual(request['intent'],'resume_install');self.assertTrue(request['storage'])
        self.assertEqual(request['stage_directory'],str(self.stage))
        self.assertEqual(request['previous_installation_sha256'],backup.digest(fixture.directory/install.packages.INSTALLATION)[1])

    def test_role_cannot_change_when_resuming_the_same_installation(self):
        fixture=self.fixture;self.indexed(storage=False)
        with fixture.patches(),self.assertRaises(mapping.DeploymentError) as caught:
            entry.start(fixture.directory,command='install',storage=True,host_root=fixture.cache)
        self.assertEqual(caught.exception.code,'install_role_changed');self.assertEqual(fixture.calls,[])

    def test_upgrade_cannot_adopt_unfinished_installation(self):
        fixture=self.fixture;self.indexed()
        with fixture.patches(),patch.object(mapping.Deployment,'discover') as discovery,self.assertRaises(mapping.DeploymentError) as caught:
            entry.start(fixture.directory,host_root=fixture.cache)
        self.assertEqual(caught.exception.code,'installation_incomplete');discovery.assert_not_called()

    def test_ready_installation_executes_retained_source_before_discovery(self):
        fixture=self.fixture;self.indexed(phase='ready')
        with fixture.patches(),patch.object(mapping.Deployment,'discover') as discovery,patch.object(entry.releases,'select_latest') as latest:
            with self.assertRaises(fixtures.Executed):entry.start(fixture.directory,host_root=fixture.cache)
        latest.assert_not_called();discovery.assert_not_called()
        self.assertEqual(self.request()['intent'],'select_install_upgrade')

    def test_ready_same_release_is_a_noop_from_its_retained_source(self):
        fixture=self.fixture;self.indexed(phase='ready')
        fixture.deployment.operator_phase='ready';fixture.deployment.observed={'web':{'running':True},'secrets-init':{'running':False}}
        with fixture.patches(),patch.object(entry,'__file__',str(self.stage/'source/scripts/operator_entry.py')),\
                patch.object(registry,'read',return_value=({'origin':'source_install'},{'provenance':fixture.proof})):
            result=entry.start(fixture.directory,host_root=fixture.cache)
        self.assertEqual(result['state'],'already_current');self.assertFalse(result['deployment_changed']);self.assertEqual(fixture.calls,[])

    def test_malformed_ready_result_refuses_instead_of_skipping_installation(self):
        state=self.indexed(phase='ready')[2];del state['install_result']['node_id'];del state['identity']['node_id']
        with self.assertRaises(mapping.DeploymentError) as caught:install.complete(state)
        self.assertEqual(caught.exception.code,'install_completion_invalid')

    def test_corrupt_package_binding_is_preserved_and_never_executes(self):
        fixture=self.fixture;reference,receipt,state=self.indexed()
        path=Path(reference['package_directory'])/'install-package.json';path.write_bytes(b'{}')
        with fixture.patches(),self.assertRaises(mapping.DeploymentError) as caught:
            entry.start(fixture.directory,command='install',host_root=fixture.cache)
        self.assertEqual(caught.exception.code,'install_reference_changed');self.assertEqual(path.read_bytes(),b'{}');self.assertEqual(fixture.calls,[])

    def test_private_continuation_prepares_normal_package_without_fixture_admission(self):
        fixture=self.fixture;package=SimpleNamespace();operation=SimpleNamespace(start=lambda:{'installation_complete':True})
        request={'stage_directory':str(self.stage),'storage':False,'intent':'new_install'};path=fixture.root/'request.json'
        with patch.object(install.packages,'prepare',return_value=package) as prepare,patch.object(startup.Installation,'begin',return_value=operation) as begin:
            self.assertTrue(install.continue_install(fixture.directory,path,request,self.checked)['installation_complete'])
        prepare.assert_called_once_with(fixture.directory,self.stage,path.parent/'installed',storage=False);begin.assert_called_once_with(package)

    def test_resume_loads_bound_record_without_rebuilding_or_selecting_a_version(self):
        fixture=self.fixture;receipt=self.indexed()[1]
        operation=SimpleNamespace(package=SimpleNamespace(receipt=receipt),start=lambda:{'installation_complete':True})
        request={'stage_directory':str(self.stage),'storage':True,'intent':'resume_install'}
        with patch.object(startup.Installation,'load',return_value=operation) as load,patch.object(install.packages,'prepare') as prepare:
            install.continue_install(fixture.directory,fixture.root/'request.json',request,self.checked)
        load.assert_called_once_with(fixture.directory);prepare.assert_not_called()

    def test_install_reference_appearing_across_exec_blocks_preparation(self):
        fixture=self.fixture;path,descriptor=self.continuation();self.indexed()
        with patch.object(entry,'__file__',str(self.stage/'source/scripts/operator_entry.py')),\
                patch.object(install,'continue_install') as continuation,self.assertRaises(mapping.DeploymentError) as caught:
            entry.continue_upgrade(path)
        self.assertEqual(caught.exception.code,'operator_handoff_installation_changed');continuation.assert_not_called()
        with self.assertRaises(OSError):os.fstat(descriptor)

    def test_private_install_handoff_keeps_source_and_closes_its_inherited_lock(self):
        fixture=self.fixture;self.indexed();path,descriptor=self.continuation(intent='resume_install')
        result={'phase':'ready','installation_complete':True}
        with patch.object(entry,'__file__',str(self.stage/'source/scripts/operator_entry.py')),\
                patch.object(install,'continue_install',return_value=result) as continuation:
            self.assertEqual(entry.continue_upgrade(path),result)
        continuation.assert_called_once_with(fixture.directory,path,registry.private_json(path),self.checked)
        self.assertTrue((path.parent/'handoff-started.json').exists())
        with self.assertRaises(OSError):os.fstat(descriptor)

    def test_installed_version_selection_preserves_the_same_locked_descriptor(self):
        fixture=self.fixture;self.indexed(phase='ready');path,descriptor=self.continuation(intent='select_install_upgrade',command='upgrade')
        def select(directory,command,host_root,cache,fd,docker):
            self.assertEqual((directory,command,host_root,fd),(fixture.directory,'upgrade',fixture.cache,descriptor))
            self.assertFalse(os.get_inheritable(fd));os.fstat(fd)
            with self.assertRaises(mapping.DeploymentError) as caught:entry._lock(cache/'entry.lock')
            self.assertEqual(caught.exception.code,'operator_entry_busy')
            return {'state':'already_current'}
        with patch.object(entry,'__file__',str(self.stage/'source/scripts/operator_entry.py')),\
                patch.object(mapping,'ComposeDocker',return_value=fixture.docker),patch.object(entry,'_start_locked',side_effect=select):
            self.assertEqual(entry.continue_upgrade(path),{'state':'already_current'})
        with self.assertRaises(OSError):os.fstat(descriptor)
