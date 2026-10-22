"""Pure checks for native acceptance result and cold-operator ordering."""
import ast
import json
import os
import re
import subprocess
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tests.native_runner_cleanup import finalize_cleanup,interrupt_fixture


ROOT=Path(__file__).resolve().parents[1]


class NativeAcceptanceCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.destination=Path(self.temporary.name)/'result.json'

    def cleanup_block(self,filename,function,**values):
        tree=ast.parse((ROOT/'tests'/filename).read_text())
        owner=next(item for item in tree.body if isinstance(item,ast.FunctionDef) and item.name==function)
        block=next(item for item in ast.walk(owner) if isinstance(item,ast.Try) and any(
            isinstance(call,ast.Call) and isinstance(call.func,ast.Name) and call.func.id=='finalize_cleanup'
            for statement in item.finalbody for call in ast.walk(statement)))
        wrapper=ast.parse('def run_cleanup():\n    pass\n').body[0]
        wrapper.body=block.finalbody
        namespace={'sys':sys,'finalize_cleanup':finalize_cleanup,**values}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),filename,'exec'),namespace)
        return namespace['run_cleanup']

    def nodes_cleanup(self,report,*,peer=None,node=None,command=None):
        return self.cleanup_block('nodes_browser_native.py','main',report=report,peer=peer,node=node,
            network='owned-network',created=True,root=self.destination.parent,command=command or Mock(),
            socket=SimpleNamespace(),fleet=SimpleNamespace(_original_getaddrinfo='original'))

    def federation_cleanup(self,report,*,nodes=(),command=None,rmtree=None,diagnostics=None):
        return self.cleanup_block('federation_stack.py','main',report=report,nodes=nodes,cleanup=[],created=True,
            network='owned-network',directory=self.destination.parent,arguments=SimpleNamespace(output=self.destination),
            command=command or Mock(),shutil=SimpleNamespace(rmtree=rmtree or Mock()),
            socket=SimpleNamespace(),_original_getaddrinfo='original',
            os=SimpleNamespace(getuid=lambda:1000,getgid=lambda:1000),browser_network_failure=diagnostics or Mock())

    def test_owned_cleanup_failure_cannot_keep_a_success_receipt(self):
        report={'passed':True}
        cleanup=[{'project':'owned','stopped':False,'error':'OSError'}]
        with self.assertRaisesRegex(RuntimeError,'fixtures did not stop'):
            finalize_cleanup(report,cleanup,self.destination,business_failed=False)
        self.assertEqual(json.loads(self.destination.read_text()),{'passed':False,'cleanup':cleanup})

    def test_success_receipt_is_written_only_after_successful_cleanup(self):
        report={'passed':True}
        cleanup=[{'project':'owned','stopped':True},{'network':'owned','stopped':True}]
        finalize_cleanup(report,cleanup,self.destination,business_failed=False)
        self.assertTrue(json.loads(self.destination.read_text())['passed'])

    def test_original_business_failure_survives_cleanup_failure(self):
        original=ValueError('business check failed')
        report={'passed':True,'error':'business check failed'}
        with self.assertRaises(ValueError) as caught:
            try:
                raise original
            finally:
                finalize_cleanup(report,[{'project':'owned','stopped':False}],self.destination,
                    business_failed=sys.exc_info()[0] is not None)
        self.assertIs(caught.exception,original)
        self.assertFalse(json.loads(self.destination.read_text())['passed'])

    def test_business_failure_is_red_even_when_cleanup_succeeds(self):
        report={'passed':True}
        finalize_cleanup(report,[{'project':'owned','stopped':True}],self.destination,business_failed=True)
        self.assertFalse(json.loads(self.destination.read_text())['passed'])

    def test_player_attempts_other_nodes_and_network_after_one_stop_fails(self):
        first=SimpleNamespace(project='first',stop=Mock())
        second=SimpleNamespace(project='second',stop=Mock(side_effect=OSError('stop failed')))
        command=Mock()
        cleanup=self.cleanup_block('player_storage_browser.py','main',report={'passed':True},nodes=[first,second],
            network='owned-network',created=True,root=self.destination.parent,command=command,
            socket=SimpleNamespace(),fleet=SimpleNamespace(_original_getaddrinfo='original'))
        with self.assertRaisesRegex(RuntimeError,'fixtures did not stop'):cleanup()
        first.stop.assert_called_once_with()
        second.stop.assert_called_once_with()
        command.assert_called_once_with('docker','network','rm','owned-network')
        result=json.loads(self.destination.read_text())
        self.assertFalse(result['passed'])
        self.assertEqual([item['stopped'] for item in result['cleanup']],[False,True,True])

    def test_s3_stops_compose_and_network_after_client_close_failure(self):
        node=SimpleNamespace(project='owned-project',client=SimpleNamespace(close=Mock(side_effect=OSError('close failed'))),compose=Mock())
        command=Mock()
        cleanup=self.cleanup_block('s3_browser_native.py','run',report={'passed':True},node=node,
            network='owned-network',directory=self.destination.parent,command=command)
        with self.assertRaisesRegex(RuntimeError,'fixtures did not stop'):cleanup()
        node.compose.assert_called_once_with('down','--timeout','15')
        command.assert_called_once_with('docker','network','rm','owned-network')
        result=json.loads(self.destination.read_text())
        self.assertFalse(result['passed'])
        self.assertEqual([item['stopped'] for item in result['cleanup']],[False,True,True])

    def test_nodes_attempt_other_node_and_network_after_a_stop_failure(self):
        node=SimpleNamespace(project='admin',compose=Mock(),stop=Mock())
        peer=SimpleNamespace(project='peer',compose=Mock(),stop=Mock(side_effect=OSError('private network stop failed')))
        command=Mock()
        with self.assertRaisesRegex(RuntimeError,'fixtures did not stop'):
            self.nodes_cleanup({'passed':True},peer=peer,node=node,command=command)()
        for owned in (peer,node):
            owned.compose.assert_called_once_with('stop','--timeout','10','web')
            owned.stop.assert_called_once_with()
        command.assert_called_once_with('docker','network','rm','owned-network')
        result=json.loads(self.destination.read_text())
        self.assertFalse(result['passed'])
        self.assertEqual([row['stopped'] for row in result['cleanup']],[True,False,True,True,True])

    def test_federation_attempts_remaining_nodes_network_and_directory_after_stop_failure(self):
        first=SimpleNamespace(project='first',stop=Mock())
        second=SimpleNamespace(project='second',stop=Mock(side_effect=OSError('stop failed')))
        command=Mock();rmtree=Mock()
        with self.assertRaisesRegex(RuntimeError,'fixtures did not stop'):
            self.federation_cleanup({'result':'passed'},nodes=[first,second],command=command,rmtree=rmtree)()
        first.stop.assert_called_once_with();second.stop.assert_called_once_with()
        self.assertEqual(command.call_args_list[-1].args,('docker','network','rm','owned-network'))
        rmtree.assert_called_once_with(self.destination.parent)
        result=json.loads(self.destination.read_text())
        self.assertFalse(result['passed']);self.assertEqual(result['result'],'failed')
        self.assertEqual([row['stopped'] for row in result['cleanup']],[False,True,True,True,True])

    def test_nodes_and_federation_network_removal_failures_are_red(self):
        def command(*arguments):
            if arguments[:3]==('docker','network','rm'):raise OSError('network removal failed')
        for kind in ('nodes','federation'):
            with self.subTest(kind=kind):
                cleanup=(self.nodes_cleanup({'passed':True},command=command) if kind=='nodes' else
                    self.federation_cleanup({'result':'passed'},command=command))
                with self.assertRaisesRegex(RuntimeError,'fixtures did not stop'):cleanup()
                result=json.loads(self.destination.read_text())
                self.assertFalse(result['passed'])
                self.assertEqual(next(row for row in result['cleanup'] if 'network' in row)['stopped'],False)

    def test_nodes_and_federation_keep_the_original_business_error(self):
        for kind in ('nodes','federation'):
            with self.subTest(kind=kind):
                original=AssertionError('original business failure')
                owned=SimpleNamespace(project='owned',compose=Mock(),stop=Mock(side_effect=OSError('stop failed')),
                    failure_diagnostics=Mock(side_effect=OSError('diagnostic failed')))
                cleanup=(self.nodes_cleanup({'passed':True},node=owned) if kind=='nodes' else
                    self.federation_cleanup({'result':'failed'},nodes=[owned],diagnostics=Mock(side_effect=OSError('diagnostic failed'))))
                with self.assertRaises(AssertionError) as caught:
                    try:raise original
                    finally:cleanup()
                self.assertIs(caught.exception,original)
                owned.stop.assert_called_once_with()
                self.assertFalse(json.loads(self.destination.read_text())['passed'])

    def test_federation_temporary_directory_failure_is_red(self):
        cleanup=self.federation_cleanup({'result':'passed'},rmtree=Mock(side_effect=OSError('temporary cleanup failed')))
        with self.assertRaisesRegex(RuntimeError,'fixtures did not stop'):cleanup()
        result=json.loads(self.destination.read_text())
        self.assertFalse(result['passed']);self.assertEqual(result['result'],'failed')
        self.assertFalse(result['cleanup'][-1]['stopped'])

    def test_federation_node_still_attempts_compose_after_client_close_failure(self):
        tree=ast.parse((ROOT/'tests/federation_stack.py').read_text())
        node=next(item for item in tree.body if isinstance(item,ast.ClassDef) and item.name=='Node')
        stop=next(item for item in node.body if isinstance(item,ast.FunctionDef) and item.name=='stop')
        namespace={}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[stop],type_ignores=[])),'Node.stop','exec'),namespace)
        original=OSError('client close failed')
        owned=SimpleNamespace(client=SimpleNamespace(close=Mock(side_effect=original)),compose=Mock(side_effect=RuntimeError('down failed')))
        with self.assertRaises(OSError) as caught:namespace['stop'](owned)
        self.assertIs(caught.exception,original)
        owned.client.close.assert_called_once_with()
        owned.compose.assert_called_once_with('down','--timeout','5','--volumes','--remove-orphans')

    def test_nodes_and_federation_success_is_written_after_complete_cleanup(self):
        for kind in ('nodes','federation'):
            with self.subTest(kind=kind):
                cleanup=(self.nodes_cleanup({'passed':True}) if kind=='nodes' else self.federation_cleanup({'result':'passed'}))
                cleanup()
                result=json.loads(self.destination.read_text())
                self.assertTrue(result['passed'])
                self.assertTrue(all(row['stopped'] for row in result['cleanup']))

    def test_receipt_write_failure_fails_otherwise_successful_acceptance(self):
        destination=Mock()
        destination.write_text.side_effect=OSError('disk full')
        with self.assertRaisesRegex(OSError,'disk full'):
            finalize_cleanup({'passed':True},[],destination,business_failed=False)

    def test_receipt_write_failure_does_not_mask_original_business_failure(self):
        destination=Mock()
        destination.write_text.side_effect=OSError('disk full')
        original=AssertionError('original media failure')
        with self.assertRaises(AssertionError) as caught:
            try:
                raise original
            finally:
                finalize_cleanup({'passed':False},[],destination,business_failed=sys.exc_info()[0] is not None)
        self.assertIs(caught.exception,original)

    def test_all_hour_jobs_join_before_cold_operator_is_started(self):
        source=(ROOT/'scripts/test-python-final.sh').read_text()
        operator=source.index('bash scripts/test-python-operator.sh')
        for name in ('sqlite_hour_pid','gui_pid','success_hour_pid'):
            self.assertLess(source.index('wait "$'+name+'"'),operator)
        self.assertLess(source.index('"$evidence/migration-browser-native.json"'),operator)

    def test_successful_cleanup_hour_keeps_the_standalone_resource_limits_and_flags(self):
        source=(ROOT/'scripts/test-python-final.sh').read_text()
        begin=source.index('docker run --rm --name "fc-native-success-hour-$unit_token"')
        end=source.index('success_hour_pid=$!',begin)
        command=source[begin:end]
        standalone=(ROOT/'scripts/test-python-migration-hour.sh').read_text()
        for required in ('--cpus 1 --memory 512m --network none --user 0','DB_TYPE=sqlite',
            'FRONTIERCLOUD_PUBLISH_NATIVE=1','FRONTIERCLOUD_CUTOVER_NATIVE=1','FRONTIERCLOUD_CLEANUP_NATIVE=1',
            'frontiercloud-python-unit-tools timeout --kill-after=10s 4100s',
            'python -m tests.resource_transfer_native'):
            with self.subTest(required=required):
                self.assertIn(required,command)
                self.assertIn(required,standalone)
        self.assertTrue(command.rstrip().endswith('&'))
        self.assertLess(source.index('gui_pid=$!'),begin)
        self.assertLess(begin,source.index('FC_PLAYER_STORAGE_ROOT='))

    def test_successful_hour_job_is_owned_by_failure_cleanup(self):
        source=(ROOT/'scripts/test-python-final.sh').read_text()
        cleanup=source.split('cleanup() {',1)[1].split('\n}',1)[0]
        self.assertIn('success_hour_pid=\'\'',source[:source.index('cleanup() {')])
        self.assertIn('"$success_hour_pid"',cleanup)
        self.assertIn('unit_names+=("fc-native-success-hour-$unit_token")',source)
        self.assertIn('docker rm -f "${unit_names[@]}"',cleanup)
        self.assertIn('wait "$fixture_pid"',cleanup)

    def test_manual_collector_preserves_failed_hour_and_invalid_json_diagnostics(self):
        from scripts.collect_native_evidence import collect
        root=Path(self.temporary.name);evidence=root/'evidence';evidence.mkdir();upload=root/'export'
        contents={'migration-hour-source-commit.txt':'a'*40+'\n',
            'migration-hour.json':'{"passed": false, "error": "InterruptedError"}\n',
            'migration-hour.log':'native job interrupted\n',
            'native-publication.json':'{"passed": false,', 'unit-coverage.json':''}
        for name,value in contents.items():(evidence/name).write_text(value,encoding='utf-8')
        manifest=collect(evidence,upload)
        self.assertFalse(manifest['complete'])
        records={entry['source']:entry for entry in manifest['entries']}
        for name in ('migration-hour-source-commit.txt','migration-hour.json','migration-hour.log'):
            self.assertEqual((upload/name).read_text(),contents[name])
            self.assertEqual(records[name]['status'],'copied')
        self.assertFalse(json.loads((upload/'migration-hour.json').read_text())['passed'])
        for name in ('native-publication.json','unit-coverage.json'):
            self.assertFalse((upload/name).exists())
            self.assertEqual((upload/(name+'.raw.txt')).read_text(),contents[name])
            self.assertEqual(records[name]['status'],'invalid_json')
        self.assertEqual(json.loads((upload/'manifest.json').read_text()),manifest)
        if os.name=='posix':
            self.assertEqual(upload.stat().st_mode&0o777,0o700)
            self.assertTrue(all(path.stat().st_mode&0o777==0o600 for path in upload.iterdir()))
        with self.assertRaisesRegex(ValueError,'fresh_destination'):collect(evidence,upload)
        with self.assertRaisesRegex(ValueError,'overlapping'):collect(evidence,evidence/'nested')
        with self.assertRaisesRegex(ValueError,'unsafe_path'):collect(evidence,root/'export'/'..'/'escape')
        command=[sys.executable,'-B',str(ROOT/'scripts/collect_native_evidence.py'),str(evidence),str(root/'cli-export')]
        process=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,text=True)
        self.assertEqual(process.returncode,1,process.stderr)
        self.assertFalse(json.loads(process.stdout)['complete'])
        self.assertEqual((root/'cli-export/native-publication.json.raw.txt').read_text(),contents['native-publication.json'])

    def test_manual_evidence_allowlist_covers_runners_and_only_known_operator_cases(self):
        from scripts.collect_native_evidence import allowlist,collect,PREPARE_CASES,INSTALL_CASES,APPLICATION_CASES
        allowed=allowlist()
        for script in ('test-python-final.sh','test-python-operator.sh'):
            source=(ROOT/'scripts'/script).read_text()
            outputs=re.findall(r'>\s*"\$evidence/([^/"\n]+)"',source)
            pairs=re.search(r'for pair in (.*?); do',source).group(1).split()
            for output in outputs:
                expanded=[output.replace('$pair',pair) for pair in pairs] if '$pair' in output else [output]
                for name in expanded:
                    with self.subTest(script=script,name=name):self.assertIn(name,allowed)
        for module,kind,cases in (('operator_prepare_native','operator-prepare',PREPARE_CASES),
                ('operator_install_native','operator-install',INSTALL_CASES),
                ('operator_application_native','operator-application',APPLICATION_CASES)):
            tree=ast.parse((ROOT/'tests'/(module+'.py')).read_text())
            actual={item.name for owner in tree.body if isinstance(owner,ast.ClassDef)
                for item in owner.body if isinstance(item,ast.FunctionDef) and item.name.startswith('test_')}
            with self.subTest(module=module):
                self.assertEqual(actual,set(cases))
                self.assertTrue(all(f'private/{kind}/{case}/result.json' in allowed for case in actual))
        root=Path(self.temporary.name);evidence=root/'evidence';evidence.mkdir();upload=root/'export'
        safe=('native-partial-cancel-mysql-RR.jsonl','unit-shards/shard-2.json','migration-ui/result.json',
            f'private/operator-install/{INSTALL_CASES[0]}/result.json')
        private=('unknown.json','private/vault.key','private/ca.pem','private/media/movie.mp4',
            's3/private/credentials.json','s3/private/data/db.sqlite3','private/operator-install/test_unknown/result.json')
        for name in safe+private:
            path=evidence/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('{"passed": false}\n' if name in safe else 'private-fixture-secret',encoding='utf-8')
        manifest=collect(evidence,upload)
        self.assertTrue(manifest['complete'])
        self.assertEqual(set(path.name for path in upload.iterdir()),{allowed[name] for name in safe}|{'manifest.json'})
        self.assertEqual(manifest['copied'],len(safe))
        # Even an allowlisted filename cannot export a hard link to private data.
        os.link(evidence/'private/vault.key',evidence/'federation.json')
        rejected=collect(evidence,root/'hardlink-export')
        self.assertFalse(rejected['complete'])
        self.assertFalse((root/'hardlink-export/federation.json').exists())
        try:os.symlink(evidence/'private/ca.pem',evidence/'native-inventory.json')
        except OSError:
            if os.name!='nt':raise
        else:
            linked=collect(evidence,root/'symlink-export')
            self.assertFalse(linked['complete'])
            self.assertFalse((root/'symlink-export/native-inventory.json').exists())

    def test_manual_s3_allowlist_and_content_scanning_reject_usable_secrets(self):
        from scripts.collect_native_evidence import collect,S3_FILES
        allowed=set(S3_FILES)
        source=(ROOT/'scripts/test-python-s3.sh').read_text()
        public=set(re.findall(r'"\$evidence/([^/"\n]+\.(?:json|jsonl|log))"',source))
        private=set(re.findall(r'"\$private/([^/"\n]+\.(?:json|jsonl|log))"',source))
        # Fixture construction output is not a sanitized business receipt.
        public.discard('fixture.log');private.add('fixture.log')
        self.assertTrue(public)
        self.assertLessEqual(public,allowed)
        self.assertFalse(private.intersection(allowed))
        self.assertTrue(all(re.fullmatch(r'[a-z0-9-]+\.(?:json|jsonl|log)',name) for name in allowed))
        self.assertNotIn('credential-revocation-provider.log',allowed)
        root=Path(self.temporary.name);evidence=root/'evidence';(evidence/'s3').mkdir(parents=True)
        (evidence/'s3/browser.json').write_text('{"passed": false, "csrf_present": true}\n')
        (evidence/'s3/credential-revocation-provider.log').write_text('private-fixture-secret')
        samples=('-----BEGIN PRIVATE KEY----- private-fixture-secret',
            '{"url":"https://s3.example.test/a?X-Amz-Signature=private-fixture-secret"}',
            '{"url":"https://s3.example.test/a?X%252dAmz%252dCredential=private-fixture-secret"}',
            '{"headers":{"Authorization":"Bearer private-fixture-secret"}}',
            '{"error":"Authorization: Bearer private-fixture-secret"}',
            '{"accessKey":"private-fixture-secret"}', '{"token":"private-fixture-secret"}',
            '{"admin_key":"private-fixture-secret",', 'token=private-fixture-secret',
            'https://example.test/a?grant=private-fixture-secret')
        for index,sample in enumerate(samples):
            with self.subTest(sample=index):
                (evidence/'s3/transactions.json').write_text(sample)
                upload=root/f'export-{index}'
                manifest=collect(evidence,upload)
                self.assertFalse(manifest['complete'])
                self.assertTrue((upload/'s3-browser.json').exists())
                self.assertFalse(any(path.name.startswith('s3-transactions') for path in upload.iterdir()))
                self.assertFalse((upload/'s3-credential-revocation-provider.log').exists())
                self.assertNotIn('private-fixture-secret',''.join(path.read_text() for path in upload.iterdir()))
        # Redacted digests are useful diagnostics, not usable credentials.
        (evidence/'s3/transactions.json').write_text(json.dumps({'secret_key':{'redacted':True,'sha256':'a'*64}}))
        redacted=collect(evidence,root/'redacted-export')
        self.assertTrue(redacted['complete'],[entry for entry in redacted['entries'] if entry['status'] not in {'missing','copied'}])

    def test_manual_operator_collection_keeps_actual_argv_without_private_state(self):
        from scripts.collect_native_evidence import collect
        root=Path(self.temporary.name);evidence=root/'evidence';case=evidence/'private/operator-install/test_example_entry_install'
        case.mkdir(parents=True);upload=root/'export'
        argv=['python3','-I','-B','/fixture/tests/operator_install_handoff_fixture.py',
            '--directory','/fixture/deploy','--stage','/fixture/source','--command','install','--lose','start']
        def write(name,value):
            path=case/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value))
        write('cli-install-initial-input.json',dict(argv=argv,working_directory='/fixture/deploy',phase='starting'))
        write('cli-install-initial-result.json',dict(argv=argv,exit_code=35,timed_out=False,diagnostic_sha256='a'*64))
        write('http-request.json',dict(admin_key='private-fixture-secret'))
        write('private-env-input.json',dict(argv=['docker','inspect','private'],environment={'ADMIN_KEY':'private-fixture-secret'}))
        write('entry-host/attempt-a/request.json',dict(admin_key='private-fixture-secret'))
        write('entry-host/attempt-a/handoff-input.json',dict(argv=['python3','-I','-B',
            '/fixture/scripts/frontiercloud_operator.py','--operator-handoff','/fixture/request.json'],intent='new_install'))
        selection=ast.parse((ROOT/'tests/operator_selection_fixture.py').read_text())
        real_context=next(node for node in ast.walk(selection) if isinstance(node,ast.Dict) and any(
            isinstance(key,ast.Constant) and key.value=='simulated_old_discovery_rejects' for key in node.keys))
        context_value=next(ast.literal_eval(value) for key,value in zip(real_context.keys,real_context.values)
            if isinstance(key,ast.Constant) and key.value=='simulated_old_discovery_rejects')
        write('selection-real-exec-current-old-input.json',dict(argv=['python3','-I','-B',
            '/fixture/tests/operator_selection_fixture.py','--phase','old','--directory','/fixture/deploy','--stage','/fixture/stage'],
            fixture_only=True,simulated_old_discovery_rejects=context_value,inherited_lock_fd=4,
            intent='new_upgrade',working_directory='/fixture/deploy'))
        (case/'cli-install-initial.log').write_text('PRIVATE KEY----- private-fixture-secret')
        # Invalid host receipts stay private but do not discard earlier diagnostics.
        (case/'cli-broken-input.json').write_text('{"argv":')
        manifest=collect(evidence,upload)
        self.assertFalse(manifest['complete'])
        self.assertEqual(manifest['copied'],4)
        collected=''.join(path.read_text() for path in upload.iterdir())
        self.assertIn('operator_install_handoff_fixture.py',collected)
        self.assertIn('frontiercloud_operator.py',collected)
        self.assertNotIn('private-fixture-secret',collected)
        self.assertTrue(all(path.suffix=='.json' for path in upload.iterdir()))
        self.assertFalse(any('cli-broken' in path.name for path in upload.iterdir()))
        broken=next(entry for entry in manifest['entries'] if entry['source'].endswith('cli-broken-input.json'))
        self.assertEqual(broken['status'],'rejected')
        # A matching input cannot lend authenticity to a different child result.
        write('cli-install-initial-result.json',dict(argv=argv+['--leave-helper'],exit_code=0))
        second=collect(evidence,root/'mismatched-export')
        self.assertFalse(second['complete'])
        self.assertFalse((root/'mismatched-export/operator-test_example_entry_install-cli-install-initial-result.json').exists())

    def test_timeout_termination_enters_the_exception_cleanup_path(self):
        with self.assertRaisesRegex(InterruptedError,'fixture interrupted'):
            interrupt_fixture(15,None)
        for filename in ('player_storage_browser.py','s3_browser_native.py','nodes_browser_native.py','federation_stack.py'):
            with self.subTest(filename=filename):
                tree=ast.parse((ROOT/'tests'/filename).read_text())
                entry=next(item for item in tree.body if isinstance(item,ast.If)
                    and ast.unparse(item.test)=="__name__ == '__main__'")
                self.assertEqual(ast.unparse(entry.body[0]),'signal.signal(signal.SIGTERM, interrupt_fixture)')


if __name__=='__main__':unittest.main()
