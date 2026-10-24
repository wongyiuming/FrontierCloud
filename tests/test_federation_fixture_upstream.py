"""Pure construction checks for disposable fixture proxy routing."""
import ast
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
NODE_SOURCE=ROOT/'tests/federation_stack.py'


class FederationFixtureUpstreamTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='fc-fixture-upstream-')
        self.addCleanup(self.temporary.cleanup)
        self.directory=Path(self.temporary.name);self.calls=[]
        tree=ast.parse(NODE_SOURCE.read_text(encoding='utf-8'))
        node=next(row for row in tree.body if isinstance(row,ast.ClassDef) and row.name=='Node')
        constructor=next(row for row in node.body if isinstance(row,ast.FunctionDef) and row.name=='__init__')
        cls=ast.ClassDef(name='Node',bases=[],keywords=[],body=[constructor],decorator_list=[])
        self.base=dict(services={name:dict(environment={'TRUSTED_PROXY_NETWORKS':'172.16.0.0/12'},
            ports=['8000:8000'],volumes=[],networks={}) for name in ('web','nginx','certificate-agent','redis')},volumes={})
        namespace=dict(copy=copy,hashlib=hashlib,ipaddress=ipaddress,json=json,os=os,re=re,ROOT=ROOT,
            ACCEPTANCE_SUBNET='172.30.251.0/24',DOMAIN_ADDRESSES={},command=self.command)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),str(NODE_SOURCE),'exec'),namespace)
        self.Node=namespace['Node']

    def command(self,*arguments,**kwargs):
        self.calls.append(arguments)
        if arguments[:3]==('docker','compose','-f'):
            storage=copy.deepcopy(self.base);storage['services'].pop('nginx')
            return json.dumps(storage)
        if arguments[0]=='sudo':return ''
        raise AssertionError('No native command may run in this pure fixture test')

    def master(self,name='master'):
        return self.Node(name,self.directory,self.base,self.directory/'test-ca/root.crt',self.directory/'bundle.pem',0,
            network='shared-fixture-fleet',address='10.252.250.11',private_subnet=ipaddress.ip_network('10.251.250.0/24'))

    def configuration(self,node):return json.loads(node.configuration.read_text())

    def test_master_backend_alias_exists_only_on_its_private_network(self):
        node=self.master();config=self.configuration(node);web=config['services']['web'];nginx=config['services']['nginx']
        aliases=web['networks']['default']['aliases'];self.assertEqual(len(aliases),1);alias=aliases[0]
        self.assertRegex(alias,r'^fc-fixture-web-[a-f0-9]{16}$')
        self.assertNotIn('aliases',web['networks']['fleet'])
        self.assertEqual(set(web['networks']),{'default','fleet'})
        self.assertEqual(set(nginx['networks']),{'default','fleet'})
        self.assertEqual(nginx['networks']['fleet']['ipv4_address'],'10.252.250.11')
        for name,service in config['services'].items():
            for network,connection in service['networks'].items():
                if name!='web' or network!='default':self.assertNotIn(alias,connection.get('aliases',[]))
        self.assertTrue(all(call[0]=='sudo' for call in self.calls))

    def test_template_changes_only_backend_hostname_and_preserves_tls_and_security(self):
        config=self.configuration(self.master());alias=config['services']['web']['networks']['default']['aliases'][0]
        mounts=[row for row in config['services']['nginx']['volumes'] if row['target']=='/etc/nginx/templates']
        self.assertEqual(len(mounts),1);mount=mounts[0]
        self.assertEqual(mount['type'],'bind');self.assertIs(mount['read_only'],False)
        directory=Path(mount['source']);self.assertTrue(directory.is_dir())
        copied=(directory/'nginx.conf.template').read_text(encoding='utf-8')
        original=(ROOT/'nginx/nginx.conf').read_text(encoding='utf-8')
        self.assertGreater(original.count('proxy_pass http://web:8000;'),0)
        self.assertEqual(copied,original.replace('proxy_pass http://web:8000;',f'proxy_pass http://{alias}:8000;'))
        self.assertNotIn('proxy_pass http://web:8000;',copied)
        self.assertIn('proxy_ssl_verify on;',copied);self.assertIn('sendfile on;',copied)
        self.assertIn('proxy_set_header X-Forwarded-Proto $scheme;',copied)
        self.assertEqual((ROOT/'nginx/nginx.conf').read_text(encoding='utf-8'),original)

    def test_fixture_keeps_private_trust_limits_and_every_node_gets_a_distinct_alias(self):
        first=self.configuration(self.master());second=self.configuration(self.master('other-master'))
        for config in (first,second):
            web=config['services']['web'];environment=web['environment']
            self.assertEqual(environment['TRUSTED_PROXY_NETWORKS'],'10.251.250.0/24')
            self.assertEqual(environment['TLS_ENABLED'],'true');self.assertEqual(web['cpus'],1.0);self.assertEqual(web['mem_limit'],'512m')
            self.assertNotIn('ports',web);self.assertNotIn('ports',config['services']['nginx'])
        self.assertNotEqual(first['services']['web']['networks']['default']['aliases'],second['services']['web']['networks']['default']['aliases'])
        self.assertEqual(self.base['services']['web']['environment']['TRUSTED_PROXY_NETWORKS'],'172.16.0.0/12')

    def test_direct_and_relay_storage_keep_direct_tls_and_empty_proxy_trust(self):
        for index,port in ((1,443),(2,8443)):
            node=self.Node('peer-'+str(index),self.directory,self.base,self.directory/'test-ca/root.crt',self.directory/'bundle.pem',index,
                network='shared-fixture-fleet',address='10.252.250.'+str(11+index),private_subnet='10.251.'+str(250-index)+'.0/24')
            config=self.configuration(node);web=config['services']['web']
            self.assertEqual(web['environment']['TRUSTED_PROXY_NETWORKS'],'')
            self.assertEqual(web['environment']['STORAGE_PORT'],str(port));self.assertEqual(web['environment']['TLS_ENABLED'],'true')
            self.assertFalse((node.directory/'nginx-templates').exists())
            self.assertFalse(any(row['target']=='/etc/nginx/templates' for row in web['volumes']))

    def test_master_private_alias_also_survives_callers_assigning_ipam_after_construction(self):
        node=self.Node('late-ipam',self.directory,self.base,self.directory/'test-ca/root.crt',self.directory/'bundle.pem',0,network='shared-fixture-fleet')
        config=self.configuration(node);alias=config['services']['web']['networks']['default']['aliases'][0]
        config['networks']['default']['ipam']={'config':[{'subnet':'10.253.240.0/24'}]}
        config['services']['web']['environment']['TRUSTED_PROXY_NETWORKS']='10.253.240.0/24'
        self.assertIn('proxy_pass http://'+alias+':8000;', (node.directory/'nginx-templates/nginx.conf.template').read_text())
        self.assertNotIn('aliases',config['services']['web']['networks']['fleet'])
