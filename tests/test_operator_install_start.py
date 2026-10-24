"""First startup preserves partial writes and reconciles a lost receipt reply."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_install_probe as probes
from scripts import operator_install_resources as resources
from scripts import operator_install_start as startup
from scripts import operator_registry as registry
from tests import test_operator_install_resources as fixtures


@unittest.skipUnless(sys.platform=='linux','Linux host startup journals')
class StartTests(unittest.TestCase):
    def setUp(self):
        fixture=fixtures.ResourceTests('runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture=fixture.fixture;self.package=fixture.package
        parent=fixture.Docker;outer=self
        class Docker(parent):
            def run(self,arguments, *, json_output=False):
                if arguments[-3:]==['config','--hash','*']:
                    return '\n'.join(name+' '+'a'*64 for name in outer.package.config['services'])
                return super().run(arguments,json_output=json_output)
        self.Docker=Docker
        self.identity={'version':1,'phase':'original_identity_verified','node_id':'b'*32,'generation':7,
            'role':'Standalone','endpoint':'','public_key_sha256':'c'*64,'keys':{'node-vault':'d'*64}}
        self.calls=[];self.identity_owners=[]
        self.patchers=[patch.object(mapping,'ComposeDocker',Docker),patch.object(mapping.Deployment,'discover',self.discover),
            patch.object(probes,'run',self.probe),
            # Model this one root-only host operation, not the shared os module.
            patch.object(startup,'os',SimpleNamespace(chown=self.chown_identity))]
        for item in self.patchers:item.start();self.addCleanup(item.stop)

    def chown_identity(self,path,uid,gid, *, follow_symlinks):
        self.assertEqual(path.parent,self.package.directory)
        self.assertRegex(path.name,r'^install-identity-[a-f0-9]{32}\.json$')
        self.assertFalse(path.is_symlink());self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(json.loads(path.read_text()),self.identity)
        self.assertEqual((uid,gid,follow_symlinks),(10001,10001,False))
        self.identity_owners.append((path,uid,gid,follow_symlinks))

    def begin(self):
        return startup.Installation.begin(self.package,verify=lambda _:self.fixture.receipt,
            public_verify=lambda:self.fixture.receipt['provenance'])

    def load(self):return startup.Installation.load(self.fixture.directory,verify=lambda _:self.fixture.receipt)

    def discover(self,*_args,**_kwargs):
        return SimpleNamespace(plan=SimpleNamespace(resources={'control':SimpleNamespace(root=self.fixture.data)}),observed={'web':{'running':True}})

    def probe(self,package,command,**values):
        self.calls.append(command)
        if command=='identity-original':return copy.deepcopy(self.identity)
        self.assertEqual(self.identity_owners,[(values['baseline'],10001,10001,False)])
        return {'version':1,'phase':'endpoint_open','node_id':self.identity['node_id'],'generation':7,
            'baseline_sha256':values['baseline_sha'],'business_opened':True,'readiness_status':200,
            'tls_enabled':False,'certificate_verified':False,'signed_identity_verified':False}

    def execute(self,docker,arguments,output,step,**_values):
        self.calls.append(step)
        self.assertEqual(resources._read(self.package.directory/'install-state.json')['phase'],'starting')
        self.assertNotIn('--force-recreate',arguments)

    def test_start_then_read_current_and_repeat_without_restarting_or_probing(self):
        operation=self.begin();result=operation.start(execute=self.execute)
        self.assertTrue(result['installation_complete']);self.assertEqual(operation.state['phase'],'ready')
        self.assertEqual(self.identity_owners,[(Path(operation.state['identity_file']),10001,10001,False)])
        current,receipt=registry.read(self.fixture.directory,self.package.receipt['project'])
        self.assertEqual((current['origin'],receipt['phase']),('source_install','installed_package'))
        calls=list(self.calls)
        self.assertEqual(self.load().start(execute=self.execute),result)
        self.assertEqual(self.calls,calls)
        self.assertFalse((self.fixture.directory/registry.OPERATION).exists())

    def test_partial_start_failure_retains_new_bytes_and_same_package_on_retry(self):
        operation=self.begin()
        def fail(*args,**values):
            self.execute(*args,**values)
            (self.fixture.data/'new-data').write_bytes(b'new data retained')
            raise OSError('lost startup reply')
        with self.assertRaisesRegex(OSError,'lost startup'):operation.start(execute=fail)
        self.assertEqual(operation.state['phase'],'starting')
        self.assertTrue(operation.state['business_may_have_written'])
        with self.assertRaises(mapping.DeploymentError) as caught:self.load().resources()
        self.assertEqual(caught.exception.code,'install_phase_invalid')
        self.assertTrue(self.load().start(execute=self.execute)['installation_complete'])
        self.assertEqual((self.fixture.data/'new-data').read_bytes(),b'new data retained')

    def test_lost_current_publish_reply_recognizes_exact_receipt_and_retains_new_writes(self):
        operation=self.begin();original=registry.publish
        def publish(*args,**kwargs):
            original(*args,**kwargs)
            (self.fixture.data/'post-open-write').write_bytes(b'never roll back')
            raise OSError('lost current receipt reply')
        with patch.object(registry,'publish',publish),self.assertRaisesRegex(OSError,'lost current'):
            operation.start(execute=self.execute)
        self.assertEqual(operation.state['phase'],'verifying');calls=list(self.calls)
        resumed=self.load();self.assertTrue(resumed.start(execute=self.execute)['installation_complete'])
        self.assertEqual(self.calls,calls);self.assertEqual(resumed.state['phase'],'ready')
        self.assertEqual((self.fixture.data/'post-open-write').read_bytes(),b'never roll back')

    def test_changed_current_or_newer_journal_is_never_overwritten(self):
        operation=self.begin();stale=self.load();operation.start(execute=self.execute)
        with self.assertRaises(mapping.DeploymentError) as caught:stale.start(execute=self.execute)
        self.assertEqual(caught.exception.code,'install_state_changed')
        path=self.fixture.directory/registry.CURRENT;value=registry.private_json(path);value['node_id']='e'*32
        path.write_bytes(backup.json_bytes(registry.sealed(value)));original=path.read_bytes()
        with self.assertRaises(mapping.DeploymentError) as caught:self.load().start(execute=self.execute)
        self.assertEqual(caught.exception.code,'install_receipt_changed');self.assertEqual(path.read_bytes(),original)

    def test_missing_completed_receipt_is_not_recreated(self):
        operation=self.begin();operation.start(execute=self.execute)
        (self.fixture.directory/registry.CURRENT).unlink()
        with self.assertRaises(mapping.DeploymentError) as caught:self.load().start(execute=self.execute)
        self.assertEqual(caught.exception.code,'install_receipt_missing')

    def test_changed_helper_owner_is_preserved_without_stopping_any_container(self):
        operation=self.begin();operation.resources()
        folder=self.package.directory/'install-probe-fixture';folder.mkdir(mode=0o700)
        owner={'version':1,'operation':operation.state['id'],'project':operation.state['project'],'token':'x',
            'name':'owner-container','image':self.package.receipt['images']['web']['id']}
        backup.private_write(folder/'helper-owner.json',backup.json_bytes(owner))
        with patch.object(startup,'_join_helper') as joined,self.assertRaises(mapping.DeploymentError) as caught:
            operation.start(execute=self.execute)
        self.assertEqual(caught.exception.code,'install_probe_owner_changed');joined.assert_not_called()
        self.assertEqual(self.calls,[])

    def test_changed_install_binding_is_rejected_by_normal_discovery(self):
        operation=self.begin();operation.start(execute=self.execute)
        value=registry.private_json(operation.path);value['endpoint_proof']['node_id']='e'*32
        operation.path.write_bytes(backup.json_bytes(registry.sealed(value)))
        with self.assertRaises(mapping.DeploymentError) as caught:registry.read(self.fixture.directory,self.package.receipt['project'])
        self.assertEqual(caught.exception.code,'install_receipt_changed')

    def test_partial_container_changes_block_retry_before_up(self):
        operation=self.begin();operation.resources()
        operation._save('starting',business_may_have_written=True)
        package=operation.package;name='web';definition=package.config['services'][name]
        row={'Name':'/fixture-web','Image':definition['image'],'Config':{'Image':definition['image'],
            'Labels':{mapping.LABEL+'project':operation.state['project'],mapping.LABEL+'service':name,
                mapping.LABEL+'oneoff':'False',mapping.LABEL+'config-hash':'a'*64,
                mapping.LABEL+'project.working_dir':str(self.fixture.directory),
                mapping.LABEL+'project.config_files':str(package.directory/'compose.json')},
            'Env':[key+'='+str(value) for key,value in definition['environment'].items()]},
            'Mounts':[{'Type':mount['type'],'Destination':target,'Name':mount['source'] if mount['type']=='volume' else None,
                'Source':mount['source'],'RW':not mount['read_only']} for target,mount in mapping.mounts(package.config,name).items()],
            'HostConfig':{'PortBindings':{}}}
        self.fixture.containers=[row];operation._containers()
        for change in ('config-hash','Image','Mounts','Env','oneoff'):
            bad=copy.deepcopy(row)
            if change in {'config-hash','oneoff'}:bad['Config']['Labels'][mapping.LABEL+change]='changed'
            elif change=='Image':bad['Image']='sha256:'+'0'*64
            elif change=='Mounts':bad['Mounts']=[]
            else:bad['Config']['Env'].append('UNREVIEWED=1')
            self.fixture.containers=[bad]
            with self.subTest(change=change),self.assertRaises(mapping.DeploymentError) as caught:operation.start(execute=self.execute)
            self.assertEqual(caught.exception.code,'install_container_changed')
        self.assertEqual(self.calls,[])


@unittest.skipUnless(sys.platform=='linux','Linux host package fixtures')
class HelperTests(unittest.TestCase):
    def test_helpers_are_readonly_and_inputs_do_not_invent_a_fence(self):
        fixture=fixtures.ResourceTests('runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
        package=fixture.package;calls=[]
        def execute(docker,arguments,directory,step,**values):
            config=backup.compose_values(json.loads((directory/'helper-compose.json').read_text()))
            self.assertTrue(config['services']['web']['read_only'])
            self.assertTrue(all(row['read_only'] for row in config['services']['web']['volumes']))
            self.assertNotIn('--fence-sha256',arguments)
            (directory/(step+'.log')).write_text(json.dumps({'version':1,'phase':'endpoint_open'}))
            calls.append(arguments)
        with patch.object(mapping,'ComposeDocker',fixture.Docker),patch.object(probes,'_join_helper') as joined:
            result=probes.run(package,'probe-open',baseline=fixture.fixture.environment,baseline_sha='a'*64,node_id='b'*32,execute=execute)
        self.assertEqual(result['phase'],'endpoint_open');joined.assert_called_once();self.assertEqual(len(calls),1)

    def test_helper_failure_is_joined_and_preserves_safe_underlying_code(self):
        fixture=fixtures.ResourceTests('runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
        def execute(docker,arguments,directory,step,**values):
            (directory/(step+'.log')).write_text(json.dumps({'error':'probe_certificate_untrusted','detail':'private value'}))
            mapping.fail('install_probe_failed','fixture')
        with patch.object(mapping,'ComposeDocker',fixture.Docker),patch.object(probes,'_join_helper') as joined:
            with self.assertRaises(mapping.DeploymentError) as caught:probes.run(fixture.package,'identity-original',execute=execute)
        self.assertEqual(caught.exception.code,'probe_certificate_untrusted');self.assertNotIn('private value',str(caught.exception))
        joined.assert_called_once()
