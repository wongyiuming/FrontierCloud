"""Actual published Python application -> canonical candidate acceptance.

Images use the real old/current Dockerfiles. Only this fixture's host port
publications and fixed container names are isolated; business code is unchanged.
"""
import copy
import errno
import hashlib
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import ssl
import subprocess
import tarfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request
import uuid

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_prepare as prepare
from scripts import operator_upgrade as upgrade
from scripts import operator_source as source_delivery
from scripts import operator_recovery as recovery
from scripts import operator_abort as abort
from scripts import operator_entry as host_entry
from scripts import operator_registry as registry
from tests import operator_deployment_native as native
from tests import operator_prepare_native as components

ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, endpoint, audit, ca):
        self.endpoint, self.audit = endpoint, audit
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.cookies),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(ca))))

    def request(self, method, path, data=None, headers=None):
        body = data or b''
        headers = dict(headers or {})
        if method not in {'GET', 'HEAD'}:
            csrf = next((row.value for row in self.cookies if row.name in {'admin_csrf','__Host-admin-csrf'}), None)
            if csrf:
                headers['X-CSRF-Token'] = csrf
        identifier = uuid.uuid4().hex
        event = {'id': identifier, 'phase': 'input', 'method': method, 'path': path.split('?',1)[0],
            'body_bytes': len(body), 'body_sha256': hashlib.sha256(body).hexdigest(),
            'csrf_present': 'X-CSRF-Token' in headers, 'credentials_redacted': True}
        event['safe_headers']={key.lower():value for key,value in headers.items() if key.lower() in {'range','origin','content-type'}}
        event['safe_query']={key:value for key,value in urllib.parse.parse_qsl(urllib.parse.urlsplit(path).query)
            if key in {'paths','resource_id','file_path','download'}}
        self.audit(event)
        request = urllib.request.Request(self.endpoint+path, data=data, method=method, headers=headers)
        try:
            response = self.opener.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read()
            result = response.status, dict(response.headers), content
        self.audit({'id': identifier, 'phase': 'result', 'status': result[0],
            'body_bytes': len(content), 'body_sha256': hashlib.sha256(content).hexdigest()})
        return result


