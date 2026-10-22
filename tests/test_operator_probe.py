"""Endpoint probes bind ordinary TLS and fresh signed node identity."""
import hashlib
import contextlib
import io
import json
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

from app.services import operator_probe as probe
from app.services import offline_upgrade as offline
from app.services.federation import protocol


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.private = protocol.new_key()
        self.public = protocol.public_key(self.private)
        self.settings = SimpleNamespace(TLS_ENABLED=True,DEPLOYMENT_MODE='',SERVER_NAME='operator.example.test',STORAGE_ENDPOINT='')
        self.baseline = {'node_id':'a'*32,'role':'Standalone','endpoint':'',
            'public_key_sha256':hashlib.sha256(self.public.encode()).hexdigest()}
        self.challenges = []

    def response(self, *, fenced=True, mutation=None):
        def fetch(endpoint,path,**_options):
            self.assertEqual(endpoint,'https://operator.example.test')
            if path == '/health/ready':return 200,b'{"status":"ready"}','leaf'
            if path.startswith('/internal/v1/identity?challenge='):
                challenge = path.split('=',1)[1]
                self.challenges.append(challenge)
                payload = {'node_id':'a'*32,'role':'Standalone','endpoint':'','public_key':self.public,
                    'challenge':challenge,'protocol':protocol.PROTOCOL_VERSION}
                if mutation:payload.update(mutation)
                return 200,json.dumps(protocol.sign(self.private,payload)).encode(),'leaf'
            self.assertEqual(path,'/api/v1/media/')
            return 503 if fenced else 200,b'','leaf'
        return fetch

    def test_fenced_and_open_probes_use_new_challenges_and_do_not_claim_media_reads(self):
        with patch.object(probe,'fetch',self.response()):
            first = probe.network_probe(self.settings,self.baseline,fenced=True)
        with patch.object(probe,'fetch',self.response(fenced=False)):
            second = probe.network_probe(self.settings,self.baseline,fenced=False)
        self.assertTrue(first['signed_identity_verified'])
        self.assertTrue(second['certificate_verified'])
        self.assertFalse(first['media_read_verified'])
        self.assertEqual((first['business_entry_status'],second['business_entry_status']),(503,200))
        self.assertEqual(len(set(self.challenges)),2)

    def test_signed_but_wrong_identity_role_challenge_or_protocol_is_refused(self):
        for mutation in ({'node_id':'b'*32},{'role':'Master'},{'challenge':'c'*32},{'protocol':-1}):
            with self.subTest(mutation=mutation),patch.object(probe,'fetch',self.response(mutation=mutation)),self.assertRaises(offline.PreparationError) as caught:
                probe.network_probe(self.settings,self.baseline,fenced=True)
            self.assertEqual(caught.exception.code,'probe_identity_mismatch')

    def test_http_standalone_reports_database_only_identity_and_fixed_roles_refuse(self):
        self.settings.TLS_ENABLED = False
        def fetch(endpoint,path,**_options):
            self.assertEqual(endpoint,'http://nginx')
            return (200,b'{"status":"ready"}',None) if path == '/health/ready' else (503,b'',None)
        with patch.object(probe,'fetch',fetch):
            result = probe.network_probe(self.settings,self.baseline,fenced=True)
        self.assertFalse(result['signed_identity_verified'])
        self.assertFalse(result['certificate_verified'])
        self.baseline['role'] = 'Master'
        with patch.object(probe,'fetch') as request,self.assertRaises(offline.PreparationError):
            probe.network_probe(self.settings,self.baseline,fenced=True)
        request.assert_not_called()

    def test_business_gate_cannot_be_mistaken_for_readiness(self):
        with patch.object(probe,'fetch',self.response(fenced=False)),self.assertRaises(offline.PreparationError) as caught:
            probe.network_probe(self.settings,self.baseline,fenced=True)
        self.assertEqual(caught.exception.code,'probe_business_gate_mismatch')

    def test_certificate_failures_remain_specific_and_context_requires_hostname_verification(self):
        for code,kind in ((10,'expired'),(62,'hostname'),(20,'untrusted')):
            error = ssl.SSLCertVerificationError(1,'fixture failure')
            error.verify_code = code
            connection = Mock()
            connection.request.side_effect = error
            with patch.object(probe.http.client,'HTTPSConnection',return_value=connection) as factory,self.assertRaises(offline.PreparationError) as caught:
                probe.fetch('https://operator.example.test','/health/ready')
            self.assertEqual(caught.exception.code,'probe_certificate_'+kind)
            context = factory.call_args.kwargs['context']
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
            connection.close.assert_called_once()

    def test_control_response_size_is_bounded(self):
        connection = Mock()
        connection.sock.getpeercert.return_value = b'fixture DER'
        connection.getresponse.return_value.read.return_value = b'x'*65537
        with patch.object(probe.http.client,'HTTPSConnection',return_value=connection),self.assertRaises(offline.PreparationError) as caught:
            probe.fetch('https://operator.example.test','/health/ready')
        self.assertEqual(caught.exception.code,'probe_response_too_large')

    def test_module_cli_preserves_a_probe_error_code_across_module_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline = Path(temporary)/'baseline.json'
            baseline.write_bytes(b'{}')
            baseline.chmod(0o600)
            result = subprocess.run([sys.executable,'-m','app.services.offline_upgrade','probe-fenced',
                '--fence-sha256','a'*64,'--node-id','a'*32,'--baseline',str(baseline),
                '--baseline-sha256','0'*64],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,1,result.stderr)
        self.assertEqual(json.loads(result.stdout)['error'],'baseline_changed')

    def test_open_probe_does_not_require_a_fictitious_maintenance_fence(self):
        for command in ('probe-open','probe-restored'):
            operation=AsyncMock(return_value={'version':1,'phase':'fixture'})
            with patch.object(probe,'execute',operation),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(offline.main([command,'--node-id','a'*32,'--baseline','retained.json',
                    '--baseline-sha256','b'*64]),0)
            self.assertIsNone(operation.call_args.args[0].fence_sha256)

    def test_fenced_probe_still_requires_fence_and_open_probe_rejects_malformed_bindings(self):
        for arguments in (["probe-fenced",'--node-id','a'*32,'--baseline','retained.json','--baseline-sha256','b'*64],
                ['probe-open','--node-id','a'*32,'--baseline','retained.json','--baseline-sha256','b'*64,'--fence-sha256','bad'],
                ['probe-open','--node-id','a'*32]):
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as caught:offline.main(arguments)
            self.assertEqual(caught.exception.code,2)


class OpenMarkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_broken_manual_maintenance_symlink_blocks_open_before_database_or_network(self):
        from app.services import runtime_gate
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'.frontiercloud-maintenance').symlink_to(root/'missing-marker')
            raw=b'{}'
            options=SimpleNamespace(command='probe-open',baseline='fixture',baseline_sha256=hashlib.sha256(raw).hexdigest())
            with patch.object(runtime_gate,'DATA_ROOT',root),patch.object(offline,'private_bytes',return_value=raw),\
                    patch.object(probe,'network_probe') as network,self.assertRaises(offline.PreparationError) as caught:
                await probe.execute(options)
        self.assertEqual(caught.exception.code,'maintenance_not_open');network.assert_not_called()
