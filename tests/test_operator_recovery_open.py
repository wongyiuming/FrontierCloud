"""Restored opening binds historical state and never replays an old snapshot."""
import asyncio
import copy
import hashlib
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

from app.services import offline_upgrade as offline
from app.services import operator_probe as probe
from scripts import operator_backup as backup
from scripts import operator_recovery_open as opening
from scripts import operator_registry as registry
from scripts import operator_upgrade as upgrade
from scripts import operator_deployment as mapping


class HistoricalProbeTests(unittest.TestCase):
    def execute(self, command, *, generation=4, changed_key=False):
        from app.core import config,db
        from app.services import runtime_gate
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = {'node_id':'a'*32,'generation':4,'role':'Standalone','endpoint':'',
                'public_key_sha256':'b'*64,'keys':{'node_vault_sha256':'c'*64},'tables':{}}
            path = root/'baseline.json'
            backup.private_write(path,backup.json_bytes(baseline))
            facts = {**baseline,'generation':generation,'keys':{'node_vault_sha256':('d' if changed_key else 'c')*64}}
            engine = SimpleNamespace(dispose=AsyncMock())
            validated,inspected = AsyncMock(),AsyncMock(return_value=facts)
            options = SimpleNamespace(command=command,baseline=str(path),baseline_sha256=backup.digest(path)[1],
                node_id='a'*32,fence_sha256='e'*64)
            network = Mock(return_value={'readiness_status':200,'tls_enabled':False,'certificate_verified':False,
                'signed_identity_verified':False,'business_entry_status':200,'media_read_verified':False})
            with patch.object(config.settings,'DB_TYPE','mysql'),patch.object(db,'engine',engine),patch.object(db,'validate_existing_database',validated),\
                    patch.object(runtime_gate,'DATA_ROOT',root),patch.object(offline,'inspect_database',inspected),patch.object(probe,'network_probe',network):
                try:
                    result = asyncio.run(probe.execute(options))
                except offline.PreparationError:
                    self.last = (validated,inspected,network,engine)
                    raise
            self.last = (validated,inspected,network,engine)
            return result

    def test_restored_probe_requires_exact_old_generation_and_keys_without_current_schema_initialization(self):
        result = self.execute('probe-restored')
        self.assertEqual((result['phase'],result['generation']),('endpoint_restored',4))
        validated,inspected,network,engine = self.last
        validated.assert_not_awaited()
        self.assertIs(inspected.await_args.kwargs['require_idle'],False)
        self.assertIs(inspected.await_args.kwargs['include_tables'],False)
        network.assert_called_once()
        engine.dispose.assert_awaited_once()

    def test_wrong_historical_generation_or_key_never_reaches_network_proof(self):
        for options in ({'generation':5},{'changed_key':True}):
            with self.subTest(options=options),self.assertRaises(offline.PreparationError) as caught:
                self.execute('probe-restored',**options)
            self.assertEqual(caught.exception.code,'node_identity_changed')
            self.last[2].assert_not_called()

    def test_forward_open_probe_still_requires_the_current_generation_validator(self):
        with self.assertRaises(offline.PreparationError) as caught:self.execute('probe-open')
        self.assertEqual(caught.exception.code,'node_identity_changed')
        self.last[0].assert_awaited_once()
        self.last[2].assert_not_called()