@unittest.skipUnless(os.environ.get('FC_OPERATOR_APPLICATION_RUNTIME') == '1', 'explicit isolated canonical application acceptance')
class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native.NativeDiscoveryTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.evidence = getattr(self,'fixture_evidence',backup.absolute(os.environ['FC_OPERATOR_APPLICATION_EVIDENCE'])/self._testMethodName)
        self.evidence.mkdir(mode=0o700)
        self.hostname=getattr(self,'fixture_hostname','operator.4399.com')
        self.project = 'fc-op-app-'+uuid.uuid4().hex[:12]
        self.directory = self.evidence/'deployment'
        self.directory.mkdir(mode=0o755)
        self.data = self.evidence/'control'
        self.docker = mapping.ComposeDocker(self.directory, [], self.project, [])
        self.docker.local_root()
        self.prepared = None
        self.events = self.evidence/'http-inputs.jsonl'
        self.ca = self.evidence/'test-ca'
        self.ca.mkdir(mode=0o700)
        self.created_volumes = []
        self.clean_images = {}
        self.addCleanup(self.remove_early_volumes)

    def remove_early_volumes(self):
        for name in self.created_volumes:
            value = self.docker.volume(name)
            if value is None: continue
            self.assertEqual(value['Labels'][mapping.LABEL+'project'],self.project)
            self.assertFalse(self.fixture.run_command(['docker','ps','-aq','--filter','volume='+name]))
            self.fixture.run_command(['docker','volume','rm',name])

    def create_ca(self):
        name = self.project+'_certificate_state'
        self.assertIsNone(self.docker.volume(name))
        self.fixture.run_command(['docker','volume','create','--label',mapping.LABEL+'project='+self.project,
            '--label',mapping.LABEL+'volume=certificate_state',name])
        self.created_volumes.append(name)
        tools = os.environ.get('FC_OPERATOR_DISCOVERY_IMAGE','frontiercloud-python-archive:db-test-tools')
        program = """from tests.certificate_fixtures import TestCA
from cryptography.hazmat.primitives import serialization
import pathlib
ca=TestCA();root=pathlib.Path('/private/operator-test-ca');root.mkdir(mode=0o700)
(root/'private.pem').write_bytes(ca.key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
(root/'private.pem').chmod(0o600)
public=ca.root.public_bytes(serialization.Encoding.PEM)
(root/'root.crt').write_bytes(public);pathlib.Path('/out/root.crt').write_bytes(public)
"""
        prepare._command(self.docker,['docker','run','--rm','--network','none','--user','0','--cpus','1','--memory','128m',
            '--mount','type=volume,source='+name+',target=/private','--mount','type=bind,source='+str(self.ca)+',target=/out',
            tools,'python','-c',program],self.evidence,'test-ca-create')

    def trust(self, tag, kind, directory, step):
        clean = self.docker.run(['docker','image','inspect',tag],json_output=True)[0]['Id']
        self.clean_images[step] = clean
        context = directory/(step+'-trust')
        context.mkdir(mode=0o700)
        shutil.copyfile(self.ca/'root.crt',context/'root.crt')
        (context/'Dockerfile').write_text('FROM '+clean+'\nUSER 0\nCOPY root.crt /usr/local/share/ca-certificates/operator-test-only-root.crt\nRUN update-ca-certificates\n'+('USER 10001:10001\n' if kind=='web' else ''))
        prepare._command(self.docker,['docker','build','--tag',tag,'--file',str(context/'Dockerfile'),str(context)],directory,step+'-trust-build')

    def candidate_build(self, docker, arguments, directory, step, **options):
        prepare._command(docker,arguments,directory,step,**options)
        if step == 'image-build':
            config = docker.config()
            for kind in ('web','nginx'):
                if kind in config['services']:
                    self.trust(config['services'][kind]['image'],kind,directory,'candidate-'+kind)

    def audit(self, event):
        descriptor = os.open(self.events, os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'ab') as stream:
            stream.write(backup.json_bytes(event)+b'\n');stream.flush();os.fsync(stream.fileno())

    def discover_fixture(self):
        deadline=time.monotonic()+20
        while True:
            try:return mapping.Deployment.discover(self.directory)
            except mapping.DeploymentError as error:
                if error.code!='docker_inventory_changing' or time.monotonic()>=deadline:raise
                self.audit(dict(phase='retry_readonly_fixture_observation',code=error.code))
                time.sleep(.2)

    def build_old(self, engine, *, legacy_init=False, storage=''):
        self.create_ca()
        source, revision, tree = components.PreparationTests.legacy_source(self, self.docker)
        old_images = {}
        for name, dockerfile in (('web','Dockerfile'), ('nginx','nginx/Dockerfile')):
            tag = self.project+'-old-'+name
            prepare._command(self.docker, ['docker','build','--label','org.opencontainers.image.revision='+revision,
                '--tag',tag,'--file',str(source/dockerfile),str(source)], self.evidence, 'old-'+name+'-build')
            old_images[name] = tag
            self.trust(tag,name,self.evidence,'old-'+name)
        port=8443 if storage=='relay' else 443
        self.endpoint='https://'+self.hostname+(':'+str(port) if port!=443 else '')
        environment='DB_TYPE='+engine+'\nTLS_ENABLED=true\nSERVER_NAME='+self.hostname+'\nDATA_DIRECTORY='+str(self.data)+'\n'
        if storage:environment+='STORAGE_TRANSPORT='+storage+'\nSTORAGE_PORT='+str(port)+'\nSTORAGE_ENDPOINT='+self.endpoint+'\n'
        (self.directory/'.env').write_text(environment)
        filename='docker-compose.storage.yaml' if storage else 'docker-compose.yaml'
        files = [source/filename]
        if engine == 'mysql':
            files.append(source/'docker-compose.mysql.yaml')
        profiles = ['mysql'] if engine == 'mysql' else []
        rendered = mapping.ComposeDocker(self.directory, files, self.project, [], profiles).config()
        for name, service in rendered['services'].items():
            service.pop('build', None)
            service.pop('profiles', None)
            service['container_name'] = self.project+'-'+name
            service.pop('ports', None)
            if storage:service.pop('network_mode',None)
            if name in prepare.APP_SERVICES:
                service['image'] = old_images['web']
            elif name == 'nginx':
                service['image'] = old_images['nginx']
                service.setdefault('networks', {})['default'] = {'aliases':[self.hostname]}
            if storage and name=='web':
                service.setdefault('networks',{})['default']={'aliases':[self.hostname]}
            service['cpus'] = 1 if name in {'web','mysql'} else .25
            service['mem_limit'] = '768m' if name == 'mysql' else '512m' if name == 'web' else '128m'
        isolated_network = rendered.setdefault('networks',{}).setdefault('default',{})
        isolated_network['internal'] = True
        isolated_network['ipam'] = {'config':[{'subnet':self.fixture.isolated_subnet()}]}
        base = self.directory/filename
        backup.private_write(base, backup.json_bytes(backup.compose_values(rendered, encode=True)))
        active_files = [base]
        if engine == 'mysql':
            # Retain the documented overlay as a source input, so later
            # candidate rendering uses its own corresponding overlay as well.
            overlay = self.directory/'docker-compose.mysql.yaml'
            shutil.copyfile(source/'docker-compose.mysql.yaml', overlay)
            active_files.append(overlay)
        # The existing test ACME factory changes only lookup/directory I/O.
        # Its CA key stays in the controller's private volume, never Web/Nginx.
        program = (ROOT/'tests/storage_certificate_agent_fixture.py').read_text().replace('/test-ca/',
            '/var/lib/frontiercloud-certificate/operator-test-ca/')
        fixture = self.directory/'fixture.yaml'
        backup.private_write(fixture,backup.json_bytes({'services':{'certificate-agent':{
            'command':['python','-c',program]}}}))
        active_files.append(fixture)
        self.active = mapping.ComposeDocker(self.directory, active_files, self.project, [], profiles)
        self.addCleanup(self.down)
        prepare._command(self.active, self.active.base+['up','-d','--no-build','--pull','missing','--wait','--wait-timeout','180'],
            self.evidence,'old-application-start',timeout=240)
        deployment = self.discover_fixture()
        public_service='web' if storage else 'nginx'
        identifier = deployment.observed[public_service]['container_id']
        container = self.docker.run(['docker','inspect',identifier], json_output=True)[0]
        # Docker's default pool can move outside the product's usual 172/12
        # after many isolated fixtures. Trust only this project's actual bridge.
        networks = list(container['NetworkSettings']['Networks'])
        self.assertEqual(len(networks),1)
        network = self.docker.run(['docker','network','inspect',networks[0]],json_output=True)[0]
        subnets = [row['Subnet'] for row in network['IPAM']['Config'] if ipaddress.ip_network(row['Subnet']).version == 4]
        self.assertEqual(len(subnets),1)
        trusted_proxy='' if storage else subnets[0]
        rendered['services']['web']['environment']['TRUSTED_PROXY_NETWORKS'] = trusted_proxy
        (self.directory/'.env').write_text((self.directory/'.env').read_text()+'TRUSTED_PROXY_NETWORKS='+trusted_proxy+'\n')
        if legacy_init:
            # Simulate an established old layout whose one-time init service
            # was removed after initial deployment. Never delete its data.
            init_id = deployment.observed['media-init']['container_id']
            init = self.docker.run(['docker','inspect',init_id],json_output=True)[0]
            self.assertFalse(init['State']['Running'])
            self.assertEqual(init['State']['ExitCode'],0)
            rendered['services'].pop('media-init')
            for definition in rendered['services'].values():
                definition.get('depends_on',{}).pop('media-init',None)
            prepare._command(self.active,['docker','rm',init_id],self.evidence,'old-legacy-init-retired')
        base.write_bytes(backup.json_bytes(backup.compose_values(rendered,encode=True)))
        prepare._command(self.active,self.active.base+['up','-d','--force-recreate','--no-build','--pull','never',
            '--wait','--wait-timeout','180'],self.evidence,'old-proxy-network-configured',timeout=240)
        deployment = self.discover_fixture()
        container = self.docker.run(['docker','inspect',deployment.observed[public_service]['container_id']],json_output=True)[0]
        self.addresses = [row['IPAddress'] for row in container['NetworkSettings']['Networks'].values() if row.get('IPAddress')]
        self.assertEqual(len(self.addresses),1)
        original = socket.getaddrinfo
        def resolver(host, port, *arguments, **options):
            return original(self.addresses[0] if host == self.hostname else host,port,*arguments,**options)
        socket.getaddrinfo = resolver
        self.addCleanup(setattr,socket,'getaddrinfo',original)
        self.client = Client(self.endpoint, self.audit,self.ca/'root.crt')
        self.assertEqual(self.client.request('GET','/health/ready')[0],200)
        return deployment, {'revision': revision, 'tree': tree, 'old_images': deployment.plan.images,
            'canonical_dockerfiles': True, 'old_asgi_started': True,'legacy_missing_init':legacy_init}

    def down(self):
        docker = self.prepared.docker if self.prepared else self.active
        self.fixture.run_command(docker.base+['down','--volumes','--timeout','10'])

    def seed(self, deployment, *, filename='legacy.mp3', pool=False):
        key = (deployment.plan.resources['secrets'].root/'admin_key').read_text().strip()
        body = urllib.parse.urlencode({'token':key}).encode()
        status,_,reply = self.client.request('POST','/api/v1/media/admin/elevate',body,
            {'Content-Type':'application/x-www-form-urlencoded'})
        self.assertEqual(status,200,reply[:200])
        blob = (ROOT/'tests/fixtures/tone.mp3').read_bytes()
        if pool:
            body=json.dumps(dict(site_type='primary',target_dir='music/OperatorLegacy',filename=filename,
                size_bytes=len(blob),storage_mode='plain')).encode()
            status,_,response=self.client.request('POST','/api/v1/media/admin/upload/session',body,{'Content-Type':'application/json'})
            self.assertEqual(status,200,response[:200]);reservation=json.loads(response)
            self.assertEqual(reservation['transport'],'Local')
            status,_,response=self.client.request('PUT',reservation['upload_url'],blob,{'Content-Type':'application/octet-stream'})
        else:
            boundary = 'fc-operator-'+uuid.uuid4().hex
            parts = []
            for name, value in (('target_dir','music'),('relative_path','OperatorLegacy/'+filename),('site_type','primary'),('storage_mode','plain')):
                parts.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+name+'"\r\n\r\n'+value+'\r\n').encode())
            parts.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="file"; filename="'+filename+'"\r\nContent-Type: audio/mpeg\r\n\r\n').encode()+blob+b'\r\n')
            body=b''.join(parts)+('--'+boundary+'--\r\n').encode()
            status,_,response = self.client.request('POST','/api/v1/media/admin/upload/item',body,
                {'Content-Type':'multipart/form-data; boundary='+boundary})
        self.assertEqual(status,200,response[:200])
        media_path = json.loads(response)['path']
        self.assertEqual(media_path,'music/OperatorLegacy/'+filename)
        status,headers,content = self.client.request('GET','/api/v1/media/admin/download?'+urllib.parse.urlencode({'paths':json.dumps([media_path])}),
            headers={'Range':'bytes=3-9'})
        self.assertEqual((status,content),(206,blob[3:10]))
        self.assertEqual(headers.get('Content-Range'),f'bytes 3-9/{len(blob)}')
        self.assertEqual(backup.digest(deployment.plan.resources['control'].root/'media'/media_path)[1],hashlib.sha256(blob).hexdigest())
        return {'path':media_path,'sha256':hashlib.sha256(blob).hexdigest(),'bytes':len(blob),'old_range_status':status}

    def candidate(self, *, folder='candidate', revision='f'*40):
        stage = self.evidence/folder
        source = stage/'source'
        stage.mkdir(mode=0o700)
        source.mkdir(mode=0o755)
        stage.chmod(0o700)
        source.chmod(0o755)
        names = subprocess.check_output(['git','-C',str(ROOT),'ls-files','-z'],text=True).split('\0')
        for name in filter(None,names):
            original = ROOT/name
            self.assertTrue(original.is_file(),name)
            self.assertFalse(original.is_symlink(),name)
            destination = source/name
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(original,destination)
        directories = []
        for path in source.rglob('*'):
            if path.is_dir():
                path.chmod(0o755)
                directories.append(path.relative_to(source).as_posix())
        inventory = {path.relative_to(source).as_posix():{'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'bytes':path.stat().st_size,'mode':path.stat().st_mode & 0o777}
            for path in source.rglob('*') if path.is_file()}
        archive = stage/'source.tar.gz'
        with tarfile.open(archive,'w:gz') as stream:
            stream.add(source,arcname='source')
        size,digest = backup.digest(archive)
        receipt = {'version':1,'phase':'source_staged','provenance':{'revision':revision,'public_provenance_verified':False},
            'files':inventory,'directories':sorted(directories),'archive':{'bytes':size,'sha256':digest},'deployment_changed':False}
        receipt['stage_sha256'] = mapping.checksum(receipt)
        backup.private_write(stage/'source-receipt.json',backup.json_bytes(receipt))
        self.assertEqual(source_delivery.verify_stage(stage),receipt)
        return stage,source_delivery.verify_stage

    def interrupted_helper(self, operation):
        attempt = operation.prepared.directory/('helper-fault-'+uuid.uuid4().hex)
        attempt.mkdir(mode=0o700)
        observed = []
        def timeout(docker, arguments, output, step, **_options):
            # Fault injection changes only this test invocation, never an image
            # or product entry point. The Compose client really times out.
            cut = arguments.index('web',arguments.index('--entrypoint'))+1
            arguments = arguments[:cut]+['-c','import time; print("FAULT_HELPER_STARTED",flush=True); time.sleep(60)']
            backup.private_write(output/'fault.json',backup.json_bytes({'fixture_only':True,
                'fault':'kill_host_compose_client_after_timeout','signal':'SIGKILL','timeout_seconds':5}))
            killpg = os.killpg
            try:
                # Simulate abrupt host-client death, preventing its graceful
                # signal handler from cleaning the daemon-side container.
                with patch.object(prepare.os,'killpg',side_effect=lambda pid,_signal:killpg(pid,signal.SIGKILL)):
                    prepare._command(docker,arguments,output,step,timeout=5)
            except mapping.DeploymentError:
                owner = backup.bounded_json(output/'helper-owner.json')
                value = docker.run(['docker','inspect',owner['name']],json_output=True)[0]
                self.assertTrue(value['State']['Running'])
                observed.append(value['Id'])
                raise
        with self.assertRaises(mapping.DeploymentError) as caught:
            operation._database_helper('preflight',attempt,execute=timeout)
        self.assertEqual(caught.exception.code,'database_preflight_timeout')
        self.assertEqual(len(observed),1)
        self.assertIn('FAULT_HELPER_STARTED',(attempt/'database-preflight.log').read_text())
        remaining = self.docker.run(['docker','ps','-aq','--filter','id='+observed[0]])
        self.assertEqual(remaining,'')
        self.assertFalse((self.data/upgrade.HARD).exists())
        return {'timed_out':True,'daemon_helper_running_after_client_timeout':True,
            'owned_helper_joined':True,'container_id':observed[0],'old_business_fenced':False}

    def disappearing_inventory(self, deployment):
        docker = deployment.docker
        docker.audit_file = self.evidence/'inventory-inputs.jsonl'
        image = os.environ.get('FC_OPERATOR_DISCOVERY_IMAGE','frontiercloud-python-archive:db-test-tools')
        image_id = docker.run(['docker','image','inspect',image],json_output=True)[0]['Id']
        owners,identifiers = [],[]
        for role in ('disappearing','sentinel'):
            token = uuid.uuid4().hex
            owner = {'name':'fc-op-helper-'+token,'token':token,'image':image_id,'project':'fc-op-inventory-'+token[:12]}
            self.addCleanup(upgrade._join_helper,docker,owner)
            prepare._command(docker,['docker','run','-d','--rm','--name',owner['name'],
                '--label',upgrade.HELPER_LABEL+'='+token,'--label',mapping.LABEL+'project='+owner['project'],
                '--label',mapping.LABEL+'oneoff=True','--network','none','--cpus','.25','--memory','64m','--read-only',
                image_id,'python','-c','import time; time.sleep(150)'],self.evidence,'inventory-'+role)
            owners.append(owner)
            identifiers.append(docker.run(['docker','inspect',owner['name']],json_output=True)[0]['Id'])
        original = docker.run
        injected = False
        failed_inspect = False
        def lost_container(arguments,**options):
            nonlocal injected,failed_inspect
            try:
                result = original(arguments,**options)
            except mapping.DeploymentError:
                if arguments[:2] == ['docker','inspect'] and identifiers[0] in arguments:
                    failed_inspect = True
                raise
            if arguments == ['docker','ps','-aq','--no-trunc'] and not injected:
                self.assertIn(identifiers[0],result.splitlines())
                injected = True
                upgrade._join_helper(docker,owners[0])
            return result
        with patch.object(docker,'run',side_effect=lost_container):
            rows = docker.containers()
        found = {row['Id'] for row in rows}
        self.assertTrue(injected)
        self.assertTrue(failed_inspect)
        self.assertNotIn(identifiers[0],found)
        self.assertIn(identifiers[1],found)
        self.assertIn(deployment.observed['web']['container_id'],found)
        for owner in owners:upgrade._join_helper(docker,owner)
        return {'actual_disappearance_injected':True,'live_foreign_sentinel_preserved':True,
            'old_web_preserved':True,'partial_inspect_not_accepted':True}

    def process(self, step, *, until='opened', flags=(), expected_exit=0):
        driver = copy.copy(self.docker)
        driver.environment = {**driver.environment,'PYTHONDONTWRITEBYTECODE':'1',
            'FC_OPERATOR_APPLICATION_RUNTIME':'1','FC_OPERATOR_APPLICATION_EVIDENCE':str(self.evidence.parent)}
        arguments = ['python3','-B',str(ROOT/'tests/operator_resume_fixture.py'),
            '--directory',str(self.directory),'--until',until,*flags]
        try:
            prepare._command(driver,arguments,self.evidence,step,timeout=900)
        except mapping.DeploymentError:
            if not expected_exit:
                raise
        result = backup.bounded_json(self.evidence/(step+'-result.json'))
        self.assertEqual(result['exit_code'],expected_exit)
        self.assertFalse(result['timed_out'])
        return json.loads((self.evidence/(step+'.log')).read_text().strip().splitlines()[-1])

    def exercise_upgrade(self, engine, *, legacy_init=False, restore=False, role='Standalone'):
        old, proof = self.build_old(engine,legacy_init=legacy_init)
        sample = self.seed(old)
        if role=='Master':
            status,_headers,body=self.client.request('POST','/api/v1/media/admin/nodes/promote',
                json.dumps({'role':'Master','endpoint':self.endpoint,'local_capacity_gib':1}).encode(),{'Content-Type':'application/json'})
            self.assertEqual(status,200,body[:200])
            old=mapping.Deployment.discover(self.directory)
        self.assertIn(role,{'Standalone','Master'})
        status,_headers,body=self.client.request('GET','/internal/v1/identity?challenge='+'a'*32)
        self.assertEqual(status,200,body[:200]);before_identity=json.loads(body)['payload']
        self.assertEqual(before_identity['role'],role)
        inventory_fault = self.disappearing_inventory(old)
        stage,verify = self.candidate()
        prepared = prepare.prepare(old,stage,self.evidence/'prepared',verify=verify,acceptance=True,execute=self.candidate_build)
        self.prepared = prepared
        if legacy_init:
            self.assertIn('media-init',prepared.receipt['added_services'])
        operation = upgrade.Upgrade.begin(old,prepared,verify=verify,acceptance=True)
        self.addCleanup(operation._reconcile_helpers)
        self.public_source_refusal()
        helper_fault = self.interrupted_helper(operation)
        host_death = self.process('independent-host-death',flags=['--leave-helper'],expected_exit=31)
        self.assertTrue(host_death['daemon_helper_running'])
        self.assertTrue(self.docker.run(['docker','inspect',host_death['container_id']],json_output=True)[0]['State']['Running'])
        self.process('independent-database-prepare',until='database_prepared')
        self.assertEqual(self.docker.run(['docker','ps','-aq','--filter','name=^/'+host_death['helper_name']+'$']),'')
        operation = upgrade.Upgrade.load(self.directory,acceptance=True)
        result = operation.state['database_result']
        self.assertEqual((result['from_generation'],result['generation']),(4,9))
        self.assertEqual(backup.digest(self.data/'media'/sample['path'])[1],sample['sha256'])
        self.assertTrue((self.data/upgrade.HARD).exists())
        self.process('independent-activation',until='active_fenced')
        operation = upgrade.Upgrade.load(self.directory,acceptance=True)
        activated = operation.state['activation']
        self.assertEqual(activated['phase'],'active_fenced')
        observed = mapping.Deployment.discover(self.directory)
        self.assertEqual(observed.operator_phase,'active_fenced')
        self.assertEqual(observed.source_files,old.docker.files)
        prepare._command(observed.docker,['python3',str(ROOT/'scripts/frontiercloud_operator.py'),
            'inspect','--project-directory',str(self.directory)],self.evidence,'host-rediscovery',timeout=60)
        host_report = json.loads((self.evidence/'host-rediscovery.log').read_text())
        self.assertEqual(host_report['operator_phase'],'active_fenced')
        container = self.docker.run(['docker','inspect',observed.observed['nginx']['container_id']],json_output=True)[0]
        self.addresses[:] = [row['IPAddress'] for row in container['NetworkSettings']['Networks'].values() if row.get('IPAddress')]
        self.assertEqual(len(self.addresses),1)
        self.assertEqual(self.client.request('GET','/health/ready')[0],200)
        self.assertEqual(self.client.request('GET','/api/v1/media/')[0],503)
        identity_path = '/internal/v1/identity?challenge='+'e'*32
        identity_status,_headers,identity_body = self.client.request('GET',identity_path)
        self.assertEqual(identity_status,200,identity_body[:200])
        self.assertEqual(json.loads(identity_body)['payload']['challenge'],'e'*32)
        for method,path in [('POST',identity_path),('HEAD',identity_path),
                ('GET',identity_path+'&action=write'),('GET','/internal/v1/identity'),
                ('GET','/internal/v1/identity?challenge='+'E'*32)]:
            self.assertEqual(self.client.request(method,path)[0],503,(method,path))
        self.assertEqual(backup.digest(self.data/'media'/sample['path'])[1],sample['sha256'])
        self.assertTrue((self.data/upgrade.HARD).exists())
        repeated = upgrade.Upgrade.resume(old,prepared).activate_fenced(verify=verify)
        self.assertEqual(repeated,activated)
        if restore:
            return self.exercise_recovery(old,prepared,proof,sample,before_identity)
        self.process('independent-opening-death',flags=['--lose-opening-reply'],expected_exit=32)
        operation = upgrade.Upgrade.load(self.directory,acceptance=True)
        self.assertEqual(operation.state['phase'],'opening')
        self.assertTrue(operation.state['business_may_have_written'])
        self.assertFalse((self.data/upgrade.HARD).exists())
        current = mapping.Deployment.discover(self.directory)
        container = self.docker.run(['docker','inspect',current.observed['nginx']['container_id']],json_output=True)[0]
        self.addresses[:] = [row['IPAddress'] for row in container['NetworkSettings']['Networks'].values() if row.get('IPAddress')]
        self.assertEqual(self.client.request('GET','/api/v1/media/')[0],200)
        # Reuse the pre-upgrade authenticated browser cookie before elevating
        # again; the original Admin secret/session boundary must still work.
        status,_headers,content = self.client.request('GET','/api/v1/media/admin/download?'+urllib.parse.urlencode({'paths':json.dumps([sample['path']])}),
            headers={'Range':'bytes=3-9'})
        self.assertEqual((status,content),(206,(ROOT/'tests/fixtures/tone.mp3').read_bytes()[3:10]))
        post_open = self.seed(current,filename='new-after-open.mp3',pool=role=='Master')
        completed = self.process('independent-opening-resume')
        opened = completed['result']
        self.assertEqual(opened['phase'],'opened')
        self.assertTrue(opened['upgrade_complete'])
        self.assertEqual(backup.digest(self.data/'media'/post_open['path'])[1],post_open['sha256'])
        self.assertEqual(upgrade.Upgrade.resume(old,prepared).open(verify=verify),opened)
        self.assertEqual(mapping.Deployment.discover(self.directory).operator_phase,'ready')
        identity_status,_headers,body=self.client.request('GET','/internal/v1/identity?challenge='+'b'*32)
        self.assertEqual(identity_status,200,body[:200]);after_identity=json.loads(body)['payload']
        self.assertEqual({k:before_identity[k] for k in ('node_id','public_key','role','endpoint')},
            {k:after_identity[k] for k in ('node_id','public_key','role','endpoint')})
        cpu = self.fixture.run_command(['python3','scripts/check_cpu_quiescence.py',
            current.observed['web']['container_id'],'--timeout','30'])
        self.assertIn('for 5 samples',cpu)
        backup.private_write(self.evidence/'result.json',backup.json_bytes({'passed':True,'old':proof,'sample':sample,
            'database':result,'activation':activated,'canonical_candidate_images_built':True,'full_deployment_activated':True,
            'helper_timeout':helper_fault,
            'inventory_fault':inventory_fault,
            'independent_host_death':host_death,'fresh_process_recovery':True,
            'opening':opened,'post_open_sample':post_open,'lost_open_reply_resumed':True,'preupgrade_session_range_status':status,
            'test_ca_wrappers':True,'clean_canonical_images':self.clean_images,
            'duplicate_activation_recreated_services':False,'cpu':cpu,'business_opened':True,'upgrade_complete':True,
            'public_release_proved':False,'fixed_role':role,'identity_preserved':True}))

    def test_actual_old_sqlite_application_to_prepared_current_generation(self):
        self.exercise_upgrade('sqlite')

    def test_actual_old_mysql_application_to_prepared_current_generation(self):
        self.exercise_upgrade('mysql',legacy_init=True)

    def test_actual_old_sqlite_master_preserves_fixed_role_identity_and_resources(self):
        self.exercise_upgrade('sqlite',role='Master')

    def test_actual_old_mysql_master_preserves_fixed_role_identity_and_resources(self):
        self.exercise_upgrade('mysql',legacy_init=True,role='Master')

    def exercise_storage(self,profile,*,paired=False,restore=False):
        old,proof=self.build_old('sqlite',storage=profile)
        pair=None
        if paired:
            from tests.operator_paired_storage_native import Pair
            pair=Pair(self,old,profile)
            old=pair.deployment
        self.assertEqual(old.role,'storage')
        path='/internal/v1/identity?challenge='+'c'*32
        status,_headers,body=self.client.request('GET',path)
        self.assertEqual(status,200,body[:200]);before=json.loads(body)['payload']
        self.assertEqual(before['role'],'Follower');self.assertEqual(before['endpoint'],self.endpoint)
        def certificate():
            port=urllib.parse.urlsplit(self.endpoint).port or 443
            with socket.create_connection((self.addresses[0],port),timeout=5) as connection:
                with ssl.create_default_context(cafile=str(self.ca/'root.crt')).wrap_socket(connection,server_hostname=self.hostname) as stream:
                    return hashlib.sha256(stream.getpeercert(binary_form=True)).hexdigest()
        original_certificate=certificate()
        stage,verify=self.candidate()
        prepared=prepare.prepare(old,stage,self.evidence/'prepared',verify=verify,acceptance=True,execute=self.candidate_build)
        self.prepared=prepared
        operation=upgrade.Upgrade.begin(old,prepared,verify=verify,acceptance=True)
        self.addCleanup(operation._reconcile_helpers)
        self.public_source_refusal()
        death=self.process('independent-storage-host-death',flags=['--leave-helper'],expected_exit=31)
        self.assertTrue(death['daemon_helper_running'])
        self.process('independent-storage-database-prepare',until='database_prepared')
        operation=upgrade.Upgrade.load(self.directory,acceptance=True)
        result=operation.state['database_result'];self.assertEqual((result['from_generation'],result['generation']),(4,9))
        self.process('independent-storage-activation',until='active_fenced')
        current=mapping.Deployment.discover(self.directory)
        container=self.docker.run(['docker','inspect',current.observed['web']['container_id']],json_output=True)[0]
        self.addresses[:]=[row['IPAddress'] for row in container['NetworkSettings']['Networks'].values() if row.get('IPAddress')]
        self.assertTrue(self.addresses)
        self.assertEqual(self.client.request('GET','/health/ready')[0],200)
        self.assertTrue((self.data/upgrade.HARD).exists())
        recovery_result=None
        recovered_preflight=None
        if restore:
            self.process('independent-storage-recovery-death',flags=['--restore-fenced','--lose-payload-move'],expected_exit=33)
            recovery_result=self.process('independent-storage-recovery-resume',flags=['--restore-fenced'])['result']
            operation=upgrade.Upgrade.load(self.directory,acceptance=True,check_images=False)
            recovered_plan=recovery.Recovery(operation)._plan()
            def cleanup_recovered_volumes():
                state=backup.bounded_json(prepared.directory/'upgrade-state.json')['recovery']
                for attempt in state.get('retained_attempts',[])+[state['attempt']]:
                    names=self.docker.run(['docker','volume','ls','-q','--filter','label='+recovery.LABEL+'='+attempt['token'],
                        '--filter','label='+mapping.LABEL+'project='+self.project]).splitlines()
                    for name in names:
                        view=self.docker.volume(name)
                        self.assertEqual(view['Labels'][recovery.LABEL],attempt['token'])
                        self.assertEqual(view['Labels'][mapping.LABEL+'project'],self.project)
                        self.docker.run(['docker','volume','rm',name])
            self.addCleanup(cleanup_recovered_volumes)
            self.addCleanup(self.fixture.run_command,recovered_plan.docker.base+['down','--volumes','--timeout','10'])
            probe=copy.copy(operation);probe.deployment=copy.copy(old)
            probe.deployment.plan,probe.deployment.docker=recovered_plan,recovered_plan.docker
            probe.root=recovered_plan.resources['control'].root
            attempt=prepared.directory/'storage-restored-preflight';attempt.mkdir(mode=0o700)
            recovered_preflight=probe._database_helper('preflight',attempt)
            self.assertEqual(recovered_preflight,operation.state['preflight'])
            self.assertEqual(recovered_preflight['generation'],4)
            self.process('independent-storage-recovery-opening-death',flags=['--restore-open','--lose-recovery-reply'],expected_exit=34)
            resumed=self.process('independent-storage-recovery-opening-resume',flags=['--restore-open'])
            self.assertTrue(resumed['result']['recovery_complete'])
        else:
            self.process('independent-storage-opening-death',flags=['--lose-opening-reply'],expected_exit=32)
            self.assertEqual(upgrade.Upgrade.load(self.directory,acceptance=True).state['phase'],'opening')
            resumed=self.process('independent-storage-opening-resume')
            self.assertEqual(resumed['result']['phase'],'opened');self.assertTrue(resumed['result']['upgrade_complete'])
        current=mapping.Deployment.discover(self.directory)
        container=self.docker.run(['docker','inspect',current.observed['web']['container_id']],json_output=True)[0]
        self.addresses[:]=[row['IPAddress'] for row in container['NetworkSettings']['Networks'].values() if row.get('IPAddress')]
        self.assertEqual(current.operator_phase,'ready');self.assertEqual(current.role,'storage')
        status,_headers,body=self.client.request('GET',path);self.assertEqual(status,200,body[:200])
        after=json.loads(body)['payload']
        self.assertEqual({k:before[k] for k in ('node_id','public_key','role','endpoint')},
            {k:after[k] for k in ('node_id','public_key','role','endpoint')})
        self.assertEqual(certificate(),original_certificate)
        paired_result=pair.verify(current) if pair else None
        cpu=self.fixture.run_command(['python3',str(ROOT/'scripts/check_cpu_quiescence.py'),current.observed['web']['container_id'],'--timeout','30'])
        self.assertIn('for 5 samples',cpu)
        backup.private_write(self.evidence/'result.json',backup.json_bytes(dict(passed=True,old=proof,database=result,
            fixed_role='Follower',storage_profile=profile,identity_preserved=True,certificate_sha256=original_certificate,
            actual_paired_cluster=paired,media_payload_acceptance=paired,paired=paired_result,recovery=recovery_result,
            restored_preflight=recovered_preflight,
            controlled_state_restored=restore,independent_process_recovery=True,
            lost_open_reply_resumed=True,upgrade_complete=not restore,public_release_proved=False,cpu=cpu)))

    def test_actual_old_direct_storage_preserves_fixed_follower_identity(self):
        self.exercise_storage('direct')

    def test_actual_old_relay_storage_preserves_fixed_follower_identity(self):
        self.exercise_storage('relay')

    def test_actual_old_paired_direct_upgrade_preserves_relationship_and_media(self):
        self.exercise_storage('direct',paired=True)

    def test_actual_old_paired_relay_upgrade_preserves_relationship_and_media(self):
        self.exercise_storage('relay',paired=True)

    def test_actual_old_paired_direct_restore_preserves_relationship_and_media(self):
        self.exercise_storage('direct',paired=True,restore=True)

    def test_actual_old_paired_relay_restore_preserves_relationship_and_media(self):
        self.exercise_storage('relay',paired=True,restore=True)

    def staged_entry(self,stage,step, *, resume=False,recover=False,lose_reply=False):
        folder=self.evidence/step;folder.mkdir(mode=0o700)
        reference=registry.operation(self.directory)
        checked=source_delivery.verify_stage(stage)
        intent={'version':1,'phase':'host_handoff','project':self.project,'project_directory':str(self.directory),
            'directory_identity':registry.root_identity(self.directory),'stage_directory':str(stage),
            'source_stage_sha256':checked['stage_sha256'],'command':'recover' if recover else 'resume' if resume else 'upgrade',
            'intent':'recover' if recover else 'resume' if resume else 'new_upgrade',
            'previous_operation_sha256':backup.digest(self.directory/registry.OPERATION)[1] if reference else None,
            'old_deployment':None if resume or recover else mapping.Deployment.discover(self.directory).summary(),
            'lock_path':str(folder/'entry.lock')}
        prepare._store(folder/'intent.json',intent)
        driver=copy.copy(self.docker)
        driver.environment={**driver.environment,'PYTHONDONTWRITEBYTECODE':'1','FC_OPERATOR_APPLICATION_RUNTIME':'1',
            'FC_OPERATOR_APPLICATION_EVIDENCE':os.environ['FC_OPERATOR_APPLICATION_EVIDENCE']}
        arguments=['python3','-I','-B',str(stage/'source/tests/operator_entry_fixture.py'),'--intent',str(folder/'intent.json')]
        if lose_reply:arguments.append('--lose-opening-reply')
        try:prepare._command(driver,arguments,self.evidence,step,timeout=1400)
        except mapping.DeploymentError:
            if not lose_reply:raise
        status=backup.bounded_json(self.evidence/(step+'-result.json'))
        self.assertEqual(status['exit_code'],(34 if recover else 32) if lose_reply else 0)
        self.assertFalse(status['timed_out'])
        return json.loads((self.evidence/(step+'.log')).read_text().strip().splitlines()[-1])

    def handoff_prepared(self,path):
        if (path/'prepared.json').is_file():
            self.prepared=prepare.Prepared.load(path,acceptance=True,check_images=False)
            operation=upgrade.Upgrade.load(self.directory,acceptance=True,check_images=False)
            operation._reconcile_helpers()

    def old_entry_selection(self,stage, *, next_stage=None):
        driver=copy.copy(self.docker)
        driver.environment={**driver.environment,'PYTHONDONTWRITEBYTECODE':'1','FC_OPERATOR_APPLICATION_RUNTIME':'1',
            'FC_OPERATOR_APPLICATION_EVIDENCE':os.environ['FC_OPERATOR_APPLICATION_EVIDENCE']}
        before=mapping.Deployment.discover(self.directory).summary()
        arguments=['python3','-I','-B',str(ROOT/'tests/operator_selection_fixture.py'),'--phase','old',
            '--directory',str(self.directory),'--stage',str(stage)]
        step='old-entry-retained-source-selection'
        if next_stage:
            arguments+=['--next-stage',str(next_stage)]
            step='old-entry-second-upgrade'
        prepare._command(driver,arguments,self.evidence,step,timeout=900 if next_stage else 120)
        result=json.loads((self.evidence/(step+'.log')).read_text())
        if next_stage:
            self.assertTrue(result['result']['upgrade_complete'])
            self.assertEqual(result['result']['revision'],'e'*40)
            current=mapping.Deployment.discover(self.directory)
            self.assertEqual((current.role,current.plan.engine,current.operator_phase),(before['role'],before['database'],'ready'))
            self.prepared=upgrade.Upgrade.load(self.directory,acceptance=True).prepared
        else:
            self.assertEqual(result['result']['state'],'already_current')
            self.assertFalse(result['result']['deployment_changed'])
            self.assertEqual(result['executed_source'],str(stage/'source'))
            self.assertEqual(mapping.Deployment.discover(self.directory).summary(),before)
        return result

    def exercise_staged_entry(self,engine):
        old,proof=self.build_old(engine,legacy_init=engine=='mysql')
        sample=self.seed(old)
        stage,verify=self.candidate()
        prepared_path=self.evidence/'staged-new-upgrade/prepared'
        self.addCleanup(self.handoff_prepared,prepared_path)
        death=self.staged_entry(stage,'staged-new-upgrade',lose_reply=True)
        self.assertTrue(death['business_may_have_written'])
        self.prepared=prepare.Prepared.load(prepared_path,acceptance=True)
        operation=upgrade.Upgrade.load(self.directory,acceptance=True)
        self.assertEqual(operation.state['phase'],'opening')
        self.assertTrue(operation.state['business_may_have_written'])
        self.assertFalse((self.data/upgrade.HARD).exists())
        # Public bootstrap must not promote local source, even while the local
        # fixture has already proved its business application can open.
        cache=self.evidence/'host-public-refusal'
        with self.assertRaises(mapping.DeploymentError) as refused:
            host_entry.start(self.directory,host_root=cache)
        self.assertEqual(refused.exception.code,'source_not_published')
        current=mapping.Deployment.discover(self.directory)
        container=self.docker.run(['docker','inspect',current.observed['nginx']['container_id']],json_output=True)[0]
        self.addresses[:]=[row['IPAddress'] for row in container['NetworkSettings']['Networks'].values() if row.get('IPAddress')]
        status,_headers,content=self.client.request('GET','/api/v1/media/admin/download?'+urllib.parse.urlencode({'paths':json.dumps([sample['path']])}),
            headers={'Range':'bytes=3-9'})
        self.assertEqual((status,content),(206,(ROOT/'tests/fixtures/tone.mp3').read_bytes()[3:10]))
        post_open=self.seed(current,filename='new-after-staged-entry.mp3')
        resumed=self.staged_entry(stage,'staged-entry-resume',resume=True)
        opened=resumed['result']
        self.assertEqual(opened['phase'],'opened')
        self.assertTrue(opened['upgrade_complete'])
        self.assertTrue(opened['certificate_verified'])
        self.assertTrue(opened['signed_identity_verified'])
        operation=upgrade.Upgrade.load(self.directory,acceptance=True)
        self.assertEqual((operation.state['database_result']['from_generation'],operation.state['database_result']['generation']),(4,9))
        if engine=='mysql':self.assertIn('media-init',self.prepared.receipt['added_services'])
        self.assertEqual(upgrade.Upgrade.advance(self.directory,acceptance=True),opened)
        self.assertEqual(backup.digest(self.data/'media'/sample['path'])[1],sample['sha256'])
        self.assertEqual(backup.digest(self.data/'media'/post_open['path'])[1],post_open['sha256'])
        current=mapping.Deployment.discover(self.directory)
        self.assertEqual(current.operator_phase,'ready')
        old_selection=self.old_entry_selection(stage) if engine=='mysql' else None
        second_upgrade=None
        if engine=='mysql':
            next_stage,_verify=self.candidate(folder='following-candidate',revision='e'*40)
            second_upgrade=self.old_entry_selection(stage,next_stage=next_stage)
            current=mapping.Deployment.discover(self.directory)
            container=self.docker.run(['docker','inspect',current.observed['nginx']['container_id']],json_output=True)[0]
            self.addresses[:]=[row['IPAddress'] for row in container['NetworkSettings']['Networks'].values() if row.get('IPAddress')]
            self.assertEqual(backup.digest(self.data/'media'/sample['path'])[1],sample['sha256'])
            self.assertEqual(backup.digest(self.data/'media'/post_open['path'])[1],post_open['sha256'])
            read_status,_headers,read_bytes=self.client.request('GET','/api/v1/media/admin/download?'+urllib.parse.urlencode({'paths':json.dumps([post_open['path']])}),
                headers={'Range':'bytes=3-9'})
            self.assertEqual((read_status,read_bytes),(206,(ROOT/'tests/fixtures/tone.mp3').read_bytes()[3:10]))
        cpu=self.fixture.run_command(['python3','scripts/check_cpu_quiescence.py',current.observed['web']['container_id'],'--timeout','30'])
        self.assertIn('for 5 samples',cpu)
        backup.private_write(self.evidence/'result.json',backup.json_bytes({'passed':True,'old':proof,'sample':sample,
            'post_open_sample':post_open,'opening':opened,'staged_entry_executed':True,'entry_source':resumed['executed_source'],
            'source_stage_sha256':resumed['source_stage_sha256'],'generation':operation.state['database_result'],
            'legacy_init_added':engine=='mysql','opening_reply_lost':True,'new_write_retained':True,'original_session_range_status':status,
            'old_entry_retained_source_selection':old_selection,
            'old_entry_second_upgrade':second_upgrade,
            'default_public_bootstrap_refused_local_source':True,'test_ca_wrappers':True,'cpu':cpu,'public_release_proved':False}))

    def test_staged_host_entry_upgrades_old_sqlite_application(self):
        self.exercise_staged_entry('sqlite')

    def test_staged_host_entry_upgrades_old_mysql_application_and_adds_init(self):
        self.exercise_staged_entry('mysql')

    def exercise_recovery(self, old, prepared, proof, sample, before_identity):
        payload = self.data/'media'/sample['path']
        inode = payload.stat().st_ino
        old_roots = {key:upgrade._identity(resource.root) for key,resource in old.plan.resources.items()}
        def cleanup_volumes():
            state = backup.bounded_json(prepared.directory/'upgrade-state.json').get('recovery',{})
            attempts = state.get('retained_attempts',[])+([state['attempt']] if state.get('attempt') else [])
            for attempt in attempts:
                names = self.docker.run(['docker','volume','ls','-q','--filter','label='+recovery.LABEL+'='+attempt['token'],
                    '--filter','label='+mapping.LABEL+'project='+self.project]).splitlines()
                for name in names:
                    value = self.docker.volume(name)
                    labels = value.get('Labels') or {}
                    self.assertEqual(labels.get(recovery.LABEL),attempt['token'])
                    self.assertEqual(labels.get(mapping.LABEL+'project'),self.project)
                    self.docker.run(['docker','volume','rm',name])
        self.addCleanup(cleanup_volumes)
        death = self.process('independent-recovery-move-death',flags=['--restore-fenced','--lose-payload-move'],expected_exit=33)
        self.assertTrue(death['payload_renamed'])
        resumed = self.process('independent-recovery-resume',flags=['--restore-fenced'])
        result = resumed['result']
        operation = upgrade.Upgrade.load(self.directory,acceptance=True,check_images=False)
        transaction = recovery.Recovery(operation)
        plan = transaction._plan()
        control = plan.resources['control'].root
        self.assertEqual((control/'media'/sample['path']).stat().st_ino,inode)
        self.assertEqual(backup.digest(control/'media'/sample['path'])[1],sample['sha256'])
        self.assertEqual({key:upgrade._identity(resource.root) for key,resource in old.plan.resources.items()},old_roots)
        self.assertFalse((self.data/'media').exists())
        self.assertTrue((self.data/upgrade.HARD).exists())
        self.assertTrue((control/upgrade.HARD).exists())
        self.assertEqual(transaction.prepare(),result)
        self.addCleanup(self.fixture.run_command,plan.docker.base+['down','--volumes','--timeout','10'])
        if plan.engine == 'mysql':
            prepare._command(plan.docker,plan.docker.base+['up','-d','--no-deps','--no-build','--pull','never',
                '--wait','--wait-timeout','90','mysql'],self.evidence,'restored-old-engine-start',timeout=120)
        probe = copy.copy(operation)
        probe.deployment = copy.copy(old)
        probe.deployment.plan,probe.deployment.docker = plan,plan.docker
        probe.root = control
        attempt = prepared.directory/('restored-preflight-'+uuid.uuid4().hex)
        attempt.mkdir(mode=0o700)
        checked = probe._database_helper('preflight',attempt)
        self.assertEqual(checked,operation.state['preflight'])
        self.assertEqual(checked['generation'],4)
        if plan.engine == 'mysql':
            prepare._command(plan.docker,plan.docker.base+['stop','mysql'],self.evidence,'restored-old-engine-stop',timeout=45)
        self.assertEqual(self.process('independent-recovery-duplicate',flags=['--restore-fenced'])['result'],result)
        lost = self.process('independent-recovery-opening-death',flags=['--restore-open','--lose-recovery-reply'],expected_exit=34)
        self.assertTrue(lost['recovered_business_may_have_written'])
        current = mapping.Deployment.discover(self.directory)
        self.assertEqual(current.operator_phase,'ready')
        self.assertEqual(current.source_files,old.docker.files)
        container = self.docker.run(['docker','inspect',current.observed['nginx']['container_id']],json_output=True)[0]
        self.addresses[:] = [row['IPAddress'] for row in container['NetworkSettings']['Networks'].values() if row.get('IPAddress')]
        self.assertEqual(self.client.request('GET','/api/v1/media/')[0],200)
        status,_headers,content = self.client.request('GET','/api/v1/media/admin/download?'+urllib.parse.urlencode({'paths':json.dumps([sample['path']])}),
            headers={'Range':'bytes=3-9'})
        self.assertEqual((status,content),(206,(ROOT/'tests/fixtures/tone.mp3').read_bytes()[3:10]))
        status_identity,_headers,identity_body=self.client.request('GET','/internal/v1/identity?challenge='+'f'*32)
        self.assertEqual(status_identity,200,identity_body[:200]);after_identity=json.loads(identity_body)['payload']
        self.assertEqual({k:before_identity[k] for k in ('node_id','public_key','role','endpoint')},
            {k:after_identity[k] for k in ('node_id','public_key','role','endpoint')})
        post_open = self.seed(current,filename='new-after-recovery.mp3',pool=before_identity['role']=='Master')
        completed = self.process('independent-recovery-opening-resume',flags=['--restore-open'])['result']
        self.assertEqual(completed['phase'],'restored_opened')
        self.assertTrue(completed['recovery_complete'])
        self.assertEqual(backup.digest(control/'media'/post_open['path'])[1],post_open['sha256'])
        self.assertEqual(self.process('independent-recovery-opening-duplicate',flags=['--restore-open'])['result'],completed)
        prepare._command(current.docker,['python3','-B',str(ROOT/'scripts/frontiercloud_operator.py'),
            'inspect','--project-directory',str(self.directory)],self.evidence,'host-restored-rediscovery',timeout=60)
        cpu = self.fixture.run_command(['python3','scripts/check_cpu_quiescence.py',current.observed['web']['container_id'],'--timeout','30'])
        self.assertIn('for 5 samples',cpu)
        stage = Path(prepared.receipt['stage_directory'])
        following = prepare.prepare(mapping.Deployment.discover(self.directory),stage,self.evidence/'following-prepared',
            acceptance=True,execute=self.candidate_build)
        next_operation = upgrade.Upgrade.begin(mapping.Deployment.discover(self.directory),following,acceptance=True)
        self.addCleanup(next_operation._reconcile_helpers)
        self.assertEqual(next_operation.state['previous_operation']['id'],operation.state['id'])
        self.assertEqual(mapping.Deployment.discover(self.directory).operator_phase,'ready')
        self.assertFalse((control/upgrade.HARD).exists())
        backup.private_write(self.evidence/'result.json',backup.json_bytes({'passed':True,'old':proof,'sample':sample,
            'controlled_state_restored':True,'failed_roots_retained':True,'payload_inode_retained':True,
            'recovery_host_death':death,'restored_preflight':checked,'recovery':result,
            'recovery_opening_death':lost,'recovery_opening':completed,'post_recovery_sample':post_open,
            'recovered_original_session_range_status':status,'next_upgrade_preparation_and_begin_accepted':True,'cpu':cpu,
            'business_opened':True,'recovery_complete':True,'public_release_proved':False,
            'fixed_role':before_identity['role'],'identity_preserved':True}))

    def test_controlled_sqlite_restore_preserves_failed_state_and_payload_inodes(self):
        self.exercise_upgrade('sqlite',restore=True)

    def test_controlled_mysql_restore_preserves_failed_state_and_payload_inodes(self):
        self.exercise_upgrade('mysql',legacy_init=True,restore=True)

    def test_controlled_sqlite_master_restore_preserves_fixed_identity_and_resources(self):
        self.exercise_upgrade('sqlite',restore=True,role='Master')

    def test_controlled_mysql_master_restore_preserves_fixed_identity_and_resources(self):
        self.exercise_upgrade('mysql',legacy_init=True,restore=True,role='Master')

    def exercise_early_abort(self, engine, *, staged=False):
        old,proof=self.build_old(engine,legacy_init=engine=='mysql')
        sample=self.seed(old)
        payload=self.data/'media'/sample['path']
        inode=payload.stat().st_ino
        roots={name:upgrade._identity(resource.root) for name,resource in old.plan.resources.items()}
        stage,verify=self.candidate()
        prepared=prepare.prepare(old,stage,self.evidence/'prepared',verify=verify,acceptance=True,execute=self.candidate_build)
        self.prepared=prepared
        first=upgrade.Upgrade.begin(old,prepared,verify=verify,acceptance=True)
        self.addCleanup(first._reconcile_helpers)
        self.public_source_refusal()
        cancelled=(self.staged_entry(stage,'staged-unfenced-cancel',recover=True) if staged
            else self.process('independent-unfenced-cancel',flags=['--abort-early']))['result']
        self.assertEqual(cancelled['phase'],'aborted')
        self.assertFalse(cancelled['services_restarted'])
        self.assertEqual(mapping.Deployment.discover(self.directory).summary(),old.summary())
        self.assertEqual(self.client.request('GET','/api/v1/media/')[0],200)
        self.assertFalse((self.data/upgrade.HARD).exists())
        following=prepare.prepare(mapping.Deployment.discover(self.directory),stage,self.evidence/'following-prepared',
            verify=verify,acceptance=True,execute=self.candidate_build)
        self.prepared=following
        operation=upgrade.Upgrade.begin(mapping.Deployment.discover(self.directory),following,verify=verify,acceptance=True)
        self.addCleanup(operation._reconcile_helpers)
        self.assertEqual(operation.state['previous_operation']['id'],first.state['id'])
        volumes=set(self.docker.run(['docker','volume','ls','-q','--filter','label='+mapping.LABEL+'project='+self.project]).splitlines())
        archive=backup.archive_tree
        injected=False
        def no_space(root,destination,**options):
            nonlocal injected
            result=archive(root,destination,**options)
            if root==self.data:
                injected=True
                self.assertNotIn('media',result)
                self.assertNotIn('recordings',result)
                backup.private_write(self.evidence/'fault.json',backup.json_bytes({'fixture_only':True,
                    'fault':'ENOSPC_after_actual_control_archive','errno':errno.ENOSPC,
                    'archive':str(destination),'sha256':backup.digest(destination)[1],'media_included':False}))
                raise OSError(errno.ENOSPC,'fixture ENOSPC after actual control archive')
            return result
        with patch.object(backup,'archive_tree',side_effect=no_space),self.assertRaisesRegex(OSError,'fixture ENOSPC'):
            operation.capture()
        self.assertTrue(injected)
        self.assertEqual(operation.state['phase'],'backing_up')
        partial=following.directory/operation.state['snapshot']
        self.assertFalse((partial/'manifest.json').exists())
        retained={path.name:backup.digest(path)[1] for path in partial.iterdir() if path.is_file()}
        self.assertTrue(retained)
        death=(self.staged_entry(stage,'staged-original-abort-opening-death',recover=True,lose_reply=True) if staged
            else self.process('independent-original-abort-opening-death',flags=['--abort-early','--lose-abort-reply'],expected_exit=34))
        self.assertTrue(death['original_resources_may_have_new_writes'])
        current=mapping.Deployment.discover(self.directory)
        self.assertEqual(current.operator_phase,'ready')
        self.assertEqual({name:upgrade._identity(resource.root) for name,resource in current.plan.resources.items()},roots)
        self.assertEqual(set(self.docker.run(['docker','volume','ls','-q','--filter','label='+mapping.LABEL+'project='+self.project]).splitlines()),volumes)
        container=self.docker.run(['docker','inspect',current.observed['nginx']['container_id']],json_output=True)[0]
        self.addresses[:]=[row['IPAddress'] for row in container['NetworkSettings']['Networks'].values() if row.get('IPAddress')]
        status,_headers,content=self.client.request('GET','/api/v1/media/admin/download?'+urllib.parse.urlencode({'paths':json.dumps([sample['path']])}),
            headers={'Range':'bytes=3-9'})
        self.assertEqual((status,content),(206,(ROOT/'tests/fixtures/tone.mp3').read_bytes()[3:10]))
        post_open=self.seed(current,filename='new-after-original-abort.mp3')
        opened=(self.staged_entry(stage,'staged-original-abort-resume',resume=True) if staged
            else self.process('independent-original-abort-resume',flags=['--abort-early']))['result']
        self.assertTrue(opened['upgrade_aborted'])
        self.assertTrue(opened['original_resources_reused'])
        self.assertFalse(opened['database_restored'])
        self.assertEqual(opened['generation'],4)
        duplicate=(self.staged_entry(stage,'staged-original-abort-duplicate',recover=True) if staged
            else self.process('independent-original-abort-duplicate',flags=['--abort-early']))['result']
        self.assertEqual(duplicate,opened)
        self.assertEqual(payload.stat().st_ino,inode)
        self.assertEqual(backup.digest(payload)[1],sample['sha256'])
        self.assertEqual(backup.digest(self.data/'media'/post_open['path'])[1],post_open['sha256'])
        self.assertEqual({path.name:backup.digest(path)[1] for path in partial.iterdir() if path.is_file()},retained)
        self.assertFalse((partial/'manifest.json').exists())
        prepare._command(current.docker,['python3','-B',str(ROOT/'scripts/frontiercloud_operator.py'),'inspect'],
            self.evidence,'host-original-abort-default-inspect',timeout=60)
        cpu=self.fixture.run_command(['python3','scripts/check_cpu_quiescence.py',current.observed['web']['container_id'],'--timeout','30'])
        self.assertIn('for 5 samples',cpu)
        backup.private_write(self.evidence/'result.json',backup.json_bytes({'passed':True,'old':proof,'sample':sample,
            'unfenced_cancel':cancelled,'injected_backup_failure':True,'incomplete_snapshot_retained':True,
            'snapshot_used':False,'original_resources_reused':True,'database_restored':False,'original_payload_inode':inode,
            'final_payload_inode':payload.stat().st_ino,'opening_death':death,'opening':opened,'post_open_sample':post_open,
            'original_session_range_status':status,'new_volume_created':False,'staged_recover_and_resume':staged,
            'cpu':cpu,'public_release_proved':False}))

    def test_early_sqlite_abort_preserves_incomplete_snapshot_and_original_resources(self):
        self.exercise_early_abort('sqlite')

    def test_early_mysql_abort_preserves_incomplete_snapshot_and_original_resources(self):
        self.exercise_early_abort('mysql')

    def test_staged_recover_preserves_sqlite_original_resources(self):
        self.exercise_early_abort('sqlite',staged=True)

    def test_staged_recover_preserves_mysql_original_resources(self):
        self.exercise_early_abort('mysql',staged=True)

    def public_source_refusal(self):
        for command in ('resume','recover'):
            step='public-'+command+'-refuses-local-source'
            with self.assertRaises(mapping.DeploymentError):
                prepare._command(self.docker,['python3','-B',str(ROOT/'scripts/frontiercloud_operator.py'),command],self.evidence,step)
            refusal=json.loads((self.evidence/(step+'.log')).read_text())
            self.assertEqual(refusal['error'],'source_not_published')
            self.assertEqual(refusal['recorded_phase'],'images_prepared')
            self.assertFalse((self.data/upgrade.HARD).exists())
