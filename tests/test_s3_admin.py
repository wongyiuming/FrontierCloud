"""ASGI Admin controls, redacted validation and actual SQL upload publication."""
import base64
import hashlib
import unittest
from unittest.mock import AsyncMock,patch

import httpx
from fastapi import FastAPI, Depends, HTTPException, Request
from sqlalchemy import select

from app.api.v1 import admin_s3 as api
from app.api.v1.admin_transport import require_secure_admin_transport
from app.services import s3_schema as s
from app.services.federation import schema as fs
from app.services.s3_control import ControlError
from app.services.upload_claims import UploadBusy
from tests import test_s3_uploads as fixtures


class S3AdminTests(unittest.IsolatedAsyncioTestCase):
    fixture_type = fixtures.S3UploadTests

    async def asyncSetUp(self):
        self.f = self.fixture_type(methodName='runTest')
        await self.f.asyncSetUp()
        self.f.store.node['role'] = 'Master'
        self.patches = [patch.object(api,'state',self.f.store),patch.object(api.cluster,'node_state',self.f.store),
                        patch.object(api,'Uploads',return_value=self.f.service),
                        patch.object(api.media_crypto,'manager',api.media_crypto.Manager(b'k'*32,now=lambda:self.f.now))]
        for item in self.patches:
            item.start()
        async def session(request:Request):
            if request.cookies.get('admin') != 'fixture':
                raise HTTPException(401,'sign in')
            if request.method != 'GET' and request.headers.get('X-CSRF-Token') != 'csrf':
                raise HTTPException(403,'csrf')
            return 'actor'
        app = FastAPI()
        app.dependency_overrides[api.admin.require_session] = session
        app.include_router(api.router,prefix='/api/v1/media/admin',dependencies=[Depends(require_secure_admin_transport)])
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('192.0.2.1',1234)),
            base_url='https://admin.test',cookies={'admin':'fixture'},headers={'X-CSRF-Token':'csrf'})
        self.root = '/api/v1/media/admin/s3'

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()
        await self.f.asyncTearDown()

    async def test_security_role_transport_session_and_csrf_are_mandatory(self):
        self.assertEqual((await self.client.get(self.root+'/members')).status_code,200)
        self.assertEqual((await self.client.get('http://admin.test'+self.root+'/members')).status_code,426)
        self.client.cookies.clear()
        self.assertEqual((await self.client.get(self.root+'/members')).status_code,401)
        self.client.cookies.set('admin','fixture')
        self.assertEqual((await self.client.post(self.root+'/members',json={},headers={'X-CSRF-Token':'wrong'})).status_code,403)
        self.f.store.node['role']='Follower'
        self.assertEqual((await self.client.get(self.root+'/members')).status_code,409)

    async def test_validation_and_status_never_reflect_credentials(self):
        secret='should-never-appear-private-secret'
        response=await self.client.post(self.root+'/members',json={'credentials':{'secret_key':secret},'quota_bytes':False})
        self.assertEqual(response.status_code,400)
        self.assertNotIn(secret,response.text)
        response=await self.client.post(self.root+'/members',content=b' '*33000)
        self.assertEqual(response.status_code,413)
        response=await self.client.get(self.root+'/members')
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertNotIn('credentials',response.text)
        self.assertNotIn('private-secret',response.text)
        self.assertIsNone(response.json()['members'][0]['physical_free_bytes'])

    async def test_revision_bound_connection_probe_cannot_claim_transfer_scope(self):
        class Probe:
            async def close(self): pass
            async def head_bucket(self): pass
        with patch.object(api,'Control',return_value=Probe()) as control:
            response=await self.client.post(self.root+f'/members/{self.f.member}/probe',json={'revision':2})
            self.assertEqual(response.status_code,409)
            control.assert_not_called()
            response=await self.client.post(self.root+f'/members/{self.f.member}/probe',json={'revision':3})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['scope'],'bucket_connection')
        class BadProbe(Probe):
            async def head_bucket(self): raise ControlError('tls_error')
        with patch.object(api,'Control',return_value=BadProbe()):
            response=await self.client.post(self.root+f'/members/{self.f.member}/probe',json={'revision':4})
        self.assertEqual(response.status_code,503)
        row=(await self.f.members.list())[0]
        self.assertEqual((row['revision'],row['probe_state'],row['enabled']),(5,'error',False))

    async def test_browser_part_confirmation_publishes_with_no_media_request_body(self):
        data=b'original S3 browser upload'
        response=await self.client.post(self.root+'/uploads',json=dict(target_dir='music/album',filename='song.mp3',
            size_bytes=len(data),storage_mode='plain'))
        self.assertEqual(response.status_code,200,response.text)
        reservation=response.json()
        self.assertEqual(reservation['transport'],'S3')
        prefix=self.root+'/uploads/'+reservation['upload_id']
        response=await self.client.post(prefix+'/initialize')
        self.assertEqual(response.status_code,200,response.text)
        digest=base64.b64encode(hashlib.sha256(data).digest()).decode()
        response=await self.client.post(prefix+'/parts/1',json=dict(checksum_sha256=digest))
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.headers['referrer-policy'],'no-referrer')
        self.assertNotIn('private-secret',response.text)
        row=await self.f.row(reservation['upload_id'])
        # Independent provider receives data; the ASGI client sends metadata only.
        self.f.provider.uploads[row['provider_upload_id']]['parts'][1]=data
        response=await self.client.post(prefix+'/parts/1/confirm')
        self.assertEqual(response.json()['confirmed_bytes'],len(data))
        response=await self.client.post(prefix+'/complete')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['state'],'published')
        response=await self.client.post(prefix+'/complete')
        self.assertEqual(response.json()['path'],'music/album/song.mp3')
        self.assertEqual((await self.client.get(self.root+'/uploads')).json()['uploads'],[])
        async with self.f.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(fs.global_media.c.size_bytes)),len(data))
            self.assertEqual(await conn.scalar(select(s.objects.c.content_encrypted)),0)

    async def test_upload_limits_and_fake_encryption_are_rejected_before_reservation(self):
        payload=dict(target_dir='music/album',filename='song.mp3',size_bytes=20,storage_mode='plain')
        with patch.object(api.admin.settings,'ADMIN_MAX_UPLOAD_FILE_SIZE',19):
            response=await self.client.post(self.root+'/uploads',json=payload)
        self.assertEqual(response.status_code,413)
        response=await self.client.post(self.root+'/uploads',json={**payload,'storage_mode':'encrypted','encryption':{'fake':'secret'},'preparation_token':'private-token'})
        self.assertEqual(response.status_code,400)
        self.assertNotIn('private-token',response.text)
        self.assertEqual(await self.f.capacity(),(0,0))

    async def test_encrypted_reservation_requires_current_actor_preparation_and_keeps_descriptor(self):
        meta=api.media_crypto.Metadata.new(20)
        payload=dict(target_dir='music/encrypted',filename='song.mp3',size_bytes=meta.ciphertext_size,storage_mode='encrypted',encryption=meta.to_dict())
        manager=api.media_crypto.manager
        wrong=manager.sign_preparation('admin:other',meta)
        response=await self.client.post(self.root+'/uploads',json={**payload,'preparation_token':wrong})
        self.assertEqual(response.status_code,409)
        self.assertNotIn(wrong,response.text)
        with patch.object(api.media_crypto,'manager',None):
            response=await self.client.post(self.root+'/uploads',json={**payload,'preparation_token':wrong})
        self.assertEqual(response.status_code,503)
        self.assertEqual(await self.f.capacity(),(0,0))
        token=manager.sign_preparation('admin:actor',meta)
        response=await self.client.post(self.root+'/uploads',json={**payload,'preparation_token':token})
        self.assertEqual(response.status_code,200,response.text)
        async with self.f.database.connect() as conn:
            self.assertEqual(await api.media_crypto.encryption_for_id(conn,response.json()['media_id']),meta)
        self.assertEqual(await self.f.capacity(),(0,meta.ciphertext_size))

    async def test_pending_page_is_bounded_and_does_not_load_historical_part_totals(self):
        for number in range(51):
            await self.f.service.reserve(f'music/list/song-{number}.mp3',7,actor='admin')
        response=await self.client.get(self.root+'/uploads')
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()['truncated'])
        rows=response.json()['uploads']
        self.assertEqual(len(rows),50)
        self.assertTrue(all(row['confirmed_bytes']==0 and 'provider_upload_id' not in row for row in rows))

    async def test_busy_upload_is_an_actionable_conflict_without_private_lease_paths(self):
        with patch.object(self.f.service,'initialize',new=AsyncMock(side_effect=UploadBusy('/private/lease-path'))):
            response=await self.client.post(self.root+'/uploads/'+'a'*32+'/initialize')
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.json()['detail']['code'],'s3_upload_busy')
        self.assertNotIn('/private',response.text)
