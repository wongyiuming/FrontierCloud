"""Signed errors reveal only fixed codes after exact peer/request verification."""
import copy
import json
from types import SimpleNamespace
import unittest

from app.services.federation import protocol as p
from app.services.federation.transport import ControlFailure
from app.services.resource_control_client import signed_request
from app.services.resource_control_errors import response,verify
from app.services.resource_transfer_protocol import TransferError


class SignedControlErrorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.private=p.new_key();self.key=p.public_key(self.private)
        self.store=SimpleNamespace(node=dict(node_id='a'*32,private_key=self.private),unseal=lambda value:value)
        self.relation=dict(peer_id='b'*32,relationship_id='c'*32)
        self.path='/internal/v1/migration/transfer/abort-clean';self.raw=b'{"secret":"never_echo_secret"}'

    def packet(self,exc=None):
        return json.loads(response(self.store,path=self.path,raw=self.raw,relation=self.relation,
            exc=exc or TransferError('migration_abort_stage_changed'),prefix='migration_abort').body)

    def check_packet(self,packet,**changes):
        args=dict(key=self.key,node_id='a'*32,master_id='b'*32,relationship_id='c'*32,path=self.path,raw=self.raw,status=409)
        return verify(packet,**{**args,**changes})

    async def test_exact_code_is_signed_and_request_secret_never_echoed(self):
        packet=self.packet()
        self.assertEqual(self.check_packet(packet),'migration_abort_stage_changed')
        self.assertNotIn('never_echo_secret',json.dumps(packet))

    async def test_each_request_or_identity_binding_change_refuses_the_error(self):
        packet=self.packet()
        for changes in (dict(raw=b'changed'),dict(path=self.path+'-changed'),dict(status=403),dict(node_id='d'*32),
                dict(master_id='d'*32),dict(relationship_id='d'*32),dict(key=p.public_key(p.new_key()))):
            with self.subTest(changes=changes),self.assertRaises(p.ProtocolError):self.check_packet(packet,**changes)

    async def test_untrusted_exceptions_do_not_echo_paths_or_arbitrary_text(self):
        for exc in (ValueError('never_echo_secret'),OSError('/private/never_echo_secret'),p.ProtocolError('never_echo_secret')):
            packet=self.packet(exc);self.assertNotIn('never_echo_secret',json.dumps(packet))
            self.assertIn(self.check_packet(packet),{'migration_abort_request_invalid','migration_abort_filesystem_unavailable'})

    async def test_invalid_shape_types_forgery_and_large_error_are_rejected(self):
        packet=self.packet()
        for changes in (dict(version=True),dict(status=True),dict(code='private error never_echo_secret'),dict(extra='unexpected')):
            forged=p.sign(self.private,{**packet['payload'],**changes})
            with self.subTest(changes=changes),self.assertRaises(p.ProtocolError):self.check_packet(forged)
        for forged in ({},dict(detail='never_echo_secret'),{**packet,'extra':'unexpected'},dict(payload='x'*5000,signature='x')):
            with self.assertRaises(p.ProtocolError):self.check_packet(forged)

    async def test_metadata_client_raises_only_verified_application_code(self):
        master=SimpleNamespace(node=dict(node_id='b'*32),unseal=lambda value:value)
        relation=dict(peer_id='a'*32,peer_key=self.key,peer_endpoint='https://source.4399.com',relationship_id='c'*32,credential='not-logged')
        value=dict(secret='never_echo_secret');raw=p.canonical(value)
        packet=json.loads(response(self.store,path=self.path,raw=raw,relation=self.relation,
            exc=TransferError('migration_abort_stage_changed'),prefix='migration_abort').body)
        async def request(*args,**kwargs):raise ControlFailure(409,packet)
        transport=SimpleNamespace(request=request)
        with self.assertRaisesRegex(TransferError,'^migration_abort_stage_changed$'):
            await signed_request(master,relation,transport,self.path,value,limit=4096,error_type=TransferError,prefix='migration_abort')
        packet['payload']['code']='never_echo_secret'
        with self.assertRaisesRegex(TransferError,'^migration_abort_peer_http_409$'):
            await signed_request(master,relation,transport,self.path,value,limit=4096,error_type=TransferError,prefix='migration_abort')

    async def test_replayed_signed_error_cannot_answer_a_different_request(self):
        packet=self.packet()
        with self.assertRaises(p.ProtocolError):self.check_packet(packet,raw=b'{"nonce":"new-request"}')
