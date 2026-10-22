"""Published Python peers retained across real Follower upgrade and recovery."""
import hashlib
import ipaddress
import json
import time
import urllib.parse

from scripts import operator_backup as backup
from scripts import operator_prepare as prepare
from tests import operator_application_native as application


class Pair:
    def __init__(self,owner,old,profile):
        self.owner,self.profile=owner,profile
        master=application.ApplicationTests()
        master.fixture_hostname='paired-master.4399.com'
        master.fixture_evidence=owner.evidence/'paired-master'
        master.setUp();owner.addCleanup(master.doCleanups)
        self.master=master
        master_old,_proof=master.build_old('sqlite')
        self.network=owner.project+'-pair'
        owner.fixture.run_command(['docker','network','create','--internal','--subnet',owner.fixture.isolated_subnet(),self.network])
        owner.fixture.addCleanup(owner.fixture.run_command,['docker','network','rm',self.network])
        roots=(owner.ca/'root.crt').read_bytes()+(master.ca/'root.crt').read_bytes()
        for fixture,storage in [(owner,True),(master,False)]:
            (fixture.ca/'root.crt').write_bytes(roots)
            config=fixture.active.config()
            fixture.trust(config['services']['web']['image'],'web',fixture.evidence,'paired-web')
            if not storage:
                fixture.trust(config['services']['nginx']['image'],'nginx',fixture.evidence,'paired-nginx')
            config['networks']['paired']={'external':True,'name':self.network}
            config['services']['web'].setdefault('networks',{})['paired']={'aliases':[fixture.hostname]} if storage else {}
            if not storage:
                config['services']['nginx'].setdefault('networks',{})['paired']={'aliases':[fixture.hostname]}
                subnet=ipaddress.ip_network(config['networks']['default']['ipam']['config'][0]['subnet'])
                owner.assertEqual(subnet.prefixlen,24)
                backend_address=str(subnet.network_address+240)
                config['services']['web']['networks']['default']={**(config['services']['web']['networks']['default'] or {}),
                    'ipv4_address':backend_address}
                config['services']['nginx']['extra_hosts']={'web':backend_address}
            base=fixture.active.files[0]
            base.write_bytes(backup.json_bytes(backup.compose_values(config,encode=True)))
            prepare._command(fixture.active,fixture.active.base+['up','-d','--force-recreate','--no-build','--pull','never',
                '--wait','--wait-timeout','180'],fixture.evidence,'paired-network-start',timeout=240)
            deployment=fixture.discover_fixture()
            service='web' if storage else 'nginx'
            info=fixture.docker.run(['docker','inspect',deployment.observed[service]['container_id']],json_output=True)[0]
            fixture.addresses[:]=[info['NetworkSettings']['Networks'][self.network]['IPAddress']]
            fixture.client=application.Client(fixture.endpoint,fixture.audit,fixture.ca/'root.crt')
        self.deployment=owner.discover_fixture()
        master_old=master.discover_fixture()
        master.seed(master_old)
        self.api(master,'/api/v1/media/admin/nodes/promote',dict(role='Master',endpoint=master.endpoint,local_capacity_gib=1))
        key=(self.deployment.plan.resources['secrets'].root/'admin_key').read_text().strip()
        status,_headers,body=owner.client.request('POST','/api/v1/media/admin/elevate',urllib.parse.urlencode({'token':key}).encode(),
            {'Content-Type':'application/x-www-form-urlencoded'})
        owner.assertEqual(status,200,body[:200])
        package=self.api(owner,'/api/v1/media/admin/nodes/pair-package',{})
        paired=self.api(master,'/api/v1/media/admin/nodes/pair',dict(package=package))
        self.relation=paired['relationship_id']
        self.api(master,f'/api/v1/media/admin/nodes/{self.relation}/mode',dict(mode=profile.capitalize()))
        self.api(master,f'/api/v1/media/admin/nodes/{self.relation}/resources',dict(storage_enabled=True,storage_capacity_gib=1,backup_enabled=False))
        self.before=self.online()
        self.sample=self.upload('before-upgrade.mp3')
        payload=self.deployment.plan.resources['control'].root/'media'/self.sample['path']
        self.inode=payload.stat().st_ino
        owner.assertEqual(backup.digest(payload)[1],self.sample['sha256'])
        owner.audit(dict(phase='paired_old_application_ready',profile=profile,relationship_id=self.relation,
            actual_peer_application=True,certificate_errors_ignored=False))

    def api(self,fixture,path,value=None):
        status,_headers,body=fixture.client.request('GET' if value is None else 'POST',path,
            None if value is None else json.dumps(value).encode(),None if value is None else {'Content-Type':'application/json'})
        self.owner.assertEqual(status,200,body[:200])
        return json.loads(body)

    def online(self,after=0):
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            rows=self.api(self.master,'/api/v1/media/admin/nodes')['relationships']
            row=next((row for row in rows if row['relationship_id']==self.relation),None)
            if row and row['status']=='online' and (row.get('last_heartbeat') or 0)>after:return row
            time.sleep(1)
        raise AssertionError('Original paired relationship did not recover online')

    def upload(self,name):
        blob=(application.ROOT/'tests/fixtures/tone.mp3').read_bytes()
        reservation=self.api(self.master,'/api/v1/media/admin/upload/session',dict(site_type=self.profile,
            target_dir='music/OperatorPaired',relative_path=None,filename=name,size_bytes=len(blob),storage_mode='plain'))
        self.owner.assertEqual(reservation['transport'],self.profile.capitalize())
        url=urllib.parse.urlsplit(reservation['upload_url'])
        if self.profile=='direct':
            self.owner.assertEqual(url.scheme+'://'+url.netloc,self.owner.endpoint)
            status,_headers,body=self.owner.client.request('PUT',url.path+('?' +url.query if url.query else ''),blob,
                {'Content-Type':'application/octet-stream','Origin':self.master.endpoint})
            self.owner.assertEqual(status,200,body[:200])
            self.api(self.master,f"/api/v1/media/admin/upload/session/{reservation['upload_id']}/finalize",{})
        else:
            status,_headers,body=self.master.client.request('PUT',reservation['upload_url'],blob,{'Content-Type':'application/octet-stream'})
            self.owner.assertEqual(status,200,body[:200])
        logical='music/OperatorPaired/'+name
        self.read(logical,blob,reservation['media_id'])
        return dict(path=logical,sha256=hashlib.sha256(blob).hexdigest(),bytes=len(blob),media_id=reservation['media_id'])

    def read(self,path,blob,media_id):
        stream=urllib.parse.urlencode({'resource_id':media_id})
        status,headers,body=self.master.client.request('GET','/api/v1/media/stream?'+stream,headers={'Range':'bytes=3-9'})
        self.owner.assertEqual(status,206)
        self.owner.assertEqual(body,blob[3:10])
        self.owner.assertEqual({key.lower():value for key,value in headers.items()}.get('content-range'),f'bytes 3-9/{len(blob)}')
        query=urllib.parse.urlencode({'paths':json.dumps([path])})
        status,_headers,body=self.master.client.request('GET','/api/v1/media/admin/download?'+query)
        self.owner.assertEqual(status,200)
        self.owner.assertEqual(hashlib.sha256(body).digest(),hashlib.sha256(blob).digest())

    def verify(self,current):
        info=self.owner.docker.run(['docker','inspect',current.observed['web']['container_id']],json_output=True)[0]
        self.owner.addresses[:]=[info['NetworkSettings']['Networks'][self.network]['IPAddress']]
        after=self.online(after=int(time.time()))
        for key in ('relationship_id','peer_id','peer_endpoint','mode','state','direction'):
            self.owner.assertIn(key,self.before)
            self.owner.assertEqual(after[key],self.before[key],key)
        payload=current.plan.resources['control'].root/'media'/self.sample['path']
        self.owner.assertEqual(payload.stat().st_ino,self.inode)
        self.owner.assertEqual(backup.digest(payload)[1],self.sample['sha256'])
        self.read(self.sample['path'],(application.ROOT/'tests/fixtures/tone.mp3').read_bytes(),self.sample['media_id'])
        new=self.upload('after-operation.mp3')
        return dict(actual_old_master_and_follower=True,relationship_id=self.relation,original_relationship_online=True,
            fresh_post_operation_heartbeat=after['last_heartbeat'],
            original_payload_inode=self.inode,original_payload=self.sample,new_payload=new,original_range_status=206,
            certificate_errors_ignored=False)
