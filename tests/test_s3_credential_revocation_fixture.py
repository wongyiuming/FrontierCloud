"""Pure host-helper confinement checks; no Docker command is executed."""
import ast
import copy
from contextlib import redirect_stderr
import hashlib
import io
import json
import re
import stat
import sys
from pathlib import Path
import tempfile
import unittest

from tests import s3_credential_revocation_fixture as helper


class RevocationFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'s3-compatible-fixture'
        self.project='fc-python-s3-'+hashlib.sha256(str(self.root).encode()).hexdigest()[:12]
        self.identity='a'*64;self.calls=[];self.writes=[];self.inspects=0
        self.config={'identities':[dict(name='fixture-admin',credentials=[dict(accessKey='admin',secretKey='admin-secret')]),
            dict(name='frontiercloud-member',actions=['Read:'+helper.BUCKET,'Write:'+helper.BUCKET,'List:'+helper.BUCKET],
                credentials=[dict(accessKey='normal',secretKey='normal-secret'),dict(accessKey='revoked',secretKey='revoked-secret')])]}
        self.values={'credentials.json':self.config,'manifest.json':dict(format='frontiercloud-python-confined-s3-fixture',version=1,
            project=self.project,network=self.project,image=helper.IMAGE,bucket=helper.BUCKET,endpoint='https://s3.4399.com:8333',
            exposed_host_ports=[],private_data_retained=True,aws_real_bucket=False),
            'compose.json':dict(name=self.project,services={'s3':dict(image=helper.IMAGE,cpus=1,mem_limit='512m',
                command=['-s3.config=/fixture/credentials.json'])},networks={'fixture':dict(name=self.project,internal=True)}),
            'revocation.json':dict(format='frontiercloud-s3-credential-revocation',version=1,phase='prepared',grant_until=1300,
                revoked_access_key_sha256=hashlib.sha256(b'revoked').hexdigest())}
        self.container=dict(Id=self.identity,Image='sha256:'+'b'*64,RestartCount=0,
            Config=dict(Image=helper.IMAGE,Labels={'com.docker.compose.project':self.project,'com.docker.compose.service':'s3',
                'frontiercloud.acceptance':'python-s3-compatible'}),State=dict(Running=True,Pid=42),HostConfig={},
            NetworkSettings=dict(Networks={self.project:{}},Ports={'8333/tcp':None}),Mounts=[
                dict(Destination='/data',Source=str(self.root/'data'),RW=True),
                *[dict(Destination='/fixture/'+name,Source=str(self.root/name),RW=False)
                  for name in ('credentials.json','server.pem','server-key.pem')]])
        self.image=dict(Id=self.container['Image'],RepoDigests=[helper.IMAGE])
        self.network=dict(Name=self.project,Internal=True,Labels={'com.docker.compose.project':self.project},Containers={self.identity:{}})
        self.after=None

    def command(self,*args):
        self.calls.append(args)
        if args[1]=='compose':return self.identity
        if args[1]=='inspect':
            self.inspects+=1
            return json.dumps([self.container if self.inspects==1 or self.after is None else self.after])
        if args[1]=='image':return json.dumps([self.image])
        if args[1]=='network':return json.dumps([self.network])
        if args[1]=='kill':return self.identity
        raise AssertionError('unexpected_command')

    def revoke(self):
        return helper.revoke(self.root,command=self.command,reader=lambda path:copy.deepcopy(self.values[path.name]),
            writer=lambda path,value,replace=False:self.writes.append((path.name,copy.deepcopy(value),replace)),clock=lambda:1000)

    def test_only_secondary_key_changes_and_hup_keeps_exact_provider_pid(self):
        result=self.revoke()
        self.assertTrue(result['passed']);self.assertEqual((result['pid_before'],result['pid_after']),(42,42))
        self.assertEqual(self.writes[0][1],self.config)
        changed=self.writes[1]
        self.assertEqual(changed[0:1],('credentials.json',));self.assertTrue(changed[2])
        self.assertEqual(changed[1]['identities'][0],self.config['identities'][0])
        self.assertEqual(changed[1]['identities'][1]['credentials'],self.config['identities'][1]['credentials'][:1])
        self.assertEqual([call for call in self.calls if call[1]=='kill'],[('docker','kill','--signal','HUP',self.identity)])

    def test_wrong_owner_image_mount_network_deadline_or_key_never_mutates(self):
        mutations=[lambda:self.values['manifest.json'].update(image='untrusted'),
            lambda:self.container['Config']['Labels'].update({'com.docker.compose.project':'foreign'}),
            lambda:self.container['Mounts'].append(dict(Destination='/var/run/docker.sock',Source='/var/run/docker.sock',RW=True)),
            lambda:self.container['HostConfig'].update(PortBindings={'8333/tcp':[dict(HostPort='8333')]}),
            lambda:self.network.update(Internal=False),lambda:self.image.update(Id='sha256:'+'c'*64),
            lambda:self.values['revocation.json'].update(grant_until=1050),
            lambda:self.values['revocation.json'].update(revoked_access_key_sha256='f'*64),
            lambda:self.config['identities'][1]['credentials'].pop(),
            lambda:self.config['identities'][1].update(actions=['Admin'])]
        baseline=copy.deepcopy((self.values,self.container,self.image,self.network,self.config))
        for index,mutate in enumerate(mutations):
            self.values,self.container,self.image,self.network,self.config=copy.deepcopy(baseline)
            self.calls.clear();self.writes.clear();self.inspects=0
            mutate()
            with self.subTest(index=index),self.assertRaises(RuntimeError):self.revoke()
            self.assertEqual(self.writes,[])
            self.assertFalse(any(call[1]=='kill' for call in self.calls))

    def test_changed_pid_is_failure_and_cannot_publish_success_receipt(self):
        self.after=copy.deepcopy(self.container);self.after['State']['Pid']=43
        with self.assertRaisesRegex(RuntimeError,'restarted_or_replaced'):self.revoke()
        self.assertEqual(len(self.writes),2)

    def test_config_rewrite_preserves_bind_mount_inode_owner_and_mode(self):
        path=Path(self.temp.name)/'credentials.json'
        path.write_text(json.dumps(dict(long_previous_value='x'*1000)));path.chmod(0o640)
        before=path.stat()
        helper.write(path,dict(short_value=1),True)
        after=path.stat()
        self.assertEqual((after.st_ino,after.st_uid,stat.S_IMODE(after.st_mode)),
                         (before.st_ino,before.st_uid,stat.S_IMODE(before.st_mode)))
        self.assertEqual(json.loads(path.read_text()),dict(short_value=1))


class RevocationCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path=Path(__file__).with_name('s3_credential_revocation_native.py')
        tree=ast.parse(path.read_text())
        functions=[node for node in tree.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and
            node.name in {'safe_failure','dispose_preserving_primary'}]
        main=next(node for node in tree.body if isinstance(node,ast.AsyncFunctionDef) and node.name=='main')
        body=next(node for node in main.body if isinstance(node,ast.Try) and any(isinstance(call,ast.Call) and
            isinstance(call.func,ast.Name) and call.func.id=='dispose_preserving_primary'
            for statement in node.finalbody for call in ast.walk(statement)))
        # Execute the actual main exception/finally clauses around controlled
        # business work, without importing app dependencies or using a fixture.
        body=copy.deepcopy(body)
        body.body=ast.parse('if business_error is not None:\n    raise business_error\nreturn "business_success"').body
        wrapper=ast.parse('async def execute(database,business_error):\n    pass').body[0]
        wrapper.body=[ast.Assign(targets=[ast.Name(id='primary',ctx=ast.Store())],value=ast.Constant(None)),body]
        namespace={'json':json,'re':re,'sys':sys}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[*functions,wrapper],type_ignores=[])),str(path),'exec'),namespace)
        self.execute=namespace['execute']

    def database(self,failure=None):
        class Database:
            calls=0
            async def dispose(self):
                self.calls+=1
                if failure is not None:raise failure
        return Database()

    async def test_cleanup_failure_preserves_business_error_and_redacts_diagnostic(self):
        class SensitiveFailure(RuntimeError):
            code='https://private/?X-Amz-Signature=do-not-print'
        failure=SensitiveFailure('secret request and configuration')
        failure.code=SensitiveFailure.code
        primary=ValueError('business-stage-failed');database=self.database(failure);output=io.StringIO()
        with redirect_stderr(output),self.assertRaises(ValueError) as caught:
            await self.execute(database,primary)
        self.assertIs(caught.exception,primary);self.assertEqual(database.calls,1)
        self.assertEqual(json.loads(output.getvalue()),dict(cleanup_failed=True,error_type='SensitiveFailure',code='native_cleanup_failed'))
        self.assertNotIn('private',output.getvalue());self.assertNotIn('secret',output.getvalue())
        failure.code='s3_timeout';output=io.StringIO()
        with redirect_stderr(output),self.assertRaises(ValueError) as caught:await self.execute(database,primary)
        self.assertIs(caught.exception,primary);self.assertEqual(json.loads(output.getvalue())['code'],'s3_timeout')

    async def test_cleanup_failure_without_prior_error_still_fails(self):
        failure=OSError('dispose failed');database=self.database(failure);output=io.StringIO()
        with redirect_stderr(output),self.assertRaises(OSError) as caught:
            await self.execute(database,None)
        self.assertIs(caught.exception,failure);self.assertEqual(database.calls,1);self.assertEqual(output.getvalue(),'')

    async def test_successful_cleanup_preserves_business_success_and_failure(self):
        self.assertEqual(await self.execute(self.database(),None),'business_success')
        failure=ValueError('original failure')
        with self.assertRaises(ValueError) as caught:await self.execute(self.database(),failure)
        self.assertIs(caught.exception,failure)

    async def test_broken_cleanup_diagnostic_stream_cannot_hide_primary_error(self):
        class Broken:
            def write(self,value):raise OSError('diagnostic output unavailable')
        failure=ValueError('original failure')
        with redirect_stderr(Broken()),self.assertRaises(ValueError) as caught:
            await self.execute(self.database(RuntimeError('cleanup failure')),failure)
        self.assertIs(caught.exception,failure)