@unittest.skipUnless(sys.platform == 'linux','private Linux host receipts')
class RecoveredReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.directory,self.parent = root/'deployment',root/'original-prepared'
        self.directory.mkdir()
        self.parent.mkdir(mode=0o700)
        self.prepared = self.parent/'recovery-attempt'
        self.prepared.mkdir(mode=0o700)
        self.original = {'id':'a'*32,'project':'fixture','images':{'web':{'id':'sha256:'+'b'*64,'os':'linux','architecture':'amd64'}},
            'old_deployment':{'services':{'web':{'image_id':'sha256:'+'c'*64},'redis':{'image_id':'sha256:'+'d'*64}}},
            'source_files':[str(self.directory/'docker-compose.yaml')],'source_env_files':[],'source_inputs':[]}
        backup.private_write(self.parent/'prepared.json',backup.json_bytes(self.original))
        original_sha = backup.digest(self.parent/'prepared.json')[1]
        self.receipt = {'version':1,'phase':'restored_original','id':'a'*32,'project':'fixture',
            'original_prepared_directory':str(self.parent),'original_prepared_sha256':original_sha,
            'recovery_id':'e'*32,'compose_sha256':'f'*64,'configuration_sha256':'0'*64,
            'images':{name:{'id':row['image_id'],'os':'linux','architecture':'amd64'} for name,row in self.original['old_deployment']['services'].items()},
            **{name:self.original[name] for name in ('source_files','source_env_files','source_inputs')}}
        backup.private_write(self.prepared/'prepared.json',backup.json_bytes(self.receipt))
        prepared_sha = backup.digest(self.prepared/'prepared.json')[1]
        self.current = {'version':1,'phase':'opening','id':'a'*32,'project':'fixture','project_directory':str(self.directory),
            'origin':'controlled_recovery','prepared_directory':str(self.prepared),'prepared_sha256':prepared_sha,
            'node_id':'1'*32,'generation':4,'roots':{'control':{'path':'fixture-root'}}}
        self.state = upgrade._sealed({'version':1,'phase':'restored_opening','revision':1,'id':'a'*32,
            'project':'fixture','project_directory':str(self.directory),'prepared_sha256':original_sha,
            'business_may_have_written':True,'preflight':{'generation':4,'node_id':'1'*32},
            'recovery':{'id':'e'*32,'stage':'opening','prepared_sha256':prepared_sha,'restored_roots':self.current['roots'],
                'attempt':{'directory':str(self.prepared),'compose_sha256':'f'*64,'configuration_sha256':'0'*64}}})
        backup.private_write(self.parent/'upgrade-state.json',backup.json_bytes(self.state))

    def test_old_images_have_a_separate_bound_origin_and_do_not_claim_candidate_image_provenance(self):
        opening.validate_receipt(self.directory,self.current,self.receipt)
        self.assertNotIn('provenance',self.receipt)
        self.assertNotEqual(self.receipt['images']['web']['id'],self.original['images']['web']['id'])

    def test_changed_generation_source_mapping_image_or_recovery_identity_refuses(self):
        for field in ('generation','source','image','recovery'):
            with self.subTest(field=field):
                current,receipt = copy.deepcopy(self.current),copy.deepcopy(self.receipt)
                if field == 'generation':current['generation']=7
                if field == 'source':receipt['source_files']=['foreign-compose.yaml']
                if field == 'image':receipt['images']['web']['id']='sha256:'+'2'*64
                if field == 'recovery':receipt['recovery_id']='3'*32
                with self.assertRaises(mapping.DeploymentError) as caught:
                    opening.validate_receipt(self.directory,current,receipt)
                self.assertEqual(caught.exception.code,'recovery_receipt_changed')

    def test_lost_final_reply_never_downgrades_an_already_ready_receipt(self):
        ready = {**self.current,'phase':'ready'}
        checksum = registry.publish(self.directory,ready,previous_sha256=None)
        state = copy.deepcopy(self.state)
        state['recovery']['registry_sha256'] = 'previous-opening-hash'
        transaction = SimpleNamespace(operation=SimpleNamespace(state=state,
            deployment=SimpleNamespace(docker=SimpleNamespace(directory=self.directory))),state=state['recovery'])
        self.assertEqual(opening.Opening(transaction).publish('opening'),checksum)
        self.assertEqual(registry.private_json(self.directory/registry.CURRENT),registry.sealed(ready))

    def test_completed_recovery_returns_its_record_without_touching_new_manual_fences_or_data(self):
        result = {'phase':'restored_opened','project':'fixture','recovery_complete':True}
        state = upgrade._sealed({**self.state,'phase':'restored_opened',
            'recovery':{**self.state['recovery'],'stage':'opened','open_result':result}})
        path = self.parent/'upgrade-state.json'
        path.write_bytes(backup.json_bytes(state))
        manual = self.directory/'manual-fence'
        manual.write_bytes(b'new owner')
        transaction = SimpleNamespace(operation=SimpleNamespace(state=state,path=path),state=state['recovery'],
            _validate=Mock(side_effect=AssertionError('completed recovery touched mutable state')))
        with patch.object(backup,'operation_lock') as lock:
            lock.return_value.__enter__.return_value=None
            self.assertEqual(opening.Opening(transaction).open(),result)
        transaction._validate.assert_not_called()
        self.assertEqual(manual.read_bytes(),b'new owner')

    def test_created_container_gate_refuses_running_services_wrong_images_or_replaced_roots(self):
        control=self.directory/'control'
        control.mkdir()
        config={'name':'fixture','services':{'web':{},'redis':{}}}
        state=copy.deepcopy(self.state)
        state['recovery']['attempt']['configuration_sha256']=mapping.checksum(config)
        state['recovery']['restored_roots']={'control':upgrade._identity(control)}
        transaction=SimpleNamespace(operation=SimpleNamespace(state=state,
            deployment=SimpleNamespace(docker=SimpleNamespace(directory=self.directory))),state=state['recovery'])
        observed={name:{'image_id':image['id'],'running':False} for name,image in self.receipt['images'].items()}
        current=SimpleNamespace(plan=SimpleNamespace(config=config,resources={'control':SimpleNamespace(root=control)}),observed=observed)
        with patch.object(mapping.Deployment,'discover',return_value=current):
            opening.Opening(transaction).verify_created()
            for field in ('running','image','root'):
                with self.subTest(field=field):
                    if field=='running':observed['web']['running']=True
                    if field=='image':observed['web']['image_id']='sha256:'+'0'*64
                    if field=='root':
                        control.rename(self.directory/'retained-control')
                        control.mkdir()
                    with self.assertRaises(mapping.DeploymentError) as caught:opening.Opening(transaction).verify_created()
                    self.assertEqual(caught.exception.code,'recovery_created_configuration_changed')
                    observed['web']={'running':False,'image_id':self.receipt['images']['web']['id']}

    def test_original_abort_receipt_requires_an_early_phase_and_unchanged_original_roots(self):
        state=copy.deepcopy(self.state)
        state['roots']=copy.deepcopy(self.current['roots'])
        state['recovery'].update(mode='original_abort',from_phase='backing_up')
        state['recovery']['attempt']['control']=state['roots']['control']['path']
        receipt=copy.deepcopy(self.receipt)
        receipt['recovery_mode']='original_abort'
        (self.prepared/'prepared.json').write_bytes(backup.json_bytes(receipt))
        current={**self.current,'prepared_sha256':backup.digest(self.prepared/'prepared.json')[1]}
        state['recovery']['prepared_sha256']=current['prepared_sha256']
        path=self.parent/'upgrade-state.json'
        path.write_bytes(backup.json_bytes(upgrade._sealed(state)))
        opening.validate_receipt(self.directory,current,receipt)
        for field in ('phase','root'):
            changed=copy.deepcopy(state)
            if field=='phase':changed['recovery']['from_phase']='database_preparing'
            else:changed['recovery']['attempt']['control']='/foreign/original-root'
            path.write_bytes(backup.json_bytes(upgrade._sealed(changed)))
            with self.assertRaises(mapping.DeploymentError) as caught:opening.validate_receipt(self.directory,current,receipt)
            self.assertEqual(caught.exception.code,'recovery_receipt_changed')


