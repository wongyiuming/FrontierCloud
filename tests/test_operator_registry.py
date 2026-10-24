"""Host receipts bind generated deployments without replacing owner inputs."""
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_registry as registry


@unittest.skipUnless(sys.platform == 'linux','Linux host receipt ownership')
class RegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.directory = self.base/'deployment'
        self.directory.mkdir()
        self.data = self.base/'data'
        self.data.mkdir()
        self.prepared = self.base/'prepared'
        self.prepared.mkdir(mode=0o700)
        self.compose = self.directory/'docker-compose.yaml'
        self.compose.write_bytes(b'owner input\n')
        self.env = self.directory/'.env'
        self.env.write_bytes(b'SECRET=private-owner-value\n')
        backup.private_write(self.prepared/'compose.json',b'{"name":"fixture"}')
        self.identifier = uuid.uuid4().hex
        receipt = {'version':1,'id':self.identifier,'project':'fixture','phase':'images_prepared',
            'compose_sha256':backup.digest(self.prepared/'compose.json')[1],
            'source_files':[str(self.compose)],'source_env_files':[],
            'source_inputs':mapping._inputs([self.compose,self.env])}
        backup.private_write(self.prepared/'prepared.json',backup.json_bytes(receipt))
        self.value = {'version':1,'id':self.identifier,'project':'fixture','phase':'active_fenced',
            'node_id':'a'*32,'generation':7,'project_directory':str(self.directory),
            'prepared_directory':str(self.prepared),'prepared_sha256':backup.digest(self.prepared/'prepared.json')[1],
            'roots':{'control':registry.root_identity(self.data)}}

    def test_private_publication_is_idempotent_and_does_not_rewrite_owner_files(self):
        before = {path:backup.digest(path) for path in (self.compose,self.env)}
        first = registry.publish(self.directory,self.value,previous_sha256=None)
        second = registry.publish(self.directory,self.value,previous_sha256=None)
        self.assertEqual(first,second)
        current,prepared = registry.read(self.directory,'fixture')
        self.assertEqual(current,registry.sealed(self.value))
        self.assertEqual(prepared['source_files'],[str(self.compose)])
        self.assertEqual(before,{path:backup.digest(path) for path in before})
        self.assertEqual((self.directory/registry.CURRENT).stat().st_mode & 0o777,0o600)

    def test_changed_original_env_cannot_be_ignored(self):
        registry.publish(self.directory,self.value,previous_sha256=None)
        self.env.write_bytes(b'SECRET=changed-owner-value\n')
        with self.assertRaises(mapping.DeploymentError) as caught:
            registry.read(self.directory,'fixture')
        self.assertEqual(caught.exception.code,'operator_source_inputs_changed')
        self.assertNotIn('changed-owner-value',str(caught.exception))

    def test_a_new_implicit_env_cannot_be_ignored(self):
        self.env.unlink()
        path = self.prepared/'prepared.json'
        prepared = registry.private_json(path)
        prepared['source_inputs'] = mapping._inputs([self.compose])
        path.write_bytes(backup.json_bytes(prepared))
        self.value['prepared_sha256'] = backup.digest(path)[1]
        registry.publish(self.directory,self.value,previous_sha256=None)
        registry.read(self.directory,'fixture')
        self.env.write_bytes(b'NEW_OWNER_CONFIGURATION=yes\n')
        with self.assertRaises(mapping.DeploymentError) as caught:
            registry.read(self.directory,'fixture')
        self.assertEqual(caught.exception.code,'operator_source_inputs_changed')

    def test_newer_or_foreign_receipt_is_preserved(self):
        first = registry.publish(self.directory,self.value,previous_sha256=None)
        newer = dict(self.value,phase='opening')
        second = registry.publish(self.directory,newer,previous_sha256=first)
        with self.assertRaises(mapping.DeploymentError) as caught:
            registry.publish(self.directory,self.value,previous_sha256=first)
        self.assertEqual(caught.exception.code,'operator_receipt_changed')
        self.assertEqual(backup.digest(self.directory/registry.CURRENT)[1],second)
        with self.assertRaises(mapping.DeploymentError):
            registry.current_hash(self.directory,'foreign-project')

    def test_replaced_root_and_modified_preparation_refuse(self):
        registry.publish(self.directory,self.value,previous_sha256=None)
        self.data.rename(self.base/'retained-original-data')
        self.data.mkdir()
        with self.assertRaises(mapping.DeploymentError) as caught:
            registry.read(self.directory,'fixture')
        self.assertEqual(caught.exception.code,'operator_root_changed')
        (self.prepared/'compose.json').write_bytes(b'changed configuration')
        with self.assertRaises(mapping.DeploymentError) as caught:
            registry.read(self.directory,'fixture')
        self.assertEqual(caught.exception.code,'operator_receipt_changed')

    def test_public_receipt_and_symbolic_link_are_not_trusted(self):
        registry.publish(self.directory,self.value,previous_sha256=None)
        path = self.directory/registry.CURRENT
        path.chmod(0o644)
        with self.assertRaises(mapping.DeploymentError) as caught:
            registry.read(self.directory,'fixture')
        self.assertEqual(caught.exception.code,'operator_receipt_unsafe')
        path.rename(self.directory/'retained-receipt.json')
        path.symlink_to(self.directory/'retained-receipt.json')
        with self.assertRaises(backup.BackupError):
            registry.read(self.directory,'fixture')

    def test_operation_reference_is_private_separate_and_bound_to_preparation(self):
        path = self.prepared/'prepared.json'
        prepared = registry.private_json(path)
        prepared['project_directory'] = str(self.directory)
        path.write_bytes(backup.json_bytes(prepared))
        value = dict(self.value,phase='upgrade_operation',prepared_sha256=backup.digest(path)[1])
        checksum = registry.publish_operation(self.directory,value,previous_sha256=None)
        self.assertEqual(registry.operation(self.directory),registry.sealed(value))
        self.assertEqual(checksum,backup.digest(self.directory/registry.OPERATION)[1])
        self.assertFalse((self.directory/registry.CURRENT).exists())
        self.assertEqual(registry.publish_operation(self.directory,value,previous_sha256=None),checksum)
        prepared['id'] = 'f'*32
        path.write_bytes(backup.json_bytes(prepared))
        with self.assertRaises(mapping.DeploymentError) as caught:
            registry.operation(self.directory)
        self.assertEqual(caught.exception.code,'operator_operation_changed')
