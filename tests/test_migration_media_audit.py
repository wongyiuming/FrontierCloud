"""Pure parsing regressions for real native Web authorization evidence."""
import ast
import copy
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.migration_media_audit import collect_media_audits,match_master_request,match_target_request


class MigrationMediaAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Execute the actual formatter alone, without importing product settings,
        # a business app, native services or any browser/Docker fixture.
        path=Path(__file__).resolve().parents[1]/'app/core/logging_config.py'
        tree=ast.parse(path.read_text(encoding='utf-8'))
        formatter=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='StructuredFormatter')
        namespace=dict(logging=logging,json=json,datetime=datetime,timezone=timezone,
            settings=SimpleNamespace(INSTANCE_NAME='acceptance'),request_id_context=ContextVar('request_id',default='d'*32),
            trace_id_context=ContextVar('trace_id',default='a'*32))
        exec(compile(ast.Module(body=[formatter],type_ignores=[]),str(path),'exec'),namespace)
        cls.formatter=namespace['StructuredFormatter']()

    def setUp(self):
        self.trace='a'*32;self.owner='b'*32;self.resource='c'*64;self.media='f'*64
        self.expected={self.trace:dict(resource_id=self.resource,object_id=self.media,range='bytes=3-9')}
        audit=dict(resource_id=self.resource,owner_id=self.owner,media_id=self.media)
        self.master=self.record(dict(method='GET',path='/api/v1/media/stream',status=200,range='bytes=3-9',media_audit=audit))
        self.target=self.record(dict(method='GET',path='/internal/v1/media/'+self.media,status=200,range='bytes=3-9',
            media_audit=dict(audit,trace_id=self.trace,parent_request_id='d'*32)))
        self.target['request_id']='e'*32  # Untrusted proxy headers never select native request identity.
        self.parents={self.trace:{'d'*32}}

    def record(self, context):
        row=logging.LogRecord('frontiercloud.http',logging.INFO,'fixture',1,'request_completed',(),None)
        row.context=context
        return json.loads(self.formatter.format(row))

    def master_match(self,row):return match_master_request(json.dumps(row),self.expected,self.owner)
    def target_match(self,row,parents=None):
        return match_target_request(json.dumps(row),self.expected,self.owner,self.parents if parents is None else parents)

    def test_actual_formatter_flattens_context_but_keeps_signed_media_audit_nested(self):
        self.assertNotIn('context',self.master)
        master=self.master_match(self.master);self.assertIsNotNone(master)
        self.assertEqual(master['request_id'],'d'*32)
        target=self.target_match(self.target,{self.trace:{master['request_id']}})
        self.assertEqual(target['media_audit'],dict(resource_id=self.resource,owner_id=self.owner,media_id=self.media,
            trace_id=self.trace,parent_request_id=master['request_id']))
        self.assertNotEqual(target['request_id'],target['media_audit']['parent_request_id'])

    def test_both_hops_require_correct_request_kind_identity_and_range(self):
        changes=[('component','nginx'),('event','other'),('method','HEAD'),('status',206),('status',401),('status','200'),
            ('status',True),('path','/api/v1/media/stream?token=not-public'),('range','bytes=0-6'),('range',None),
            ('trace_id','1'*32),('trace_id','0'*32),('trace_id',[]),('request_id','bad'),('request_id','0'*32)]
        for original,match in ((self.master,self.master_match),(self.target,self.target_match)):
            for key,value in changes:
                with self.subTest(kind=original['path'],key=key,value=value):
                    row=copy.deepcopy(original);row[key]=value;self.assertIsNone(match(row))
            for key,value in (('owner_id','1'*32),('resource_id','2'*64),('media_id','3'*64)):
                with self.subTest(kind=original['path'],audit_key=key):
                    row=copy.deepcopy(original);row['media_audit'][key]=value;self.assertIsNone(match(row))

    def test_target_path_and_authenticated_trace_and_parent_must_match_this_master_request(self):
        self.assertIsNone(self.target_match(self.target,{}))
        for key,value in (('trace_id','1'*32),('trace_id',None),('parent_request_id','e'*32),
                ('parent_request_id','1'*32),('parent_request_id','0'*32),('parent_request_id',None),('parent_request_id',[])):
            with self.subTest(key=key,value=value):
                row=copy.deepcopy(self.target);row['media_audit'][key]=value;self.assertIsNone(self.target_match(row))
        row=copy.deepcopy(self.target);row['path']='/internal/v1/media/'+'1'*64
        self.assertIsNone(self.target_match(row))
        row=copy.deepcopy(self.master);row['path']=self.target['path']
        self.assertIsNone(self.master_match(row))
        self.assertIsNone(self.target_match(self.target,{'1'*32:{'d'*32}}))

    def test_unrelated_or_partial_log_lines_cannot_become_evidence(self):
        for value in ('not json','{"component":','[]','null','true','1',json.dumps('x'),json.dumps({}),
                json.dumps({**self.target,'media_audit':None}),json.dumps({**self.target,'media_audit':[]}),
                json.dumps({'context':self.target}),json.dumps({**self.target,'component':'uvicorn.error'}),' '*65537):
            with self.subTest(prefix=value[:40]):
                self.assertIsNone(match_master_request(value,self.expected,self.owner))
                self.assertIsNone(match_target_request(value,self.expected,self.owner,self.parents))

    def test_authorization_evidence_never_claims_payload_bytes_or_exports_unrelated_log_values(self):
        row=copy.deepcopy(self.target)
        row.update(bytes_sent=7,content_range='bytes 3-9/999',request_completion='OK',token='private-capability',
            exception='private-error',client_ip='private-ip')
        row['media_audit']['credential']='private-credential'
        result=self.target_match(row);self.assertIsNotNone(result)
        self.assertEqual(result['status'],200)  # Browser asserts final 206/bytes/hash separately.
        self.assertEqual(set(result),{'trace_id','request_id','method','path','status','range','media_audit'})
        encoded=json.dumps(result)
        for value in ('bytes_sent','content_range','request_completion','private-','token','credential','exception','client_ip'):
            self.assertNotIn(value,encoded)

    def test_only_a_parent_observed_for_the_same_browser_trace_is_accepted(self):
        valid=self.target_match(self.target,{self.trace:{'d'*32,'7'*32}})
        self.assertIsNotNone(valid)
        self.assertIsNone(self.target_match(self.target,{self.trace:{'7'*32},'1'*32:{'d'*32}}))
        row=copy.deepcopy(self.target);row['media_audit']['parent_request_id']='7'*32
        self.assertIsNotNone(self.target_match(row,{self.trace:{'d'*32,'7'*32}}))

    def test_shared_collector_joins_actual_formatted_records_and_exports_only_safe_matches(self):
        calls=[]
        def command(*arguments,**keywords):
            calls.append((arguments,keywords))
            row=self.master if arguments[-1]=='master-web-id' else self.target
            return 'unrelated native startup line\n'+json.dumps(row)
        result=collect_media_audits(command,'master-web-id','target-web-id',123,self.expected,self.owner)
        self.assertEqual(result['master_authorization_audit'],[self.master_match(self.master)])
        self.assertEqual(result['target_audit'],[self.target_match(self.target)])
        self.assertEqual(calls,[(('docker','logs','--since','123','--tail','400',container),{'timeout':5})
            for container in ('master-web-id','target-web-id')])

    def test_shared_collector_leaves_wrong_parent_incomplete_at_the_original_deadline(self):
        wrong=copy.deepcopy(self.target);wrong['media_audit']['parent_request_id']='1'*32
        def command(*arguments,**keywords):return json.dumps(self.master if arguments[-1]=='master' else wrong)
        with patch('tests.migration_media_audit.time.monotonic',side_effect=[0,0,11]),patch('tests.migration_media_audit.time.sleep'):
            result=collect_media_audits(command,'master','target',123,self.expected,self.owner)
        self.assertEqual(len(result['master_authorization_audit']),1)
        self.assertEqual(result['target_audit'],[])


if __name__=='__main__':unittest.main()
