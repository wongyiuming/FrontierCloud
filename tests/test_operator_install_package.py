"""Fresh package preparation never initializes or replaces business state."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_install_package as install


@unittest.skipUnless(sys.platform=='linux','Linux fresh-root and private-package semantics')
class InstallPackageTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.directory=self.root/'deployment';self.directory.mkdir()
        self.stage=self.root/'stage';self.stage.mkdir(mode=0o700)
        self.source=self.stage/'source';self.source.mkdir()
        self.output=self.root/'package';self.data=self.directory/'data'
        self.environment=self.directory/'.env';self.environment.write_bytes(b'OWNER_SECRET=literal$private\n')
        self.base=self.source/'docker-compose.yaml';self.base.write_bytes(b'canonical unit Compose\n')
        self.receipt={'stage_sha256':'a'*64,'provenance':{'revision':'b'*40,'public_provenance_verified':True}}
        self.config={'name':'fixture-install','services':{
            'web':{'image':'old-web-name','cpus':'1.0','environment':{'DB_TYPE':'sqlite'},
                'volumes':[{'type':'bind','source':str(self.data),'target':'/app/data'},
                    {'type':'volume','source':'secrets','target':'/run/frontiercloud-secrets'}]},
            'redis':{'image':'redis:fixture','volumes':[{'type':'volume','source':'cache','target':'/data'}]},
            'nginx':{'image':'old-nginx-name'}},
            'volumes':{'secrets':{},'cache':{}}}
        self.containers=[];self.volumes={};self.images={};self.commands=[];self.mutate=None
        outer=self
        class Docker:
            def __init__(self,directory,files,name,env_files):
                self.directory=directory;self.files=files;self.name=name;self.environment={}
                self.base=['docker','compose','--project-directory',str(directory)]
                for path in files:self.base+=['-f',str(path)]
                self.base+=['--project-name',name]
            def config(self):
                return backup.compose_values(json.loads(self.files[0].read_text()))
            def local_root(self):return outer.root/'docker'
            def containers(self):return copy.deepcopy(outer.containers)
            def volume(self,name):return outer.volumes.get(name)
            def run(self,arguments, *, json_output=False):
                if arguments[:3]==['docker','volume','ls']:
                    return '\n'.join(name for name,row in outer.volumes.items() if row.get('owned'))
                if arguments[:2]==['docker','version']:return {'Arch':'amd64','Os':'linux'}
                if arguments[:3]==['docker','image','inspect']:return [outer.images[arguments[-1]]]
                raise AssertionError(arguments)
        self.Docker=Docker

    def image(self,tag,labels=None):
        identity='sha256:'+hashlib.sha256(tag.encode()).hexdigest()
        image={'Id':identity,'Os':'linux','Architecture':'amd64','Config':{'Labels':labels or {}}}
        self.images[tag]=image;self.images[identity]=image

    def execute(self,docker,arguments,output,step):
        self.commands.append((arguments,step))
        if step=='install-image-build':
            for row in docker.config()['services'].values():
                if row.get('build'):self.image(row['image'],row['build']['labels'])
        else:
            self.assertEqual(arguments[:2],['docker','pull'])
            self.image(arguments[-1])
        if self.mutate:self.mutate(step)

    def prepare(self):
        with patch.object(mapping,'ComposeDocker',self.Docker),\
                patch.object(install,'_render',return_value=([self.base],copy.deepcopy(self.config))):
            return install.prepare(self.directory,self.stage,self.output,verify=lambda _:self.receipt,execute=self.execute)

    def test_prepare_keeps_env_and_missing_data_and_canonical_files_untouched(self):
        original=self.environment.read_bytes()
        package=self.prepare()
        self.assertEqual(package.receipt['phase'],'install_package_prepared')
        self.assertFalse(package.receipt['deployment_changed'])
        self.assertFalse(self.data.exists())
        self.assertFalse((self.directory/'docker-compose.yaml').exists())
        self.assertEqual(self.environment.read_bytes(),original)
        self.assertNotIn('old_deployment',package.receipt)
        self.assertNotIn('snapshot',package.receipt)
        self.assertEqual(package.config['services']['web']['mem_limit'],512*1024*1024)
        self.assertTrue(all(row['image'].startswith('sha256:') and row['pull_policy']=='never' and not row.get('build') for row in package.config['services'].values()))
        self.assertEqual([step for _,step in self.commands],['install-image-build','install-image-pull-redis'])
        with patch.object(mapping,'ComposeDocker',self.Docker):
            loaded=install.Package.load(self.output,verify=lambda _:self.receipt)
        self.assertEqual(loaded.receipt,package.receipt)

    def test_nonempty_data_never_reaches_build_and_all_bytes_remain(self):
        self.data.mkdir();marker=self.data/'identity-kept';marker.write_bytes(b'owner identity or pending upload')
        with self.assertRaises(mapping.DeploymentError) as caught:self.prepare()
        self.assertEqual(caught.exception.code,'install_data_occupied')
        self.assertEqual(marker.read_bytes(),b'owner identity or pending upload')
        self.assertEqual(self.commands,[]);self.assertFalse(self.output.exists())

    def test_project_or_foreign_named_volume_refuses_before_build(self):
        for row in ({'owned':True},{'owned':False}):
            self.volumes={'fixture-install_secrets':row}
            with self.assertRaises(mapping.DeploymentError) as caught:self.prepare()
            self.assertEqual(caught.exception.code,'install_volume_occupied')
        self.assertEqual(self.commands,[])

    def test_foreign_stopped_container_mount_and_existing_project_refuse(self):
        for container in ({'Config':{'Labels':{mapping.LABEL+'project':'fixture-install'}}},
                {'Mounts':[{'Type':'bind','Source':str(self.directory)}]}):
            self.containers=[container]
            with self.assertRaises(mapping.DeploymentError):self.prepare()
        self.assertEqual(self.commands,[])

    def test_another_project_in_the_same_working_directory_refuses_before_build(self):
        self.containers=[{'Config':{'Labels':{mapping.LABEL+'project':'other-project',mapping.LABEL+'service':'redis',
            mapping.LABEL+'project.working_dir':str(self.directory)}}}]
        with self.assertRaises(mapping.DeploymentError) as caught:self.prepare()
        self.assertEqual(caught.exception.code,'install_directory_occupied')
        self.assertEqual(self.commands,[]);self.assertFalse(self.output.exists());self.assertFalse(self.data.exists())

    def test_different_existing_compose_is_never_overwritten(self):
        path=self.directory/'docker-compose.yaml';path.write_bytes(b'owner custom Compose\n')
        with self.assertRaises(mapping.DeploymentError) as caught:self.prepare()
        self.assertEqual(caught.exception.code,'install_existing_compose_differs')
        self.assertEqual(path.read_bytes(),b'owner custom Compose\n')
        self.assertEqual(self.commands,[])

    def test_source_and_unknown_mount_fail_before_private_package_or_build(self):
        for mount in ({'type':'bind','source':str(self.root),'target':'/var/run/docker.sock'},
                {'type':'bind','source':str(self.directory),'target':'/app/data'}):
            self.config['services']['web']['volumes']=[mount]
            with self.assertRaises(mapping.DeploymentError):self.prepare()
            self.assertFalse(self.output.exists());self.assertEqual(self.commands,[])

    def test_missing_implicit_env_added_during_build_refuses_completion(self):
        self.environment.unlink()
        self.mutate=lambda step:self.environment.write_bytes(b'NEW_OWNER_SETTING=1\n') if step=='install-image-build' else None
        with self.assertRaises(mapping.DeploymentError) as caught:self.prepare()
        self.assertEqual(caught.exception.code,'install_inputs_changed')
        self.assertFalse((self.output/'install-package.json').exists())
        self.assertEqual(self.environment.read_bytes(),b'NEW_OWNER_SETTING=1\n')

    def test_data_added_during_build_is_retained_and_refuses_completion(self):
        def mutate(step):
            if step=='install-image-build':
                self.data.mkdir();(self.data/'owner-new-data').write_bytes(b'keep new data')
        self.mutate=mutate
        with self.assertRaises(mapping.DeploymentError) as caught:self.prepare()
        self.assertEqual(caught.exception.code,'install_data_occupied')
        self.assertEqual((self.data/'owner-new-data').read_bytes(),b'keep new data')
        self.assertFalse((self.output/'install-package.json').exists())

    def test_build_error_retains_private_intent_without_claiming_package_or_creating_data(self):
        self.mutate=lambda _:(_ for _ in ()).throw(OSError('fixture build failure'))
        with self.assertRaises(OSError):self.prepare()
        self.assertTrue((self.output/'install-intent.json').is_file())
        self.assertFalse((self.output/'install-package.json').exists());self.assertFalse(self.data.exists())

    def test_public_prepare_and_load_cannot_admit_unpublished_fixture(self):
        self.receipt['provenance']['public_provenance_verified']=False
        with self.assertRaises(mapping.DeploymentError) as caught:self.prepare()
        self.assertEqual(caught.exception.code,'source_not_published')
        self.assertEqual(self.commands,[])

    def test_modified_activation_file_or_image_label_refuses_package_load(self):
        self.prepare()
        for image in self.images.values():image['Config']['Labels']={}
        with patch.object(mapping,'ComposeDocker',self.Docker):
            with self.assertRaises(mapping.DeploymentError) as caught:install.Package.load(self.output,verify=lambda _:self.receipt)
        self.assertEqual(caught.exception.code,'install_image_changed')
