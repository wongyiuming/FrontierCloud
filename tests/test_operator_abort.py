"""Pre-snapshot abort never restores SQL, moves payloads or replaces volumes."""
import asyncio
import copy
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch
import uuid

from scripts import operator_abort as abort
from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_recovery as recovery
from scripts import operator_upgrade as upgrade
from tests.test_operator_backup import FakeDocker,populate


class OriginalIdentityTests(unittest.TestCase):
    def test_identity_only_skips_pending_work_and_initialization_without_weakening_normal_preflight(self):
        from app.core import config,db
        from app.services import offline_upgrade as offline,runtime_gate
        facts={'version':1,'generation':4,'node_id':'a'*32,'role':'Standalone','endpoint':'',
            'keys':{'node_vault_sha256':'b'*64},'public_key_sha256':'c'*64,'tables':{}}
        engine=SimpleNamespace(dispose=AsyncMock())
        initialize=AsyncMock(side_effect=AssertionError('original identity check tried database preparation'))
        for command,idle in [('identity-original',False),('preflight',True)]:
            options=SimpleNamespace(command=command,node_id=None)
            inspected=AsyncMock(return_value=facts)
            pending=Mock()
            with patch.object(config.settings,'DB_TYPE','mysql'),patch.object(config.settings,'DEPLOYMENT_MODE',''),\
                    patch.object(config.settings,'TLS_ENABLED',False),patch.object(db,'engine',engine),\
                    patch.object(db,'init_db',initialize),patch.object(offline,'inspect_database',inspected),\
                    patch.object(offline,'pending_files',pending),patch.object(runtime_gate,'acquire',side_effect=AssertionError('readonly identity acquired a writer lease')):
                result=asyncio.run(offline.execute(options))
            self.assertIs(inspected.await_args.kwargs['require_idle'],idle)
            self.assertIs(inspected.await_args.kwargs['include_tables'],False)
            self.assertTrue(result['readonly'])
            self.assertEqual(result['phase'],'preflight_passed' if idle else 'original_identity_verified')
            if idle:self.assertGreater(pending.call_count,0)
            else:pending.assert_not_called()
        initialize.assert_not_awaited()


