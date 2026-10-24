"""Bounded local RPC, shared readonly work and truthful observation failures."""
import asyncio
import errno
import json
import socket
import unittest
from unittest.mock import AsyncMock,patch

import httpx
from fastapi import Request
from requests import exceptions as requests_errors

from app.api import internal_certificate
from app.api.v1 import admin_nodes
from app.core.config import settings
from app.services.certmanager import client
from app.services.certmanager.manager import classify

STATUS=dict(state='ACTIVE',enabled=True,controller_running=True,https_ready=True,serving_matches=True,
    job={'state':'ACTIVE'})


class CertificateControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.inputs=[];self.handler=None
        async def request(value):
            self.inputs.append(json.loads(value.content))
            if self.handler is not None:return await self.handler(value)
            return httpx.Response(200,json=STATUS)
        self.patching=patch.object(client.httpx,'AsyncHTTPTransport',side_effect=lambda **kwargs:httpx.MockTransport(request))
        self.patching.start()

    async def asyncTearDown(self):
        self.patching.stop()
        await asyncio.sleep(0)
        self.assertIsNone(client._status_task)

    async def test_overlapping_status_joins_once_cancellation_isolated_and_later_read_is_fresh(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def blocked(request):entered.set();await release.wait();return httpx.Response(200,json=STATUS)
        self.handler=blocked
        tasks=[asyncio.create_task(client.certificate_control()) for _ in range(4)]
        await entered.wait();tasks[0].cancel()
        with self.assertRaises(asyncio.CancelledError):await tasks[0]
        self.assertEqual(len(self.inputs),1);self.assertTrue(all(not task.done() for task in tasks[1:]))
        release.set();results=await asyncio.gather(*tasks[1:])
        results[0]['job']['state']='changed by caller'
        self.assertEqual(results[1]['job']['state'],'ACTIVE')
        self.assertEqual((await client.certificate_control())['state'],'ACTIVE')
        self.assertEqual(len(self.inputs),2)

    async def test_browser_downloads_and_invalid_status_arguments_are_never_coalesced(self):
        async def echoed(request):return httpx.Response(200,json=json.loads(request.content))
        self.handler=echoed
        results=await asyncio.gather(*(client.certificate_control(action='download',browser_public_key='public-'+str(i),
            browser_nonce='nonce-'+str(i)) for i in range(3)))
        self.assertEqual({v['browser_nonce'] for v in results},{'nonce-0','nonce-1','nonce-2'})
        self.assertEqual(len(self.inputs),3)
        async def rejected(request):return httpx.Response(409,json={'detail':'private controller detail'})
        self.handler=rejected
        with self.assertRaises(client.CertificateControlError) as caught:
            await client.certificate_control(browser_public_key='not-a-status-argument')
        self.assertEqual(caught.exception.code,'controller_request_rejected')

    async def test_http_status_codes_distinguish_busy_timeout_rejection_and_controller_failure(self):
        for status,code in ((429,'controller_busy'),(503,'controller_busy'),(408,'controller_timeout'),
                (504,'controller_timeout'),(409,'controller_request_rejected'),(500,'controller_unavailable')):
            with self.subTest(status=status):
                async def response(request,status=status):return httpx.Response(status,text='private controller detail')
                self.handler=response
                with self.assertRaises(client.CertificateControlError) as caught:await client.certificate_control()
                self.assertEqual(str(caught.exception),code)

    async def test_rpc_timeout_and_connection_failures_do_not_echo_private_paths(self):
        for error,code in ((httpx.ReadTimeout('private path'),'controller_timeout'),
                (httpx.ConnectError('private path'),'controller_unavailable')):
            with self.subTest(code=code):
                async def failed(request,error=error):raise error
                self.handler=failed
                with self.assertRaises(client.CertificateControlError) as caught:await client.certificate_control()
                self.assertEqual(str(caught.exception),code)

    async def test_all_lifecycle_states_remain_readable_during_issuance_and_renewal(self):
        for state in ('DISABLED','PENDING','VALIDATING','ISSUING','ACTIVE','RENEWING','DEGRADED','FAILED'):
            with self.subTest(state=state):
                async def response(request,state=state):return httpx.Response(200,json={**STATUS,'state':state})
                self.handler=response
                self.assertEqual((await client.certificate_control())['state'],state)
        self.assertEqual(len(self.inputs),8)

    async def test_bad_response_and_unbounded_response_are_distinct(self):
        for body,code in ((b'not-json','controller_response_invalid'),(b'{}','controller_response_invalid'),
                (b'[]','controller_response_invalid'),(b'x'*140000,'controller_response_too_large')):
            with self.subTest(code=code,length=len(body)):
                async def invalid(request,body=body):return httpx.Response(200,content=body)
                self.handler=invalid
                with self.assertRaises(client.CertificateControlError) as caught:await client.certificate_control()
                self.assertEqual(str(caught.exception),code)

    async def test_total_deadline_stops_a_transport_that_never_returns(self):
        stopped=asyncio.Event()
        async def stalled(request):
            try:await asyncio.Event().wait()
            finally:stopped.set()
        self.handler=stalled
        with self.assertRaises(client.CertificateControlError) as caught:await client.certificate_control()
        self.assertEqual(caught.exception.code,'controller_timeout');self.assertTrue(stopped.is_set())

    async def test_local_admin_timeout_is_unknown_and_not_a_failed_certificate(self):
        with patch.object(settings,'TLS_ENABLED',True),patch('app.services.certmanager.certificate_control',
                AsyncMock(side_effect=client.CertificateControlError('controller_timeout'))):
            result=await admin_nodes._certificate_view()
        self.assertEqual((result['state'],result['last_error']),('UNAVAILABLE','controller_timeout'))
        for name in ('controller_running','https_ready','serving_matches'):self.assertIsNone(result[name])

    async def test_authenticated_follower_reports_busy_observation_but_never_success_for_download(self):
        request=Request({'type':'http','method':'POST','path':'/internal/v1/certificate','headers':[]})
        with (patch.object(internal_certificate,'authenticated',AsyncMock(return_value={'direction':'upstream'})),
                patch.object(internal_certificate.state,'node',{'role':'Follower'}),
                patch.object(internal_certificate,'certificate_control',AsyncMock(side_effect=client.CertificateControlError('controller_busy')))):
            request.state.node_control_body=b'{"action":"status"}'
            result=json.loads((await internal_certificate.certificate(request)).body)
            self.assertEqual((result['state'],result['last_error']),('UNAVAILABLE','controller_busy'))
            request.state.node_control_body=b'{"action":"download"}'
            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as caught:await internal_certificate.certificate(request)
            self.assertEqual(caught.exception.status_code,409)


class CertificateErrorClassTests(unittest.TestCase):
    def test_ca_errors_are_distinct_from_local_rpc_and_keep_existing_storage_diagnostics(self):
        for error,code in ((requests_errors.ReadTimeout('private'),'ca_request_timeout'),
                (requests_errors.ConnectTimeout('private'),'ca_request_timeout'),(socket.timeout('private'),'ca_request_timeout'),
                (requests_errors.SSLError('private'),'tls_verification_failed'),
                (requests_errors.ConnectionError('private'),'ca_connection_failed'),
                (OSError(errno.ENOSPC,'private'),'certificate_storage_unavailable')):
            with self.subTest(code=code):self.assertEqual(classify(error),code)
