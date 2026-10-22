"""Source handoff does not replace release proof or an indexed transaction."""
from contextlib import ExitStack
import json
import hashlib
import os
from pathlib import Path
import sys
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import operator_backup as backup
from scripts import operator_abort as abort
from scripts import operator_deployment as mapping
from scripts import operator_entry as entry
from scripts import operator_prepare as preparation
from scripts import operator_registry as registry
from scripts import operator_source as sources
from scripts.operator_upgrade import Upgrade


class Executed(BaseException):
    pass


@unittest.skipUnless(sys.platform=='linux','host exec, inherited locks and Linux source modes')
class HandoffTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.directory=self.root/'deployment'
        self.directory.mkdir()
        self.cache=self.root/'host'
        self.stage=self.root/'selected'
        self.proof={'revision':'a'*40,'public_provenance_verified':True}
        self.create_stage(self.stage)
        self.docker=SimpleNamespace(local_root=lambda:self.root,containers=lambda:[],environment={'PATH':os.environ['PATH']})
        self.deployment=SimpleNamespace(docker=SimpleNamespace(directory=self.directory),plan=SimpleNamespace(name='fixture'),
            summary=lambda:{'project':'fixture','configuration_sha256':'original'},operator_phase=None)
        self.calls=[]

    def create_stage(self,stage, *, public=True,protocol=True,install=False,program='# unit fixture entry only\n'):
        stage.mkdir(mode=0o700)
        source=stage/'source'
        (source/'scripts').mkdir(parents=True)
        (source/'protocol').mkdir()
        (source/'scripts/frontiercloud_operator.py').write_text(program)
        if protocol:backup.private_write(source/'protocol/python-operator.json',backup.json_bytes(entry.PROTOCOL))
        if install:backup.private_write(source/'protocol/python-install.json',backup.json_bytes(entry.installations.PROTOCOL))
        files={}
        for path in source.rglob('*'):
            path.chmod(0o755 if path.is_dir() else 0o644)
            if path.is_file():files[path.relative_to(source).as_posix()]={'bytes':path.stat().st_size,'sha256':backup.digest(path)[1],'mode':0o644}
        source.chmod(0o755)
        archive=stage/'source.tar.gz'
        with tarfile.open(archive,'w:gz') as stream:stream.add(source,arcname='source')
        archive.chmod(0o600)
        size,digest=backup.digest(archive)
        value={'version':1,'phase':'source_staged','provenance':{**self.proof,'public_provenance_verified':public},
            'files':files,'directories':['protocol','scripts'],'archive':{'bytes':size,'sha256':digest},'deployment_changed':False}
        value['stage_sha256']=mapping.checksum(value)
        backup.private_write(stage/'source-receipt.json',backup.json_bytes(value))
        return value

    def patches(self):
        stack=ExitStack()
        stack.enter_context(patch.object(mapping,'ComposeDocker',return_value=self.docker))
        stack.enter_context(patch.object(mapping.Deployment,'discover',return_value=self.deployment))
        stack.enter_context(patch.object(preparation,'host_records_outside_data'))
        stack.enter_context(patch.object(entry.releases,'select_latest',return_value=self.proof))
        stack.enter_context(patch.object(entry,'_operation',return_value=(None,None)))
        def stage(destination,**options):
            self.assertEqual(options,{'_selected':self.proof})
            return self.create_stage(destination)
        stack.enter_context(patch.object(sources,'stage',side_effect=stage))
        def execute(program,arguments,environment):
            self.calls.append({'program':program,'arguments':arguments,'environment':environment,'cwd':Path.cwd()})
            request=registry.private_json(Path(arguments[-1]))
            self.assertTrue(os.get_inheritable(request['lock_fd']))
            raise Executed()
        stack.enter_context(patch.object(entry.os,'execve',side_effect=execute))
        original=Path.cwd()
        stack.callback(os.chdir,original)
        return stack

    def test_new_upgrade_executes_the_verified_entry_with_no_user_plan_or_version(self):
        with self.patches():
            with self.assertRaises(Executed):entry.start(self.directory,host_root=self.cache)
        self.assertEqual(len(self.calls),1)
        call=self.calls[0]
        self.assertEqual(call['arguments'][1:3],['-I','-B'])
        self.assertEqual(call['arguments'][-2],'--operator-handoff')
        self.assertEqual(call['cwd'],self.directory)
        self.assertNotIn('PYTHONPATH',call['environment'])
        request_path=Path(call['arguments'][-1])
        request=registry.private_json(request_path)
        self.assertEqual(request['intent'],'new_upgrade')
        self.assertEqual(request['old_deployment'],self.deployment.summary())
        self.assertIsNone(request['previous_operation_sha256'])
        self.assertEqual(request['source_stage_sha256'],sources.verify_stage(Path(request['stage_directory']))['stage_sha256'])
        self.assertTrue((request_path.parent/'handoff-input.json').is_file())
        with self.assertRaises(OSError):os.fstat(request['lock_fd'])

    def test_pending_operation_keeps_its_exact_source_without_querying_latest_or_live_discovery(self):
        prepared=self.root/'prepared';prepared.mkdir(mode=0o700)
        checked=sources.verify_stage(self.stage)
        backup.private_write(prepared/'prepared.json',backup.json_bytes({'stage_directory':str(self.stage),
            'source_stage_sha256':checked['stage_sha256'],'provenance':checked['provenance']}))
        reference={'prepared_directory':str(prepared)}
        backup.private_write(self.directory/registry.OPERATION,b'private index for unit test')
        with self.patches(),patch.object(entry,'_operation',return_value=(reference,{'phase':'future_phase'})),\
                patch.object(entry.releases,'select_latest') as latest,patch.object(mapping.Deployment,'discover') as discover,\
                patch.object(sources,'stage') as stage:
            with self.assertRaises(Executed):entry.start(self.directory,host_root=self.cache)
        latest.assert_not_called();discover.assert_not_called();stage.assert_not_called()
        request=registry.private_json(Path(self.calls[0]['arguments'][-1]))
        self.assertEqual((request['intent'],request['stage_directory']),('resume',str(self.stage)))

    def test_public_handoff_never_admits_a_native_fixture(self):
        path=self.root/'unpublished';self.create_stage(path,public=False)
        with self.assertRaises(mapping.DeploymentError) as caught:entry._stage(path)
        self.assertEqual(caught.exception.code,'source_not_published')

    def test_old_release_without_operator_refuses_before_exec_or_deployment_changes(self):
        with self.patches(),patch.object(sources,'stage',side_effect=lambda path,**_:self.create_stage(path,protocol=False)):
            with self.assertRaises(mapping.DeploymentError) as caught:entry.start(self.directory,host_root=self.cache)
        self.assertEqual(caught.exception.code,'source_operator_missing')
        self.assertEqual(self.calls,[])
        self.assertFalse((self.directory/registry.OPERATION).exists())

    def test_container_mount_overlap_refuses_before_creating_host_material(self):
        self.docker.containers=lambda:[{'Mounts':[{'Type':'bind','Source':str(self.root)}]}]
        with self.patches():
            with self.assertRaises(mapping.DeploymentError) as caught:entry.start(self.directory,host_root=self.cache)
        self.assertEqual(caught.exception.code,'operator_records_overlap_mount')
        self.assertFalse(self.cache.exists())

    def test_occupied_or_changed_source_is_preserved_and_never_executed(self):
        (self.stage/'source/scripts/frontiercloud_operator.py').write_text('# changed after verification\n')
        with self.assertRaises(entry.releases.ReleaseError):entry._stage(self.stage)
        self.assertEqual((self.stage/'source/scripts/frontiercloud_operator.py').read_text(),'# changed after verification\n')

    def test_entry_lock_excludes_an_independent_open_and_does_not_follow_symlinks(self):
        path=self.root/'entry.lock'
        descriptor=entry._lock(path)
        try:
            with self.assertRaises(mapping.DeploymentError) as caught:entry._lock(path)
            self.assertEqual(caught.exception.code,'operator_entry_busy')
        finally:os.close(descriptor)
        link=self.root/'link';link.symlink_to(path)
        with self.assertRaises(OSError):entry._lock(link)

    def request(self):
        folder=self.root/'handoff';folder.mkdir(mode=0o700)
        lock_path=self.root/'lock-folder';lock_path.mkdir(mode=0o700)
        descriptor=entry._lock(lock_path/'entry.lock')
        value=registry.sealed({'version':1,'phase':'host_handoff','project_directory':str(self.directory),
            'directory_identity':registry.root_identity(self.directory),'stage_directory':str(self.stage),
            'source_stage_sha256':sources.verify_stage(self.stage)['stage_sha256'],'intent':'new_upgrade',
            'previous_operation_sha256':None,'old_deployment':self.deployment.summary(),
            'lock_path':str(lock_path/'entry.lock'),'lock_fd':descriptor})
        backup.private_write(folder/'request.json',backup.json_bytes(value))
        return folder/'request.json',descriptor

    def test_staged_continuation_checks_same_deployment_and_keeps_begin_and_advance_private(self):
        request,descriptor=self.request()
        prepared=SimpleNamespace(receipt={})
        result={'phase':'opened','upgrade_complete':True}
        with patch.object(entry,'__file__',str(self.stage/'source/scripts/operator_entry.py')),\
                patch.object(entry,'_operation',return_value=(None,None)),\
                patch.object(mapping.Deployment,'discover',return_value=self.deployment),\
                patch.object(preparation,'prepare',return_value=prepared) as prepare,\
                patch.object(Upgrade,'begin') as begin,patch.object(Upgrade,'advance',return_value=result) as advance:
            self.assertEqual(entry.continue_upgrade(request),result)
        prepare.assert_called_once_with(self.deployment,self.stage,request.parent/'prepared')
        begin.assert_called_once_with(self.deployment,prepared)
        advance.assert_called_once_with(self.directory)
        with self.assertRaises(OSError):os.fstat(descriptor)

    def test_stale_operation_reference_refuses_before_preparing_or_advancing(self):
        request,descriptor=self.request()
        backup.private_write(self.directory/registry.OPERATION,b'changed operation')
        with patch.object(entry,'__file__',str(self.stage/'source/scripts/operator_entry.py')),\
                patch.object(entry,'_operation',return_value=({'new':'reference'},{})),\
                patch.object(preparation,'prepare') as prepare,patch.object(Upgrade,'advance') as advance:
            with self.assertRaises(mapping.DeploymentError) as caught:entry.continue_upgrade(request)
        self.assertEqual(caught.exception.code,'operator_handoff_operation_changed')
        prepare.assert_not_called();advance.assert_not_called()
        with self.assertRaises(OSError):os.fstat(descriptor)

    def test_continuation_cannot_run_from_another_unverified_checkout(self):
        request,descriptor=self.request()
        try:
            with self.assertRaises(mapping.DeploymentError) as caught:entry.continue_upgrade(request)
            self.assertEqual(caught.exception.code,'operator_handoff_source_changed')
        finally:os.close(descriptor)

    def test_terminal_completion_requires_its_bound_result(self):
        state={'phase':'opened','project':'fixture','open_result':{'phase':'opened','project':'fixture','upgrade_complete':True}}
        self.assertTrue(entry._complete(state))
        state['open_result']['project']='another'
        with self.assertRaises(mapping.DeploymentError):entry._complete(state)

    def test_current_ready_release_does_not_download_build_or_restart(self):
        self.deployment.operator_phase='ready'
        self.deployment.observed={'web':{'running':True},'redis':{'running':True},'media-init':{'running':False}}
        state={'phase':'opened','project':'fixture','open_result':{'phase':'opened','project':'fixture','upgrade_complete':True}}
        reference,_=self.indexed()
        with self.patches(),patch.object(entry,'__file__',str(self.stage/'source/scripts/operator_entry.py')),\
                patch.object(entry,'_operation',return_value=(reference,state)),\
                patch.object(registry,'read',return_value=({}, {'provenance':self.proof})),patch.object(sources,'stage') as stage:
            result=entry.start(self.directory,host_root=self.cache)
        self.assertEqual(result['state'],'already_current')
        self.assertFalse(result['deployment_changed'])
        self.assertFalse(result['business_read_verified'])
        stage.assert_not_called()
        self.assertEqual(self.calls,[])

    def test_old_terminal_entry_hands_off_before_discovery_or_new_version_selection(self):
        reference,_=self.indexed()
        state={'phase':'opened','project':'fixture','open_result':{'phase':'opened','project':'fixture','upgrade_complete':True}}
        with self.patches(),patch.object(entry,'_operation',return_value=(reference,state)),\
                patch.object(mapping.Deployment,'discover',side_effect=AssertionError('old entry cannot understand added service')) as discover,\
                patch.object(entry.releases,'select_latest') as latest,patch.object(sources,'stage') as stage:
            with self.assertRaises(Executed):entry.start(self.directory,host_root=self.cache)
        discover.assert_not_called();latest.assert_not_called();stage.assert_not_called()
        value=registry.private_json(Path(self.calls[-1]['arguments'][-1]))
        self.assertEqual((value['command'],value['intent'],value['stage_directory']),('upgrade','select_upgrade',str(self.stage)))
        self.assertEqual(value['host_root'],str(self.cache))

    def selection_request(self):
        path,descriptor,reference,checked=self.selected_request('resume')
        value=registry.private_json(path)
        os.close(descriptor)
        host=self.root/'selection-host';host.mkdir(mode=0o700)
        cache=host/hashlib.sha256(str(self.directory).encode()).hexdigest();cache.mkdir(mode=0o700)
        descriptor=entry._lock(cache/'entry.lock')
        value.update(command='upgrade',intent='select_upgrade',host_root=str(host),lock_path=str(cache/'entry.lock'),lock_fd=descriptor)
        selected=path.parent/'select-upgrade.json';backup.private_write(selected,backup.json_bytes(registry.sealed(value)))
        return selected,descriptor,reference,host,cache

    def test_retained_selection_reuses_the_inherited_lock_without_close_reopen(self):
        request,descriptor,reference,host,cache=self.selection_request()
        state={'phase':'opened','project':'fixture','open_result':{'phase':'opened','project':'fixture','upgrade_complete':True}}
        result={'state':'already_current'}
        def selected(directory,command,host_root,cache_root,fd,docker):
            self.assertEqual((directory,command,host_root,cache_root,fd),(self.directory,'upgrade',host,cache,descriptor))
            os.fstat(fd)
            with self.assertRaises(mapping.DeploymentError) as caught:entry._lock(cache/'entry.lock')
            self.assertEqual(caught.exception.code,'operator_entry_busy')
            return result
        with patch.object(entry,'__file__',str(self.stage/'source/scripts/operator_entry.py')),\
                patch.object(entry,'_operation',return_value=(reference,state)),patch.object(mapping,'ComposeDocker',return_value=self.docker),\
                patch.object(entry,'_start_locked',side_effect=selected):
            self.assertEqual(entry.continue_upgrade(request),result)
        with self.assertRaises(OSError):os.fstat(descriptor)
        another=entry._lock(cache/'entry.lock');os.close(another)

    def test_terminal_selection_refuses_if_the_same_reference_is_now_pending(self):
        request,descriptor,reference,_,_=self.selection_request()
        with patch.object(entry,'__file__',str(self.stage/'source/scripts/operator_entry.py')),\
                patch.object(entry,'_operation',return_value=(reference,{'phase':'opening'})),patch.object(entry,'_start_locked') as select:
            with self.assertRaises(mapping.DeploymentError) as caught:entry.continue_upgrade(request)
        self.assertEqual(caught.exception.code,'operator_handoff_operation_changed')
        select.assert_not_called()
        with self.assertRaises(OSError):os.fstat(descriptor)

    def test_terminal_selection_rejects_a_lock_outside_its_directory_cache(self):
        request,descriptor,reference,_,_=self.selection_request()
        value=registry.private_json(request);value['host_root']=str(self.root/'other-host');Path(value['host_root']).mkdir(mode=0o700)
        changed=request.parent/'wrong-cache.json';backup.private_write(changed,backup.json_bytes(registry.sealed(value)))
        state={'phase':'opened','project':'fixture','open_result':{'phase':'opened','project':'fixture','upgrade_complete':True}}
        with patch.object(entry,'__file__',str(self.stage/'source/scripts/operator_entry.py')),\
                patch.object(entry,'_operation',return_value=(reference,state)),patch.object(entry,'_start_locked') as select:
            with self.assertRaises((mapping.DeploymentError,FileNotFoundError)):entry.continue_upgrade(changed)
        select.assert_not_called()
        with self.assertRaises(OSError):os.fstat(descriptor)

    def indexed(self):
        prepared=self.root/'indexed';prepared.mkdir(mode=0o700)
        checked=sources.verify_stage(self.stage)
        backup.private_write(prepared/'prepared.json',backup.json_bytes({'stage_directory':str(self.stage),
            'source_stage_sha256':checked['stage_sha256'],'provenance':checked['provenance']}))
        backup.private_write(self.directory/registry.OPERATION,b'unit private operation index')
        return {'prepared_directory':str(prepared)},checked

    def test_auxiliary_commands_keep_completed_operation_source_without_querying_latest(self):
        reference,_=self.indexed()
        state={'phase':'opened','project':'fixture','open_result':{'phase':'opened','project':'fixture','upgrade_complete':True}}
        for command in ('resume','recover'):
            with self.patches(),patch.object(entry,'_operation',return_value=(reference,state)),\
                    patch.object(entry.releases,'select_latest') as latest,patch.object(mapping.Deployment,'discover') as discover,\
                    patch.object(sources,'stage') as stage:
                with self.assertRaises(Executed):entry.start(self.directory,command=command,host_root=self.cache)
            latest.assert_not_called();discover.assert_not_called();stage.assert_not_called()
            request=registry.private_json(Path(self.calls[-1]['arguments'][-1]))
            self.assertEqual((request['command'],request['intent'],request['stage_directory']),(command,command,str(self.stage)))

    def test_missing_operation_never_selects_a_new_release_for_resume_or_recover(self):
        for command in ('resume','recover'):
            with self.patches(),patch.object(entry.releases,'select_latest') as latest:
                with self.assertRaises(mapping.DeploymentError) as caught:entry.start(self.directory,command=command,host_root=self.cache)
            self.assertEqual(caught.exception.code,'upgrade_not_found')
            latest.assert_not_called()
        self.assertEqual(self.calls,[])

    def selected_request(self,command):
        path,descriptor=self.request()
        reference,checked=self.indexed()
        value=registry.private_json(path)
        value.update(command=command,intent=command,previous_operation_sha256=backup.digest(self.directory/registry.OPERATION)[1])
        selected=path.parent/'selected-request.json'
        backup.private_write(selected,backup.json_bytes(registry.sealed(value)))
        return selected,descriptor,reference,checked

    def test_completed_resume_can_return_its_result_without_another_prepare(self):
        request,descriptor,reference,_=self.selected_request('resume')
        result={'phase':'opened','upgrade_complete':True,'project':'fixture'}
        with patch.object(entry,'__file__',str(self.stage/'source/scripts/operator_entry.py')),\
                patch.object(entry,'_operation',return_value=(reference,{'phase':'opened'})),\
                patch.object(preparation,'prepare') as prepare,patch.object(Upgrade,'advance',return_value=result) as advance:
            self.assertEqual(entry.continue_upgrade(request),result)
        prepare.assert_not_called();advance.assert_called_once_with(self.directory)
        with self.assertRaises(OSError):os.fstat(descriptor)

    def test_recover_uses_selected_implementation_without_ever_advancing_forward(self):
        request,descriptor,reference,_=self.selected_request('recover')
        result={'phase':'aborted','abort_complete':True}
        with patch.object(entry,'__file__',str(self.stage/'source/scripts/operator_entry.py')),\
                patch.object(entry,'_operation',return_value=(reference,{'phase':'backing_up'})),\
                patch.object(abort,'recover',return_value=result) as recover,\
                patch.object(preparation,'prepare') as prepare,patch.object(Upgrade,'advance') as advance:
            self.assertEqual(entry.continue_upgrade(request),result)
        recover.assert_called_once_with(self.directory)
        prepare.assert_not_called();advance.assert_not_called()
        with self.assertRaises(OSError):os.fstat(descriptor)

    def test_staged_recover_rejects_a_different_source_bound_by_the_same_index(self):
        request,descriptor,reference,_=self.selected_request('recover')
        path=Path(reference['prepared_directory'])/'prepared.json'
        value=registry.private_json(path);value['source_stage_sha256']='f'*64
        path.unlink();backup.private_write(path,backup.json_bytes(value))
        with patch.object(entry,'__file__',str(self.stage/'source/scripts/operator_entry.py')),\
                patch.object(entry,'_operation',return_value=(reference,{'phase':'backing_up'})),patch.object(abort,'recover') as recover:
            with self.assertRaises(mapping.DeploymentError) as caught:entry.continue_upgrade(request)
        self.assertEqual(caught.exception.code,'operator_handoff_source_changed')
        recover.assert_not_called()
        with self.assertRaises(OSError):os.fstat(descriptor)

    def test_private_command_cannot_change_recovery_intent_into_forward_upgrade(self):
        request,descriptor,_,_=self.selected_request('recover')
        value=registry.private_json(request);value['command']='upgrade'
        changed=request.parent/'invalid-command.json';backup.private_write(changed,backup.json_bytes(registry.sealed(value)))
        try:
            with self.assertRaises(mapping.DeploymentError) as caught:entry.continue_upgrade(changed)
            self.assertEqual(caught.exception.code,'operator_handoff_invalid')
        finally:os.close(descriptor)

    def test_operation_phase_is_forward_compatible_but_non_object_material_refuses(self):
        prepared=self.root/'operation';prepared.mkdir(mode=0o700)
        reference={'id':'1','project':'fixture','project_directory':str(self.directory),'prepared_sha256':'a','prepared_directory':str(prepared)}
        state={'version':1,'phase':'future_phase',**{key:reference[key] for key in ('id','project','project_directory','prepared_sha256')}}
        state['state_sha256']=mapping.checksum(state)
        path=prepared/'upgrade-state.json';backup.private_write(path,backup.json_bytes(state))
        with patch.object(registry,'operation',return_value=reference):self.assertEqual(entry._operation(self.directory),(reference,state))
        path.unlink();backup.private_write(path,b'[]')
        with patch.object(registry,'operation',return_value=reference):
            with self.assertRaises(mapping.DeploymentError) as caught:entry._operation(self.directory)
        self.assertEqual(caught.exception.code,'upgrade_identity_changed')

    def test_real_exec_preserves_the_same_process_and_excludes_competing_entry_until_exit(self):
        program='''import fcntl,json,os,sys
request=json.load(open(sys.argv[-1]));fd=request['lock_fd']
actual=os.fstat(fd);expected=os.stat(request['lock_path'])
assert (actual.st_dev,actual.st_ino)==(expected.st_dev,expected.st_ino)
other=os.open(request['lock_path'],os.O_RDWR);blocked=False
try:fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError:blocked=True
os.close(other)
print(json.dumps({'pid':os.getpid(),'competing_lock_blocked':blocked,'cwd':os.getcwd(),
 'isolated':sys.flags.isolated,'bytecode_disabled':sys.dont_write_bytecode,'native_fixture_only':True}))
'''
        executable=self.root/'exec-stage';self.create_stage(executable,program=program)
        root=Path(__file__).resolve().parents[1]
        # This source tree is an explicit unit fixture, never public-release
        # evidence. Only the real host exec/descriptor lifetime is under test.
        driver='''import os,sys,shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
from scripts import operator_entry as entry,operator_source as sources,operator_deployment as mapping,operator_prepare as preparation
directory=Path(sys.argv[2]);host=Path(sys.argv[3]);stage=Path(sys.argv[4])
proof={'revision':'a'*40,'public_provenance_verified':True}
docker=SimpleNamespace(local_root=lambda:host.parent,containers=lambda:[],environment={'PATH':os.environ['PATH']})
deployment=SimpleNamespace(docker=SimpleNamespace(directory=directory),plan=SimpleNamespace(name='fixture'),
 summary=lambda:{'project':'fixture'},operator_phase=None)
with patch.object(mapping,'ComposeDocker',return_value=docker),patch.object(mapping.Deployment,'discover',return_value=deployment),\\
 patch.object(preparation,'host_records_outside_data'),patch.object(entry.releases,'select_latest',return_value=proof),\\
 patch.object(entry,'_operation',return_value=(None,None)),patch.object(sources,'stage',side_effect=lambda target,**_:shutil.copytree(stage,target)):
 entry.start(directory,host_root=host)
'''
        process=subprocess.Popen([sys.executable,'-I','-B','-c',driver,str(root),str(self.directory),str(self.cache),str(executable)],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        stdout,stderr=process.communicate(timeout=10)
        self.assertEqual(process.returncode,0,stderr)
        value=json.loads(stdout)
        self.assertEqual(value['pid'],process.pid)
        self.assertEqual(value['cwd'],str(self.directory))
        self.assertTrue(value['competing_lock_blocked'])
        self.assertEqual(value['isolated'],1)
        self.assertTrue(value['bytecode_disabled'])
        lock=next(self.cache.glob('*/entry.lock'))
        descriptor=entry._lock(lock);os.close(descriptor)