@unittest.skipUnless(sys.platform == 'linux','Linux directory persistence')
class OpeningSequenceTests(unittest.TestCase):
    def fixture(self, root, *, resumed=False):
        control = root/'control'
        control.mkdir()
        marker = control/upgrade.HARD
        marker.write_bytes(b'owned')
        if resumed:marker.unlink()
        state = {'stage':'opening' if resumed else 'verified','prepared_sha256':'b'*64,
            'baseline':str(root/'baseline.json'),'baseline_sha256':'c'*64,
            'attempt':{'configuration_sha256':None},'id':'d'*32}
        original = {'project':'fixture','preflight':{'node_id':'a'*32,'generation':4},'recovery':state,'acceptance_only':True,
            **({'business_may_have_written':True} if resumed else {})}
        docker = SimpleNamespace(base=['docker','compose'],stop=Mock())
        config = {'services':{'web':{'environment':{'TLS_ENABLED':'false'}}}}
        state['attempt']['configuration_sha256'] = mapping.checksum(config)
        plan = SimpleNamespace(docker=docker,resources={'control':SimpleNamespace(root=control)},config=config)
        operation = SimpleNamespace(state=original,path=root/'state.json',deployment=SimpleNamespace(docker=SimpleNamespace(directory=root)))
        transaction = SimpleNamespace(operation=operation,state=state,_plan=Mock(return_value=plan),_validate=Mock(),
            _original_cold=Mock(),_cold_plan=Mock(return_value=SimpleNamespace(assert_cold=Mock())),
            _restored_roots=Mock(),_payloads_at_target=Mock())
        def saved(stage,*,opening_boundary=False,**values):
            state.update(values)
            state['stage']=stage
            if opening_boundary:original['business_may_have_written']=True
        transaction._save=saved
        subject = opening.Opening(transaction)
        subject.baseline=Mock(return_value=(root/'baseline.json','c'*64))
        subject.prepare_receipt=Mock(return_value='b'*64)
        subject.failed_resources_cold=Mock()
        subject.verify_created=Mock()
        subject.publish=Mock(side_effect=lambda phase:'registry-'+phase)
        subject.attempt=Mock(return_value=root)
        proof={'phase':'endpoint_restored','generation':4,'node_id':'a'*32,'baseline_sha256':'c'*64,
            'business_opened':True,'readiness_status':200,'tls_enabled':False}
        subject.helper=Mock(return_value=SimpleNamespace(state={},_database_helper=Mock(return_value=proof)))
        current=SimpleNamespace(plan=plan,observed={'web':{'running':True}},summary=Mock(return_value={'stable':True}))
        return subject,operation,marker,current,docker

    def run_open(self, subject, current, execute):
        with patch.object(backup,'operation_lock') as lock,patch.object(upgrade,'_read',return_value=subject.operation.state),\
                patch.object(mapping.Deployment,'discover',return_value=current):
            lock.return_value.__enter__.return_value=None
            return subject.open(execute=execute)

    def test_stopped_containers_are_rebound_before_the_irreversible_opening_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            subject,operation,marker,current,_docker=self.fixture(Path(temporary))
            calls=[]
            def execute(_docker,arguments,*_args,**_options):
                calls.append((arguments[:],operation.state.get('business_may_have_written',False),marker.exists()))
            result=self.run_open(subject,current,execute)
            self.assertEqual(result['phase'],'restored_opened')
            self.assertIn('create',calls[0][0])
            self.assertIn('--force-recreate',calls[0][0])
            self.assertEqual(calls[0][1:],(False,True))
            self.assertIn('up',calls[1][0])
            self.assertNotIn('--force-recreate',calls[1][0])
            self.assertEqual(calls[1][1:],(True,False))
            self.assertFalse(any('--volumes' in arguments or '-v' in arguments for arguments,*_ in calls))
            subject.verify_created.assert_called_once()

    def test_create_failure_leaves_the_restored_fence_and_opening_boundary_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            subject,operation,marker,current,_docker=self.fixture(Path(temporary))
            def execute(_docker,arguments,*_args,**_options):
                self.assertIn('create',arguments)
                raise mapping.DeploymentError('create_failed','private diagnostics retained')
            with self.assertRaises(mapping.DeploymentError):self.run_open(subject,current,execute)
            self.assertEqual(subject.state['stage'],'verified')
            self.assertFalse(operation.state.get('business_may_have_written',False))
            self.assertTrue(marker.exists())
            subject.verify_created.assert_not_called()

    def test_opening_retry_never_recreates_or_stops_services_that_may_hold_new_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            subject,_operation,_marker,current,docker=self.fixture(Path(temporary),resumed=True)
            calls=[]
            result=self.run_open(subject,current,lambda _docker,arguments,*_args,**_options:calls.append(arguments[:]))
            self.assertTrue(result['recovery_complete'])
            self.assertEqual(len(calls),1)
            self.assertIn('up',calls[0])
            self.assertNotIn('--force-recreate',calls[0])
            docker.stop.assert_not_called()
            subject.verify_created.assert_not_called()
