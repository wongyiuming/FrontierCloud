"""Fresh resource intents survive lost replies without claiming an upgrade."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_install_package as packages
from scripts import operator_install_resources as resources
from scripts import operator_registry as registry
from tests import test_operator_install_package as fixtures


@unittest.skipUnless(sys.platform=='linux','Linux private resource journal and no-replace publication')
class ResourceTests(unittest.TestCase):
    def setUp(self):
        fixture=fixtures.InstallPackageTests('runTest');fixture.setUp();self.fixture=fixture
        self.addCleanup(fixture.doCleanups)
        self.package=fixture.prepare()
        (fixture.root/'docker').mkdir()
        parent=fixture.Docker;outer=self
        self.lost_volume_reply=False
        class Docker(parent):
            def run(self,arguments, *, json_output=False):
                if arguments[:3]==['docker','volume','create']:
                    name=arguments[-1];labels={}
                    for index,argument in enumerate(arguments):
                        if argument=='--label':
                            key,value=arguments[index+1].split('=',1);labels[key]=value
                    root=fixture.root/'docker/volumes'/name/'_data';root.mkdir(parents=True)
                    fixture.volumes[name]={'Name':name,'Driver':'local','Options':None,'Labels':labels,'Mountpoint':str(root),'owned':True}
                    if outer.lost_volume_reply:
                        outer.lost_volume_reply=False
                        raise OSError('fixture lost successful volume-create reply')
                    return name
                return super().run(arguments,json_output=json_output)
        self.Docker=Docker

    def begin(self):
        with patch.object(mapping,'ComposeDocker',self.Docker):
            return resources.Installation.begin(self.package,verify=lambda _:self.fixture.receipt,
                public_verify=lambda:self.fixture.receipt['provenance'])

    def load(self):
        with patch.object(mapping,'ComposeDocker',self.Docker):
            return resources.Installation.load(self.fixture.directory,verify=lambda _:self.fixture.receipt)

    def provision(self,operation):
        with patch.object(mapping,'ComposeDocker',self.Docker):return operation.resources()

    def test_explicit_install_reference_and_resources_never_start_services_or_forge_old_state(self):
        operation=self.begin();result=self.provision(operation)
        self.assertEqual(result['phase'],'resources_prepared')
        self.assertFalse(result['installation_complete']);self.assertFalse(result['services_started'])
        self.assertEqual(set(result['owned_volumes']),set(self.package.receipt['fresh_resources']['volumes']))
        self.assertEqual((self.fixture.directory/'docker-compose.yaml').read_bytes(),self.fixture.base.read_bytes())
        self.assertEqual(self.provision(self.load()),result)
        self.assertFalse((self.fixture.directory/registry.OPERATION).exists())
        self.assertFalse((self.fixture.directory/registry.CURRENT).exists())
        self.assertNotIn('old_deployment',operation.state)
        self.assertNotIn('snapshot',operation.state)
        for row in self.fixture.volumes.values():self.assertEqual(row['Labels'][resources.LABEL],self.package.receipt['id'])

    def test_lost_volume_create_reply_reuses_only_same_owned_token(self):
        operation=self.begin();self.lost_volume_reply=True
        with self.assertRaisesRegex(OSError,'lost successful'):self.provision(operation)
        name=next(iter(self.fixture.volumes));root=Path(self.fixture.volumes[name]['Mountpoint']);inode=root.stat().st_ino
        result=self.provision(self.load())
        self.assertEqual(result['owned_volumes'][name]['inode'],inode)
        self.assertEqual(len(self.fixture.volumes),2)

    def test_foreign_replacement_volume_is_preserved_and_refuses_retry(self):
        operation=self.begin();self.lost_volume_reply=True
        with self.assertRaises(OSError):self.provision(operation)
        name=next(iter(self.fixture.volumes));row=self.fixture.volumes[name];row['Labels'][resources.LABEL]='different-owner'
        marker=Path(row['Mountpoint'])/'owner-data';marker.write_bytes(b'never delete')
        with self.assertRaises(mapping.DeploymentError) as caught:self.provision(self.load())
        self.assertEqual(caught.exception.code,'install_volume_foreign')
        self.assertEqual(marker.read_bytes(),b'never delete')

    def test_changed_private_env_refuses_before_copy_or_data_creation(self):
        operation=self.begin();self.fixture.environment.write_bytes(b'OWNER_CHANGED_INPUT=1\n')
        with self.assertRaises(mapping.DeploymentError) as caught:self.provision(operation)
        self.assertEqual(caught.exception.code,'install_inputs_changed')
        self.assertFalse(self.fixture.data.exists());self.assertEqual(self.fixture.volumes,{})
        self.assertFalse((self.fixture.directory/'docker-compose.yaml').exists())

    def test_new_canonical_conflict_is_preserved_before_any_control_creation(self):
        operation=self.begin();target=self.fixture.directory/'docker-compose.yaml';target.write_bytes(b'owner custom input')
        with self.assertRaises(mapping.DeploymentError) as caught:self.provision(operation)
        self.assertEqual(caught.exception.code,'install_existing_compose_differs')
        self.assertEqual(target.read_bytes(),b'owner custom input');self.assertFalse(self.fixture.data.exists())

    def test_live_foreign_mount_added_after_indexing_blocks_resource_mutation(self):
        operation=self.begin();self.fixture.containers=[{'Mounts':[{'Type':'bind','Source':str(self.fixture.directory)}]}]
        with self.assertRaises(mapping.DeploymentError) as caught:self.provision(operation)
        self.assertEqual(caught.exception.code,'install_foreign_mount')
        self.assertFalse(self.fixture.data.exists());self.assertEqual(self.fixture.volumes,{})

    def test_another_project_appearing_in_the_directory_blocks_resource_mutation(self):
        operation=self.begin()
        self.fixture.containers=[{'Config':{'Labels':{mapping.LABEL+'project':'another-project',
            mapping.LABEL+'project.working_dir':str(self.fixture.directory)}}}]
        with self.assertRaises(mapping.DeploymentError) as caught:self.provision(operation)
        self.assertEqual(caught.exception.code,'install_directory_occupied')
        self.assertFalse(self.fixture.data.exists());self.assertEqual(self.fixture.volumes,{})

    def test_new_control_content_after_resource_preparation_is_never_discarded(self):
        operation=self.begin();self.provision(operation)
        marker=self.fixture.data/'unexpected-owner-data';marker.write_bytes(b'keep new file')
        with self.assertRaises(mapping.DeploymentError) as caught:self.provision(self.load())
        self.assertEqual(caught.exception.code,'install_data_occupied')
        self.assertEqual(marker.read_bytes(),b'keep new file')

    def test_completed_canonical_missing_is_not_silently_recreated(self):
        operation=self.begin();self.provision(operation)
        target=self.fixture.directory/'docker-compose.yaml';target.unlink()
        with self.assertRaises(mapping.DeploymentError) as caught:self.provision(self.load())
        self.assertEqual(caught.exception.code,'install_canonical_missing');self.assertFalse(target.exists())

    def test_stale_instance_cannot_advance_a_newer_install_journal(self):
        original=self.begin();next_operation=self.load();self.provision(next_operation)
        with self.assertRaises(mapping.DeploymentError) as caught:self.provision(original)
        self.assertEqual(caught.exception.code,'install_state_changed')

    def test_public_proof_changes_before_indexing_refuse_without_install_reference(self):
        with patch.object(mapping,'ComposeDocker',self.Docker):
            with self.assertRaises(mapping.DeploymentError) as caught:
                resources.Installation.begin(self.package,verify=lambda _:self.fixture.receipt,public_verify=lambda:{'changed':True})
        self.assertEqual(caught.exception.code,'release_proof_changed')
        self.assertFalse((self.fixture.directory/packages.INSTALLATION).exists())

    def test_resource_only_resume_never_downgrades_a_possible_business_write_boundary(self):
        operation=self.begin();state=registry.private_json(operation.path)
        state['business_may_have_written']=True
        operation.path.write_bytes(backup.json_bytes(registry.sealed(state)))
        with self.assertRaises(mapping.DeploymentError) as caught:self.load()
        self.assertEqual(caught.exception.code,'install_state_invalid')
        self.assertFalse(self.fixture.data.exists())
