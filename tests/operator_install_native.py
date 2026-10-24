"""Actual fresh package, resource and first-start acceptance in isolated projects."""
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import time
import unittest
import urllib.parse
import uuid
from unittest.mock import patch

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_entry as host_entry
from scripts import operator_install_package as install
from scripts import operator_install_resources as resources
from scripts import operator_install_start as startup
from scripts import operator_prepare as preparation
from scripts import operator_registry as registry
from scripts import operator_recovery as recovery
from scripts import operator_upgrade as upgrades
from tests import operator_application_native as applications


@unittest.skipUnless(os.environ.get('FC_OPERATOR_INSTALL_RUNTIME')=='1','explicit isolated fresh-package acceptance')
class InstallTests(unittest.TestCase):
    def exercise(self,engine,transport='', *, provision=False,launch=False,restore=False,through_entry=False,directory_conflict=False):
        started=time.monotonic()
        evidence=backup.absolute(os.environ['FC_OPERATOR_INSTALL_EVIDENCE'])/self._testMethodName
        evidence.mkdir(mode=0o700)
        directory=evidence/'deployment';directory.mkdir()
        storage=bool(transport)
        control=evidence/'control'
        identifier='fc-install-'+uuid.uuid4().hex[:12]
        factory=applications.ApplicationTests('runTest');factory.evidence=evidence
        factory.docker=mapping.ComposeDocker(directory,[],identifier,[])
        factory.ca=evidence/'test-ca';factory.clean_images={}
        if launch:
            self.addCleanup(self.remove_fixture_network,factory.docker,identifier)
            factory.docker.run(['docker','network','create','--label',mapping.LABEL+'project='+identifier,
                '--label',mapping.LABEL+'network=default',identifier+'_default'])
            network=factory.docker.run(['docker','network','inspect',identifier+'_default'],json_output=True)[0]
            subnets=[row['Subnet'] for row in network['IPAM']['Config'] if ':' not in row['Subnet']]
            self.assertEqual(len(subnets),1)
        environment=directory/'.env'
        environment.write_text('DB_TYPE='+engine+'\nTLS_ENABLED='+('true' if storage or launch else 'false')+
            '\nSERVER_NAME=install.4399.com\nDATA_DIRECTORY='+str(control)+'\n'+
            ('TRUSTED_PROXY_NETWORKS='+subnets[0]+'\n' if launch and not storage else '')+
            ('STORAGE_TRANSPORT='+transport+'\nSTORAGE_PORT='+('8443' if transport=='relay' else '443')+
                '\nSTORAGE_ENDPOINT=https://install.4399.com'+(':8443' if transport=='relay' else '')+'\n' if storage else ''))
        original=environment.read_bytes()
        names=['secrets-init','media-init','certificate-init','certificate-agent','web','redis','stun']
        if not storage:names+=['nginx','mysql']
        # Isolate fixed names/ports with an explicit local fixture. The real
        # canonical Dockerfiles and engine pulls execute in every profile.
        lines=['name: '+identifier,'services:']
        for name in names:
            lines+=['  '+name+':','    container_name: '+identifier+'-'+name,'    ports: !reset []']
            if name=='web':lines+=['    mem_limit: 512m']
            if storage and name=='certificate-agent':lines+=['    network_mode: !reset null']
            if launch and name==('web' if storage else 'nginx'):
                lines+=['    networks: {default: {aliases: [install.4399.com]}}']
            if name=='mysql':lines+=['    cpus: "1.0"','    mem_limit: 768m']
            if launch and name=='certificate-agent':
                program=(applications.ROOT/'tests/storage_certificate_agent_fixture.py').read_text().replace('/test-ca/',
                    '/var/lib/frontiercloud-certificate/operator-test-ca/')
                lines+=['    command: '+json.dumps(['python','-c',program])]
        override=directory/'fixture.yaml';override.write_text('\n'.join(lines)+'\n')
        stage,verify=factory.candidate()
        if directory_conflict:
            self.refuse_foreign_directory(directory,stage,verify,evidence,factory,control,environment,original,override)
            return
        if launch:self.create_test_ca(factory,evidence)
        if through_entry:
            self.start_through_entry(directory,stage,evidence,factory,environment,original,started,storage=storage)
            return
        def build(docker,arguments,output,step,**values):
            preparation._command(docker,arguments,output,step,**values)
            if launch and step=='install-image-build':
                config=docker.config()
                for kind in ('web',) if storage else ('web','nginx'):
                    factory.trust(config['services'][kind]['image'],kind,output,'install-'+kind)
        package=install.prepare(directory,stage,evidence/'package',storage=storage,overrides=[override],verify=verify,acceptance=True,
            execute=build if launch else preparation._command)
        loaded=install.Package.load(package.directory,verify=verify,acceptance=True)
        self.assertEqual(loaded.receipt,package.receipt)
        self.assertEqual(package.receipt['fresh_resources']['database'],engine)
        self.assertEqual(package.receipt['fresh_resources']['role'],'storage' if storage else 'business')
        self.assertFalse(control.exists())
        self.assertEqual(environment.read_bytes(),original)
        self.assertFalse((directory/'docker-compose.yaml').exists())
        self.assertFalse((directory/'docker-compose.storage.yaml').exists())
        self.assertFalse(package.docker.run(['docker','ps','-aq','--filter','label='+mapping.LABEL+'project='+identifier]))
        self.assertFalse(package.docker.run(['docker','volume','ls','-q','--filter','label='+mapping.LABEL+'project='+identifier]))
        self.assertNotIn('old_deployment',package.receipt)
        self.assertTrue(package.receipt['acceptance_only'])
        if storage:self.assertEqual(package.config['services']['web']['environment']['STORAGE_TRANSPORT'],transport)
        if storage:self.assertEqual(package.config['services']['web']['environment']['TRUSTED_PROXY_NETWORKS'],'')
        with self.assertRaises(mapping.DeploymentError) as refused:install.Package.load(package.directory,verify=verify)
        self.assertEqual(refused.exception.code,'source_not_published')
        if launch:
            self.start_application(package,verify,evidence,factory,environment,original,started,restore=restore)
            return
        owned=None
        if provision:
            operation=resources.Installation.begin(package,verify=verify,acceptance=True)
            self.addCleanup(self.remove_owned_volumes,package,operation.state['id'])
            original_run=operation.package.docker.run
            injected=False;first_name=None;first_inode=None
            def lost_reply(arguments,**values):
                nonlocal injected,first_name,first_inode
                result=original_run(arguments,**values)
                if arguments[:3]==['docker','volume','create'] and not injected:
                    injected=True;first_name=arguments[-1]
                    first_inode=Path(operation.package.docker.volume(first_name)['Mountpoint']).stat().st_ino
                    backup.private_write(evidence/'fault.json',backup.json_bytes({'fixture_only':True,
                        'fault':'lost_actual_volume_create_reply','volume':first_name,'inode':first_inode}))
                    raise OSError('fixture lost successful volume-create reply')
                return result
            with patch.object(operation.package.docker,'run',side_effect=lost_reply),self.assertRaisesRegex(OSError,'lost successful'):
                operation.resources()
            self.assertTrue(injected)
            resumed=resources.Installation.load(directory,verify=verify,acceptance=True)
            owned=resumed.resources()
            self.assertFalse(owned['services_started']);self.assertFalse(owned['installation_complete'])
            self.assertEqual(owned['owned_volumes'][first_name]['inode'],first_inode)
            self.assertEqual(resources.Installation.load(directory,verify=verify,acceptance=True).resources(),owned)
            self.assertTrue(control.is_dir());self.assertEqual(list(control.iterdir()),[])
            self.assertEqual(environment.read_bytes(),original)
            self.assertFalse(package.docker.run(['docker','ps','-aq','--filter','label='+mapping.LABEL+'project='+identifier]))
            self.assertNotIn('old_deployment',resumed.state);self.assertNotIn('snapshot',resumed.state)
            for item in package.receipt['canonical_files']:
                self.assertEqual(backup.digest(Path(item['target']))[1],item['sha256'])
        backup.private_write(evidence/'result.json',backup.json_bytes({'passed':True,'project':identifier,'role':'storage' if storage else 'business',
            'database':engine,'transport':transport,'source_stage_sha256':package.receipt['source_stage_sha256'],
            'images':package.receipt['images'],'real_canonical_images_built':True,'pinned_package_reloaded':True,
            'environment_sha256':hashlib.sha256(original).hexdigest(),'environment_unchanged':True,'control_created':provision,
            'canonical_compose_created':provision,'project_containers':[],'project_volumes':list(owned['owned_volumes']) if owned else [],
            'deployment_started':False,'owned_resource_result':owned,'actual_volume_create_reply_lost_and_reconciled':provision,
            'public_source_proved':False,'public_load_refused_local_source':True,'seconds':round(time.monotonic()-started,3)}))

    def test_business_sqlite_package(self):self.exercise('sqlite')
    def test_business_mysql_package(self):self.exercise('mysql')
    def test_direct_storage_package(self):self.exercise('sqlite','direct')
    def test_relay_storage_package(self):self.exercise('sqlite','relay')

    def remove_owned_volumes(self,package,identifier):
        package.docker.audit_file=package.directory/'install-fixture-cleanup-inputs.jsonl'
        for name in package.receipt['fresh_resources']['volumes']:
            value=package.docker.volume(name)
            if value is None:continue
            self.assertEqual(value['Labels'][resources.LABEL],identifier)
            self.assertEqual(value['Labels'][mapping.LABEL+'project'],package.receipt['project'])
            self.assertEqual(list(Path(value['Mountpoint']).iterdir()),[])
            self.assertFalse(package.docker.run(['docker','ps','-aq','--filter','volume='+name]))
            package.docker.run(['docker','volume','rm',name])

    def test_business_sqlite_owned_resources(self):self.exercise('sqlite',provision=True)
    def test_business_mysql_owned_resources(self):self.exercise('mysql',provision=True)
    def test_direct_storage_owned_resources(self):self.exercise('sqlite','direct',provision=True)
    def test_relay_storage_owned_resources(self):self.exercise('sqlite','relay',provision=True)

    def start_application(self,package,verify,evidence,factory,environment,original,started, *, restore=False):
        directory=Path(package.receipt['project_directory'])
        operation=startup.Installation.begin(package,verify=verify,acceptance=True)
        self.addCleanup(self.remove_started,package,operation.state['id'])
        first=[]
        def lost_start(docker,arguments,output,step,**values):
            name=next(name for name,key in package.receipt['fresh_resources']['volumes'].items() if key=='certificate_state')
            program="""import pathlib,shutil
root=pathlib.Path('/private/operator-test-ca');root.mkdir(mode=0o700)
for name in ('private.pem','root.crt'):
    shutil.copyfile('/fixture/'+name,root/name);(root/name).chmod(0o600)
"""
            preparation._command(docker,['docker','run','--rm','--network','none','--user','0','--cpus','1','--memory','128m',
                '--mount','type=volume,source='+name+',target=/private',
                '--mount','type=bind,source='+str(evidence/'private-test-ca')+',target=/fixture,readonly',
                os.environ.get('FC_OPERATOR_DISCOVERY_IMAGE','frontiercloud-python-archive:db-test-tools'),
                'python','-c',program],evidence,'test-ca-seed')
            preparation._command(docker,arguments,output,step,**values)
            first.extend(row['Id'] for row in docker.containers() if
                (row.get('Config',{}).get('Labels') or {}).get(mapping.LABEL+'project')==package.receipt['project'])
            backup.private_write(evidence/'start-fault.json',backup.json_bytes({'fixture_only':True,'fault':'lost_actual_compose_up_reply',
                'first_container_ids':first}))
            raise OSError('fixture lost actual startup reply')
        with self.assertRaisesRegex(OSError,'lost actual'):operation.start(execute=lost_start)
        self.assertEqual(operation.state['phase'],'starting');self.assertTrue(operation.state['business_may_have_written'])
        resumed=startup.Installation.load(directory,verify=verify,acceptance=True)
        publish=registry.publish
        def lost_publish(*args,**values):
            publish(*args,**values)
            raise OSError('fixture lost actual ready receipt reply')
        with patch.object(registry,'publish',lost_publish),self.assertRaisesRegex(OSError,'lost actual ready'):
            resumed.start()
        self.assertEqual(resumed.state['phase'],'verifying')
        current=mapping.Deployment.discover(directory)
        self.assertEqual(set(first),{row['container_id'] for row in current.observed.values()})
        storage=package.receipt['fresh_resources']['role']=='storage'
        edge=package.docker.run(['docker','inspect',current.observed['web' if storage else 'nginx']['container_id']],json_output=True)[0]
        addresses=[row['IPAddress'] for row in edge['NetworkSettings']['Networks'].values() if row.get('IPAddress')]
        self.assertEqual(len(addresses),1)
        def audit(value):
            with (evidence/'http-inputs.jsonl').open('ab') as stream:stream.write(backup.json_bytes(value)+b'\n')
            (evidence/'http-inputs.jsonl').chmod(0o600)
        resolver=socket.getaddrinfo
        def resolve(host,port,*args,**kwargs):return resolver(addresses[0] if host=='install.4399.com' else host,port,*args,**kwargs)
        endpoint='https://install.4399.com'+(':8443' if storage and package.config['services']['web']['environment']['STORAGE_TRANSPORT']=='relay' else '')
        factory.client=applications.Client(endpoint,audit,factory.ca/'root.crt')
        with patch.object(socket,'getaddrinfo',resolve):
            self.assertEqual(factory.client.request('GET','/health/ready')[0],200)
            sample=None if storage else factory.seed(current,filename='fresh-after-lost-reply.mp3')
            result=startup.Installation.load(directory,verify=verify,acceptance=True).start()
            self.assertTrue(result['installation_complete']);self.assertEqual(result['generation'],9)
            self.assertEqual(startup.Installation.load(directory,verify=verify,acceptance=True).start(),result)
            if sample:self.assertEqual(backup.digest(current.plan.resources['control'].root/'media'/sample['path'])[1],sample['sha256'])
        self.assertTrue(result['certificate_verified']);self.assertTrue(result['signed_identity_verified'])
        self.assertEqual(environment.read_bytes(),original)
        self.assertFalse((directory/registry.OPERATION).exists())
        self.assertEqual(set(first),{row['container_id'] for row in mapping.Deployment.discover(directory).observed.values()})
        following=None;restored=None
        if restore or package.receipt['fresh_resources']['database']=='mysql':
            original_control=current.plan.resources['control'].root
            original_media_inode=(original_control/'media'/sample['path']).stat().st_ino
            stage,_verify=factory.candidate(folder='following-candidate',revision='e'*40)
            prepared=preparation.prepare(current,stage,evidence/'following-prepared',verify=verify,acceptance=True,
                execute=factory.candidate_build)
            self.assertEqual(prepared.receipt['new_ephemeral_volumes'],{})
            operation=upgrades.Upgrade.begin(current,prepared,verify=verify,acceptance=True)
            if restore:
                operation.capture();operation.prepare_database(verify=verify)
                self.assertEqual(operation.state['database_from_generation'],result['generation'])
                self.assertEqual(operation.state['phase'],'database_prepared')
                self.assertFalse(operation.state.get('business_may_have_written',False))
                transaction=recovery.Recovery(operation);transaction.prepare()
                restored=transaction.open()
                self.assertTrue(restored['recovery_complete'])
                self.assertEqual((restored['generation'],restored['node_id']),(result['generation'],result['node_id']))
                self.assertEqual(recovery.Recovery(upgrades.Upgrade.load(directory,verify=verify,acceptance=True)).open(),restored)
            else:
                following=upgrades.Upgrade.advance(directory,verify=verify,acceptance=True)
                self.assertTrue(following['upgrade_complete'])
                self.assertEqual(following['node_id'],result['node_id'])
            current=mapping.Deployment.discover(directory)
            current_control=current.plan.resources['control'].root
            self.assertEqual((current_control/'media'/sample['path']).stat().st_ino,original_media_inode)
            self.assertEqual(backup.digest(current_control/'media'/sample['path'])[1],sample['sha256'])
            if restore:
                self.assertNotEqual(current_control,original_control)
                self.assertTrue(original_control.is_dir());self.assertFalse((original_control/'media').exists())
                self.assertTrue((original_control/upgrades.HARD).exists());self.assertFalse((current_control/upgrades.HARD).exists())
            edge=package.docker.run(['docker','inspect',current.observed['nginx']['container_id']],json_output=True)[0]
            addresses[:]=[row['IPAddress'] for row in edge['NetworkSettings']['Networks'].values() if row.get('IPAddress')]
            with patch.object(socket,'getaddrinfo',resolve):
                status,headers,content=factory.client.request('GET','/api/v1/media/admin/download?'+urllib.parse.urlencode({'paths':json.dumps([sample['path']])}),
                    headers={'Range':'bytes=3-9'})
            self.assertEqual((status,content),(206,(applications.ROOT/'tests/fixtures/tone.mp3').read_bytes()[3:10]))
            self.assertEqual(current.operator_phase,'ready')
            with self.assertRaises(mapping.DeploymentError) as refused:
                startup.Installation.load(directory,verify=verify,acceptance=True).start()
            self.assertEqual(refused.exception.code,'install_existing_deployment')
        preparation._command(package.docker,['python3',str(applications.ROOT/'scripts/check_cpu_quiescence.py'),
            current.observed['web']['container_id'],'--timeout','30'],evidence,'cpu-idle',timeout=40)
        backup.private_write(evidence/'result.json',backup.json_bytes({'passed':True,'project':package.receipt['project'],
            'database':package.receipt['fresh_resources']['database'],'role':package.receipt['fresh_resources']['role'],
            'actual_compose_up_reply_lost':True,'actual_current_publish_reply_lost':True,'container_ids_preserved':True,
            'post_open_api_upload_and_range_preserved':sample,'environment_unchanged':True,'result':result,
            'normal_discovery_used_current_receipt':True,'public_source_proved':False,'fixture_isolated_trusted_tls':True,
            'following_upgrade':following,'following_candidate_same_application_source':following is not None,
            'controlled_recovery':restored,'restored_control_root':str(current.plan.resources['control'].root) if restored else None,
            'recovery_source_same_application':restore,'original_media_inode_retained':True if restore or following else None,
            'seconds':round(time.monotonic()-started,3)}))

    def create_test_ca(self,factory,evidence):
        factory.ca.mkdir(mode=0o700)
        private=evidence/'private-test-ca';private.mkdir(mode=0o700)
        program="""from tests.certificate_fixtures import TestCA
from cryptography.hazmat.primitives import serialization
import pathlib
ca=TestCA();root=pathlib.Path('/private')
(root/'private.pem').write_bytes(ca.key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
(root/'private.pem').chmod(0o600)
public=ca.root.public_bytes(serialization.Encoding.PEM)
(root/'root.crt').write_bytes(public);pathlib.Path('/public/root.crt').write_bytes(public)
"""
        preparation._command(factory.docker,['docker','run','--rm','--network','none','--user','0','--cpus','1','--memory','128m',
            '--mount','type=bind,source='+str(private)+',target=/private',
            '--mount','type=bind,source='+str(factory.ca)+',target=/public',
            os.environ.get('FC_OPERATOR_DISCOVERY_IMAGE','frontiercloud-python-archive:db-test-tools'),
            'python','-c',program],evidence,'test-ca-create')

    def remove_fixture_network(self,docker,identifier):
        names=docker.run(['docker','network','ls','-q','--filter','label='+mapping.LABEL+'project='+identifier]).splitlines()
        for name in names:
            network=docker.run(['docker','network','inspect',name],json_output=True)[0]
            self.assertEqual(network['Labels'][mapping.LABEL+'project'],identifier)
            self.assertFalse(network['Containers']);docker.run(['docker','network','rm',name])

    def start_through_entry(self,directory,stage,evidence,factory,environment,original,started, *, storage):
        self.addCleanup(self.remove_entry_started,directory)
        driver=copy.copy(factory.docker)
        driver.environment={**driver.environment,'PYTHONDONTWRITEBYTECODE':'1','FC_OPERATOR_INSTALL_RUNTIME':'1',
            'FC_OPERATOR_INSTALL_EVIDENCE':os.environ['FC_OPERATOR_INSTALL_EVIDENCE'],
            'FC_OPERATOR_DISCOVERY_IMAGE':os.environ.get('FC_OPERATOR_DISCOVERY_IMAGE','frontiercloud-python-archive:db-test-tools')}
        def command(step, *, loss='none',first_storage=False,upgrade=False,expected=0):
            arguments=['python3','-I','-B',str(applications.ROOT/'tests/operator_install_handoff_fixture.py'),
                '--directory',str(directory),'--stage',str(stage),'--command','upgrade' if upgrade else 'install','--lose',loss]
            if first_storage:arguments+=['--storage']
            try:preparation._command(driver,arguments,evidence,step,timeout=420)
            except mapping.DeploymentError:
                result=registry.private_json(evidence/(step+'-result.json'))
                if result['exit_code']!=expected or expected==0:raise
            result=registry.private_json(evidence/(step+'-result.json'))
            self.assertEqual(result['exit_code'],expected)
            return json.loads((evidence/(step+'.log')).read_text())
        self.assertEqual(command('cli-install-initial',loss='start',first_storage=storage,expected=35)['exit_code'],35)
        operation=startup.Installation.load(directory,acceptance=True);package=operation.package
        self.assertEqual(operation.state['phase'],'starting');self.assertTrue(operation.state['business_may_have_written'])
        original_ids={row['Id'] for row in package.docker.containers() if
            (row.get('Config',{}).get('Labels') or {}).get(mapping.LABEL+'project')==package.receipt['project']}
        self.assertEqual(command('cli-install-resume-publication',loss='publication',expected=36)['exit_code'],36)
        current=mapping.Deployment.discover(directory)
        self.assertEqual(startup.Installation.load(directory,acceptance=True).state['phase'],'verifying')
        edge=package.docker.run(['docker','inspect',current.observed['web' if storage else 'nginx']['container_id']],json_output=True)[0]
        addresses=[row['IPAddress'] for row in edge['NetworkSettings']['Networks'].values() if row.get('IPAddress')]
        self.assertEqual(len(addresses),1)
        resolver=socket.getaddrinfo
        def resolve(host,port,*args,**values):return resolver(addresses[0] if host=='install.4399.com' else host,port,*args,**values)
        def audit(value):
            with (evidence/'http-inputs.jsonl').open('ab') as stream:stream.write(backup.json_bytes(value)+b'\n')
            (evidence/'http-inputs.jsonl').chmod(0o600)
        endpoint='https://install.4399.com'+(':8443' if storage and package.config['services']['web']['environment']['STORAGE_TRANSPORT']=='relay' else '')
        factory.client=applications.Client(endpoint,audit,factory.ca/'root.crt')
        with patch.object(socket,'getaddrinfo',resolve):
            self.assertEqual(factory.client.request('GET','/health/ready')[0],200)
            sample=None if storage else factory.seed(current,filename='new-after-host-exit.mp3')
        result=command('cli-install-finish')
        self.assertTrue(result['installation_complete']);self.assertEqual(result['phase'],'ready')
        self.assertTrue(result['certificate_verified']);self.assertTrue(result['signed_identity_verified'])
        self.assertEqual(command('cli-install-repeat'),result)
        no_op=command('cli-upgrade-current',upgrade=True)
        self.assertEqual(no_op['state'],'already_current');self.assertFalse(no_op['deployment_changed'])
        current=mapping.Deployment.discover(directory)
        self.assertEqual(original_ids,{row['container_id'] for row in current.observed.values()})
        self.assertEqual(environment.read_bytes(),original);self.assertFalse((directory/registry.OPERATION).exists())
        if sample:
            with patch.object(socket,'getaddrinfo',resolve):
                status,headers,content=factory.client.request('GET','/api/v1/media/admin/download?'+urllib.parse.urlencode({'paths':json.dumps([sample['path']])}),headers={'Range':'bytes=3-9'})
            self.assertEqual((status,content),(206,(applications.ROOT/'tests/fixtures/tone.mp3').read_bytes()[3:10]))
        with self.assertRaises(mapping.DeploymentError) as refused:host_entry._stage(Path(package.receipt['stage_directory']))
        self.assertEqual(refused.exception.code,'source_not_published')
        preparation._command(package.docker,['python3',str(applications.ROOT/'scripts/check_cpu_quiescence.py'),
            current.observed['web']['container_id'],'--timeout','30'],evidence,'cpu-idle',timeout=40)
        backup.private_write(evidence/'result.json',backup.json_bytes({'passed':True,'project':package.receipt['project'],
            'role':package.receipt['fresh_resources']['role'],'database':package.receipt['fresh_resources']['database'],
            'actual_host_exec':True,'host_exit_after_start':35,'host_exit_after_publication':36,
            'initial_storage_flag_only':storage,'container_ids_retained':True,'sample':sample,'result':result,'upgrade_same_release':no_op,
            'source_stage_directory':package.receipt['stage_directory'],'package_directory':str(package.directory),
            'local_source_and_test_ca_admission':True,'public_release_proved':False,'public_stage_refused_local_source':True,
            'seconds':round(time.monotonic()-started,3)}))

    def remove_entry_started(self,directory):
        if not (directory/install.INSTALLATION).exists():return
        operation=startup.Installation.load(directory,acceptance=True)
        self.remove_started(operation.package,operation.state['id'])

    def refuse_foreign_directory(self,directory,stage,verify,evidence,factory,control,environment,original,override):
        docker=factory.docker;docker.audit_file=evidence/'directory-conflict-inputs.jsonl'
        project='fc-install-foreign-'+uuid.uuid4().hex[:12];name=project+'-stopped'
        image=docker.run(['docker','image','inspect',os.environ.get('FC_OPERATOR_DISCOVERY_IMAGE','frontiercloud-python-archive:db-test-tools')],json_output=True)[0]['Id']
        identifier=docker.run(['docker','create','--name',name,'--network','none',
            '--label',mapping.LABEL+'project='+project,'--label',mapping.LABEL+'service=web',
            '--label',mapping.LABEL+'project.working_dir='+str(directory),image,'python','-c','raise SystemExit(0)'])
        def cleanup():
            value=docker.run(['docker','inspect',identifier],json_output=True)[0]
            self.assertEqual((value['Name'],value['Image'],value['Mounts']),('/'+name,image,[]))
            self.assertEqual(value['Config']['Labels'][mapping.LABEL+'project'],project)
            self.assertFalse(value['State']['Running']);docker.run(['docker','rm',identifier])
        self.addCleanup(cleanup)
        def forbidden(*args,**values):raise AssertionError('conflicting directory must refuse before image build/pull')
        with self.assertRaises(mapping.DeploymentError) as caught:
            install.prepare(directory,stage,evidence/'package',overrides=[override],verify=verify,acceptance=True,execute=forbidden)
        self.assertEqual(caught.exception.code,'install_directory_occupied')
        self.assertFalse(control.exists());self.assertFalse((evidence/'package').exists())
        self.assertEqual(environment.read_bytes(),original)
        value=docker.run(['docker','inspect',identifier],json_output=True)[0]
        self.assertEqual(value['State']['Status'],'created')
        backup.private_write(evidence/'result.json',backup.json_bytes({'passed':True,'refusal':'install_directory_occupied',
            'foreign_project':project,'foreign_container':identifier,'foreign_container_untouched_before_fixture_cleanup':True,
            'image_build_or_pull_executed':False,'control_created':False,'package_created':False,'environment_unchanged':True,
            'public_release_proved':False,'fixture_only':True}))

    def remove_started(self,package,identifier):
        package.docker.audit_file=package.directory/'install-fixture-cleanup-inputs.jsonl'
        for row in package.docker.containers():
            labels=row.get('Config',{}).get('Labels') or {}
            if labels.get(mapping.LABEL+'project')!=package.receipt['project']:continue
            name=labels[mapping.LABEL+'service']
            preparation._store(package.directory/('fixture-final-'+name+'.json'),row)
            try:
                preparation._command(package.docker,['docker','logs','--tail','200',row['Id']],package.directory,'fixture-final-'+name,timeout=20)
            except mapping.DeploymentError:pass
        package.docker.run(package.docker.base+['down','--timeout','10'])
        for name in package.receipt['fresh_resources']['volumes']:
            value=package.docker.volume(name)
            if value is None:continue
            self.assertEqual(value['Labels'][resources.LABEL],identifier)
            self.assertEqual(value['Labels'][mapping.LABEL+'project'],package.receipt['project'])
            self.assertFalse(package.docker.run(['docker','ps','-aq','--filter','volume='+name]))
            package.docker.run(['docker','volume','rm',name])
        state_path=package.directory.parent/'following-prepared/upgrade-state.json'
        if state_path.exists():
            value=registry.private_json(state_path).get('recovery',{})
            for attempt in value.get('retained_attempts',[])+([value['attempt']] if value.get('attempt') else []):
                token=attempt['token']
                names=package.docker.run(['docker','volume','ls','-q','--filter','label='+mapping.LABEL+'project='+package.receipt['project'],
                    '--filter','label='+recovery.LABEL+'='+token]).splitlines()
                for name in names:
                    volume=package.docker.volume(name)
                    self.assertEqual(volume['Labels'][recovery.LABEL],token)
                    self.assertEqual(volume['Labels'][mapping.LABEL+'project'],package.receipt['project'])
                    self.assertFalse(package.docker.run(['docker','ps','-aq','--filter','volume='+name]))
                    package.docker.run(['docker','volume','rm',name])

    def test_business_sqlite_first_start(self):self.exercise('sqlite',launch=True)
    def test_business_mysql_first_start(self):self.exercise('mysql',launch=True)
    def test_direct_storage_first_start(self):self.exercise('sqlite','direct',launch=True)
    def test_relay_storage_first_start(self):self.exercise('sqlite','relay',launch=True)
    def test_installed_sqlite_controlled_recovery(self):self.exercise('sqlite',launch=True,restore=True)
    def test_installed_mysql_controlled_recovery(self):self.exercise('mysql',launch=True,restore=True)
    def test_business_sqlite_entry_install(self):self.exercise('sqlite',launch=True,through_entry=True)
    def test_business_mysql_entry_install(self):self.exercise('mysql',launch=True,through_entry=True)
    def test_direct_storage_entry_install(self):self.exercise('sqlite','direct',launch=True,through_entry=True)
    def test_relay_storage_entry_install(self):self.exercise('sqlite','relay',launch=True,through_entry=True)
    def test_same_directory_foreign_project_refuses_before_build(self):self.exercise('sqlite',directory_conflict=True)