@unittest.skipUnless(sys.platform=='linux' and os.geteuid()==0,'private root-owned Linux journals')
class AbortTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name)
        self.docker=FakeDocker(self.base,'source')
        self.docker.config_value['services'].pop('mysql')
        self.docker.create_volume('certificate_runtime')
        for number,value in enumerate(self.docker.images.values(),1):value['id']='sha256:'+str(number)*64
        self.plan=backup.Plan.load(self.docker)
        populate(self.plan)
        self.root=self.plan.resources['control'].root
        self.output=self.base/'prepared'
        self.output.mkdir(mode=0o700)
        identifier=uuid.uuid4().hex
        services={}
        for number,(name,definition) in enumerate(self.plan.config['services'].items(),1):
            image=self.docker.images[definition['image']]['id']
            container_id=str(number)*64
            services[name]={'container_id':container_id,'image_id':image,'running':True}
            self.docker.containers_value.append({'Id':container_id,'Image':image,'Config':{'Labels':{
                mapping.LABEL+'project':self.plan.name,mapping.LABEL+'service':name,
                mapping.LABEL+'project.working_dir':str(self.docker.directory),mapping.LABEL+'oneoff':'False'}},
                'State':{'Running':True},'Mounts':[]})
        self.receipt={'id':identifier,'old_deployment':{'services':services}}
        backup.private_write(self.output/'prepared.json',backup.json_bytes(self.receipt))
        self.prepared=SimpleNamespace(directory=self.output,receipt=self.receipt,docker=self.docker,config=copy.deepcopy(self.plan.config))
        self.deployment=SimpleNamespace(docker=self.docker,plan=self.plan,inputs=[])
        state=upgrade._sealed({'version':1,'revision':1,'phase':'images_prepared','id':identifier,'project':self.plan.name,
            'project_directory':str(self.docker.directory),'prepared_sha256':backup.digest(self.output/'prepared.json')[1],
            'roots':{name:upgrade._identity(resource.root) for name,resource in self.plan.resources.items()},
            'retained_snapshots':[],'retained_fence_material':[],'acceptance_only':True,'previous_registry_sha256':None})
        backup.private_write(self.output/'upgrade-state.json',backup.json_bytes(state))
        self.operation=upgrade.Upgrade(self.deployment,self.prepared,state)
        self.verify=Mock()
        for name,replacement in [('_prepared_inputs',self.verify),('_reconcile_helpers',Mock())]:
            mocked=patch.object(upgrade.Upgrade,name,replacement)
            mocked.start();self.addCleanup(mocked.stop)
        owner=self
        class Docker:
            def __init__(self,directory,files,name,env_files):
                self.directory,self.files,self.name,self.env_files=directory,files,name,env_files
                self.value=backup.compose_values(backup.bounded_json(files[0]))
            def config(self):return copy.deepcopy(self.value)
            def local_root(self):return owner.docker.root
            def containers(self):return owner.docker.containers()
            def volume(self,name):return owner.docker.volume(name)
            def image_id(self,name):return next(value for value in owner.docker.images.values() if value['id']==name)
            def run(self,*_args,**_options):raise AssertionError('original abort attempted resource creation')
        self.factory=Docker

    def transaction(self):
        self.operation=upgrade.Upgrade.resume(self.deployment,self.prepared)
        return abort.OriginalAbort(self.operation,docker_factory=self.factory)

    def test_unfenced_cancel_changes_only_its_journal_and_preserves_new_manual_maintenance(self):
        marker=self.root/upgrade.MANUAL
        backup.private_write(marker,b'owner maintenance')
        with patch.object(self.docker,'stop') as stop:
            result=self.transaction().abort()
        self.assertEqual(result['phase'],'aborted')
        self.assertTrue(result['abort_complete'])
        self.assertFalse(result['deployment_changed'])
        self.assertTrue(result['manual_maintenance_preserved'])
        stop.assert_not_called()
        self.assertEqual(marker.read_bytes(),b'owner maintenance')
        with patch.object(abort.OriginalAbort,'_select',side_effect=AssertionError('duplicate cancellation changed data')):
            self.assertEqual(self.transaction().abort(),result)
        with patch.object(upgrade.Upgrade,'load',return_value=self.operation):
            self.assertEqual(upgrade.Upgrade.advance(self.docker.directory),result)

    def test_changed_original_container_is_preserved_before_any_stop(self):
        self.docker.containers_value[0]['Id']='f'*64
        with patch.object(self.docker,'stop') as stop,self.assertRaises(mapping.DeploymentError) as caught:
            self.transaction().abort()
        self.assertEqual(caught.exception.code,'abort_original_containers_changed')
        stop.assert_not_called()
        self.assertEqual(self.operation.state['phase'],'images_prepared')

    def test_incomplete_snapshot_is_retained_and_original_roots_and_payloads_are_reused(self):
        self.operation._fence()
        self.operation._save('backing_up',snapshot='snapshot-incomplete')
        snapshot=self.output/'snapshot-incomplete'
        snapshot.mkdir(mode=0o700)
        backup.private_write(snapshot/'control.tar',b'partial state backup')
        roots={name:upgrade._identity(resource.root) for name,resource in self.plan.resources.items()}
        volumes=set(self.docker.volumes)
        inode=(self.root/'media/music.mp3').stat().st_ino
        transaction=self.transaction()
        with patch.object(abort.OriginalAbort,'open',return_value={'phase':'original_fenced'}),\
                patch.object(backup,'restore',side_effect=AssertionError('abort restored SQL')),\
                patch.object(recovery,'rename_noreplace',side_effect=AssertionError('abort moved media')):
            transaction.abort()
        self.assertEqual(transaction.state['mode'],'original_abort')
        self.assertEqual(transaction.state['from_phase'],'backing_up')
        self.assertEqual(transaction.state['stage'],'restored_fenced')
        self.assertEqual(set(self.docker.volumes),volumes)
        self.assertEqual({name:upgrade._identity(resource.root) for name,resource in self.plan.resources.items()},roots)
        self.assertEqual((self.root/'media/music.mp3').stat().st_ino,inode)
        self.assertEqual((snapshot/'control.tar').read_bytes(),b'partial state backup')
        self.assertFalse((snapshot/'manifest.json').exists())
        self.verify.assert_called_once_with(abort.sources.verify_stage,snapshot=False)
        self.assertIsInstance(recovery.load_recovery(self.operation),abort.OriginalAbort)

    def test_possible_ddl_or_post_open_writes_never_use_original_abort(self):
        for phase,values in [('database_preparing',{}),('opening',{'business_may_have_written':True})]:
            self.operation._save(phase,**values)
            with patch.object(self.docker,'stop') as stop,self.assertRaises(mapping.DeploymentError) as caught:
                self.transaction().abort()
            self.assertEqual(caught.exception.code,'abort_database_may_have_changed')
            stop.assert_not_called()

    def test_manual_fence_blocks_opening_but_preserves_the_selected_abort(self):
        self.operation._fence()
        marker=self.root/upgrade.MANUAL
        backup.private_write(marker,b'manual')
        with patch.object(self.docker,'stop') as stop,self.assertRaises(mapping.DeploymentError) as caught:
            self.transaction().abort()
        self.assertEqual(caught.exception.code,'manual_maintenance_active')
        self.assertEqual(self.operation.state['recovery']['mode'],'original_abort')
        self.assertEqual(self.operation.state['phase'],'recovering')
        stop.assert_not_called()
        self.assertEqual(marker.read_bytes(),b'manual')

    def test_recovery_mode_cannot_be_relabelled_from_a_database_preparation_phase(self):
        self.operation._fence()
        with patch.object(abort.OriginalAbort,'open',return_value={}):self.transaction().abort()
        value={**self.operation.state['recovery'],'from_phase':'database_preparing'}
        self.operation._save('restored_fenced',recovery=value)
        with self.assertRaises(mapping.DeploymentError) as caught:self.transaction().prepare()
        self.assertEqual(caught.exception.code,'abort_database_may_have_changed')
