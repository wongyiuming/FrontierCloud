"""Prepared reloading never trusts mutable tags or unverified local source."""
import copy
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_prepare as prepare
from scripts import operator_upgrade as upgrade


@unittest.skipUnless(sys.platform == 'linux','Linux host preparation ownership')
class PreparedReloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root/'deployment'
        self.directory.mkdir()
        self.stage = self.root/'stage'
        self.stage.mkdir(mode=0o700)
        (self.stage/'source').mkdir()
        self.output = self.root/'prepared'
        self.output.mkdir(mode=0o700)
        self.identifier = uuid.uuid4().hex
        self.provenance = {'revision':'f'*40,'public_provenance_verified':False}
        self.config = {'name':'fixture','services':{
            name:{'image':'sha256:'+character*64,'pull_policy':'never'}
            for name,character in [('web','a'),('redis','b'),('nginx','c')]}}
        self.config['services']['web']['environment'] = {'DB_TYPE':'sqlite'}
        for filename in ('compose.json','old-compose.json'):
            backup.private_write(self.output/filename,backup.json_bytes(self.config))
        self.receipt = {'version':1,'id':self.identifier,'phase':'images_prepared','project':'fixture',
            'project_directory':str(self.directory),'provenance':self.provenance,'acceptance_only':True,
            'stage_directory':str(self.stage),'source_directory':str(self.stage/'source'),'source_stage_sha256':'d'*64,
            'compose_sha256':backup.digest(self.output/'compose.json')[1],
            'old_compose_sha256':backup.digest(self.output/'old-compose.json')[1],
            'configuration_sha256':mapping.checksum(self.config),'old_inputs':[],'old_env_files':[],'old_profiles':[],
            'old_deployment':{'role':'business','database':'sqlite'},
            'images':{name:{'id':definition['image'],'os':'linux','architecture':'amd64'} for name,definition in self.config['services'].items()}}
        backup.private_write(self.output/'prepared.json',backup.json_bytes(self.receipt))
        self.calls = []
        self.bad_label = False
        self.bad_platform = False
        owner = self
        class Docker:
            def __init__(self,directory,files,name,env_files):
                self.directory,self.files,self.name,self.env_files = directory,files,name,env_files
            def local_root(self):return owner.root
            def config(self):return copy.deepcopy(owner.config)
            def run(self,arguments,**_options):
                owner.calls.append(arguments)
                owner.assertEqual(arguments[:3],['docker','image','inspect'])
                return [{'Id':arguments[3],'Os':'linux','Architecture':'arm64' if owner.bad_platform else 'amd64',
                    'Config':{'Labels':{'io.frontiercloud.operator.id':owner.identifier,
                        'io.frontiercloud.operator.source':'wrong' if owner.bad_label else 'd'*64}}}]
        self.factory = Docker

    def verify(self,path):
        self.assertEqual(path,self.stage)
        return {'stage_sha256':'d'*64,'provenance':copy.deepcopy(self.provenance)}

    def load(self,**options):
        return prepare.Prepared.load(self.output,verify=self.verify,docker_factory=self.factory,**options)

    def test_reload_verifies_all_pinned_image_identities_without_mutation(self):
        result = self.load(acceptance=True)
        self.assertEqual(result.config,self.config)
        self.assertEqual(result.receipt,self.receipt)
        self.assertEqual(len(self.calls),3)
        self.assertTrue(all(row[:3]==['docker','image','inspect'] for row in self.calls))

    def test_unpublished_preparation_never_resumes_via_the_public_default(self):
        with self.assertRaises(mapping.DeploymentError) as caught:
            self.load()
        self.assertEqual(caught.exception.code,'source_not_published')
        self.assertEqual(self.calls,[])

    def test_changed_source_label_platform_or_compose_refuses(self):
        for field,code in [('bad_label','candidate_image_proof_mismatch'),('bad_platform','prepared_image_changed')]:
            with self.subTest(field=field):
                setattr(self,field,True)
                with self.assertRaises(mapping.DeploymentError) as caught:self.load(acceptance=True)
                self.assertEqual(caught.exception.code,code)
                setattr(self,field,False)
        (self.output/'compose.json').write_bytes(b'{"changed":true}')
        with self.assertRaises(mapping.DeploymentError) as caught:self.load(acceptance=True)
        self.assertEqual(caught.exception.code,'prepared_compose_changed')

    def test_metadata_only_recovery_load_keeps_source_and_configuration_checks(self):
        result = self.load(acceptance=True,check_images=False)
        self.assertEqual(result.config,self.config)
        self.assertEqual(self.calls,[])
        (self.output/'old-compose.json').write_bytes(b'{}')
        with self.assertRaises(mapping.DeploymentError) as caught:self.load(acceptance=True,check_images=False)
        self.assertEqual(caught.exception.code,'prepared_compose_changed')


class AdvanceTests(unittest.TestCase):
    def test_each_completed_phase_is_reloaded_and_opening_is_not_rebacked_up(self):
        phases = iter(['images_prepared','backed_up','database_prepared','active_fenced'])
        calls = []
        result = {'phase':'opened','upgrade_complete':True}
        def loaded(*_args,**_options):
            phase = next(phases)
            calls.append(('load',phase))
            return SimpleNamespace(state={'phase':phase},capture=lambda:calls.append(('capture',phase)),
                prepare_database=lambda **_options:calls.append(('database',phase)),
                activate_fenced=lambda **_options:calls.append(('activate',phase)),open=lambda **_options:result)
        with patch.object(upgrade.Upgrade,'load',side_effect=loaded):
            self.assertEqual(upgrade.Upgrade.advance(Path('/fixture')),result)
        self.assertEqual([name for name,_phase in calls],['load','capture','load','database','load','activate','load'])
        operation = SimpleNamespace(state={'phase':'opening'},open=lambda **_options:result)
        with patch.object(upgrade.Upgrade,'load',return_value=operation):
            self.assertEqual(upgrade.Upgrade.advance(Path('/fixture')),result)
