"""Native startup ownership and complete ASGI response/cancellation boundaries."""
import asyncio
import os
from pathlib import Path
import tempfile
import threading
import unittest
import subprocess
import sys
from unittest.mock import patch

from app.core.config import settings
from app.core.joined_io import joined_io
from app.middleware.native_storage import NativeStorageDeliveryMiddleware
from app.middleware.node_retirement import NodeRetirementMiddleware
from app.services import media_manager,node_retirement_runtime as runtime,runtime_gate,upload_claims
from app.services.federation.state import state
from app.services.node_retirement_gate import GateError,NAME


def scope(path='/internal/v1/native-test',method='GET',headers=()):
    return dict(type='http',asgi={'version':'3.0','spec_version':'2.4'},http_version='1.1',scheme='https',
        method=method,path=path,raw_path=path.encode(),query_string=b'',headers=list(headers),server=('native.4399.com',443),client=('192.0.2.1',50000),root_path='')


async def receive():return {'type':'http.request','body':b'','more_body':False}


@unittest.skipUnless(os.name=='posix','native runtime requires Linux descriptors and flock')
class RetirementRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name)
        self.native=runtime_gate.acquire(self.root);self.runtime=runtime.Runtime(self.root,self.native)

    async def asyncTearDown(self):
        self.runtime.close();self.native.close();self.temporary.cleanup()

    def wrapped(self,app):return NodeRetirementMiddleware(app,runtime_provider=lambda:self.runtime)

    async def test_actual_native_range_delivery_keeps_lease_after_the_router_returns(self):
        media=self.root/'media';target=media/'music/album/original.mp3';target.parent.mkdir(parents=True)
        payload=b'opaque ciphertext unchanged'*8192;target.write_bytes(payload)
        router_done=asyncio.Event();body_started=asyncio.Event();release=asyncio.Event();messages=[]
        async def router(_scope,_receive,send):
            await send({'type':'http.response.start','status':200,'headers':[(b'x-accel-redirect',b'/_protected_media/music/album/original.mp3')]})
            await send({'type':'http.response.body','body':b''});router_done.set()
        async def send(message):
            messages.append(message)
            if message['type']=='http.response.body' and message.get('body'):
                body_started.set();await release.wait()
        with patch.object(settings,'DEPLOYMENT_MODE','only_stroge'),patch.object(state,'node',{'role':'Follower'}),patch.object(media_manager,'MEDIA_ROOT',media):
            task=asyncio.create_task(self.wrapped(NativeStorageDeliveryMiddleware(router))(scope(headers=[(b'range',b'bytes=3-9')]),receive,send))
            try:
                await asyncio.wait_for(body_started.wait(),3);self.assertTrue(router_done.is_set())
                with self.assertRaisesRegex(GateError,'retirement_requests_busy'):self.runtime.gate.lease(exclusive=True)
            finally:release.set();await task
        self.assertEqual(messages[0]['status'],206)
        self.assertEqual(b''.join(message.get('body',b'') for message in messages),payload[3:10])
        self.assertEqual(target.read_bytes(),payload)
        with self.runtime.gate.lease(exclusive=True):pass

    async def test_request_cancellation_joins_started_file_io_before_exclusive_clearance(self):
        started=asyncio.Event();release=threading.Event();loop=asyncio.get_running_loop();target=self.root/'joined-test-output'
        def operation():
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):raise RuntimeError('test I/O release timeout')
            target.write_bytes(b'finished before the lease is released')
        async def app(_scope,_receive,_send):await joined_io(operation)
        async def send(_message):pass
        task=asyncio.create_task(self.wrapped(app)(scope(method='POST'),receive,send))
        try:
            await asyncio.wait_for(started.wait(),3);task.cancel();await asyncio.sleep(0);await asyncio.sleep(0)
            self.assertFalse(task.done())
            with self.assertRaisesRegex(GateError,'retirement_requests_busy'):self.runtime.gate.lease(exclusive=True)
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(target.read_bytes(),b'finished before the lease is released')
        with self.runtime.gate.lease(exclusive=True):pass

    async def test_two_admitted_requests_both_have_to_finish(self):
        entered=[asyncio.Event(),asyncio.Event()];release=[asyncio.Event(),asyncio.Event()];index=iter(range(2))
        async def app(_scope,_receive,_send):
            number=next(index);entered[number].set();await release[number].wait()
        async def send(_message):pass
        tasks=[asyncio.create_task(self.wrapped(app)(scope(),receive,send)) for _ in range(2)]
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)),3)
            release[0].set();await tasks[0]
            with self.assertRaisesRegex(GateError,'retirement_requests_busy'):self.runtime.gate.lease(exclusive=True)
        finally:
            for event in release:event.set()
            await asyncio.gather(*tasks)
        with self.runtime.gate.lease(exclusive=True):pass

    async def test_exception_releases_lease_and_does_not_become_a_fake_http_success(self):
        async def app(_scope,_receive,_send):raise RuntimeError('original route error')
        async def send(_message):self.fail('route error was masked')
        with self.assertRaisesRegex(RuntimeError,'original route error'):await self.wrapped(app)(scope(),receive,send)
        with self.runtime.gate.lease(exclusive=True):pass

    async def test_busy_runtime_rejects_new_internal_work_but_health_stays_available(self):
        entered=[];messages=[]
        async def app(_scope,_receive,send):
            entered.append(_scope['path']);await send({'type':'http.response.start','status':204,'headers':[]});await send({'type':'http.response.body','body':b''})
        async def send(message):messages.append(message)
        with self.runtime.gate.lease(exclusive=True):
            await self.wrapped(app)(scope(),receive,send)
            self.assertEqual(messages[0]['status'],503);self.assertEqual(entered,[])
            messages.clear();await self.wrapped(app)(scope('/health/ready'),receive,send)
            self.assertEqual(messages[0]['status'],204);self.assertEqual(entered,['/health/ready'])

    async def test_only_the_exact_retirement_post_can_acquire_its_own_exclusive_lease(self):
        async def app(request,_receive,_send):
            if request['path']=='/internal/v1/retirement' and request['method']=='POST':
                with self.runtime.gate.lease(exclusive=True):pass
            else:
                with self.assertRaisesRegex(GateError,'retirement_requests_busy'):self.runtime.gate.lease(exclusive=True)
        async def send(_message):pass
        for path,method in (('/internal/v1/retirement','POST'),('/internal/v1/retirement','GET'),('/internal/v1/retirement-other','POST')):
            await self.wrapped(app)(scope(path,method),receive,send)

    async def test_changed_held_native_filename_blocks_admission_without_removing_evidence(self):
        original=self.root/runtime_gate.LOCK_NAME;retained=original.with_name('retained-runtime-lock');original.rename(retained)
        original.touch(mode=0o600);os.chown(original,self.runtime.native_identity[2],self.runtime.native_identity[3]);messages=[]
        async def app(*_args):self.fail('changed runtime identity reached a native handler')
        async def send(message):messages.append(message)
        await self.wrapped(app)(scope(),receive,send)
        self.assertEqual(messages[0]['status'],503);self.assertTrue(retained.exists());self.assertTrue(original.exists())

    async def test_missing_startup_runtime_rejects_internal_handlers(self):
        messages=[]
        async def app(*_args):self.fail('request ran without startup ownership')
        async def send(message):messages.append(message)
        await NodeRetirementMiddleware(app)(scope(),receive,send)
        self.assertEqual(messages[0]['status'],503)

    async def test_runtime_ownership_registration_and_shutdown_are_exact(self):
        self.runtime.close()
        registered=runtime.start(self.root,self.native)
        try:
            self.assertIs(runtime.current(),registered)
            with self.assertRaisesRegex(GateError,'retirement_runtime_already_started'):runtime.start(self.root,self.native)
            with self.assertRaisesRegex(GateError,'retirement_runtime_owner_changed'):runtime.stop(self.runtime)
            self.assertIs(runtime.current(),registered)
        finally:runtime.stop(registered)
        with self.assertRaisesRegex(GateError,'retirement_runtime_unavailable'):runtime.current()
        self.assertTrue((self.root/NAME).exists());self.assertIsNotNone(self.native.descriptor)

    async def test_file_claims_join_exclusion_before_even_creating_their_directory(self):
        claims=self.root/'upload-leases'
        with self.runtime.gate.lease(exclusive=True):
            with self.assertRaisesRegex(upload_claims.UploadBusy,'completing retirement'):
                with upload_claims.claim('a'*32,root=claims):self.fail('claim entered under final clearance')
            self.assertFalse(claims.exists())
        with upload_claims.claim('a'*32,root=claims):
            with self.assertRaisesRegex(GateError,'retirement_requests_busy'):self.runtime.gate.lease(exclusive=True)
        with self.runtime.gate.lease(exclusive=True):pass

    async def test_separate_process_file_claim_holds_the_same_persistent_request_lease(self):
        code="""import sys
from app.services.upload_claims import claim
with claim('a'*32,root=sys.argv[1]):
 print('held',flush=True)
 sys.stdin.readline()
"""
        child=subprocess.Popen([sys.executable,'-B','-c',code,str(self.root/'recording-leases')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(await asyncio.to_thread(child.stdout.readline),'held\n')
            with self.assertRaisesRegex(GateError,'retirement_requests_busy'):self.runtime.gate.lease(exclusive=True)
            child.stdin.write('release\n');child.stdin.flush()
            self.assertEqual(await asyncio.to_thread(child.wait,10),0,child.stderr.read())
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=10)
            for stream in (child.stdin,child.stdout,child.stderr):stream.close()
        with self.runtime.gate.lease(exclusive=True):pass
