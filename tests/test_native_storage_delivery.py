import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from starlette.responses import Response

from app.middleware import native_storage as native
from app.services import media_manager,karaoke_storage
from app.services.federation.state import state


@unittest.skipIf(os.name=="nt","Native storage product runs in Linux Compose")
class NativeStorageDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_fstat_closes_the_already_opened_file_descriptor(self):
        opened = []
        def fail(descriptor):
            opened.append(descriptor)
            raise OSError('simulated inode inspection failure')
        with patch.object(native.os, 'fstat', fail):
            with self.assertRaisesRegex(OSError, 'inode inspection failure'):
                native._open_beneath(self.media, ['music', 'artist', 'cipher.mp3'])
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])
        self.assertEqual((self.media/'music/artist/cipher.mp3').read_bytes(), self.payload)

    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.media=self.root/"media";self.recordings=self.root/"recordings"
        (self.media/"music/artist").mkdir(parents=True)
        self.payload=b"opaque ciphertext has no server decryption step"
        (self.media/"music/artist/cipher.mp3").write_bytes(self.payload)
        self.recording_path=f"{'a'*32}/{'b'*32}/{'c'*32}.bin"
        (self.recordings/Path(self.recording_path).parent).mkdir(parents=True)
        (self.recordings/self.recording_path).write_bytes(self.payload)
        self.patches=[patch.object(native.settings,"DEPLOYMENT_MODE","only_stroge"),patch.object(state,"node",{"role":"Follower"}),
                      patch.object(media_manager,"MEDIA_ROOT",self.media),patch.object(karaoke_storage,"ROOT",self.recordings)]
        for item in self.patches:item.start()
        app=FastAPI();app.add_middleware(native.NativeStorageDeliveryMiddleware)
        @app.api_route('/media',methods=['GET','HEAD'])
        async def media():
            return Response(headers={"X-Accel-Redirect":"/_protected_media/music/artist/cipher.mp3","Content-Type":"audio/mpeg",
                "X-Media-Object-ID":"d"*64,"Access-Control-Allow-Origin":"https://master.4399.com","Cache-Control":"no-store"})
        @app.api_route('/recording',methods=['GET','HEAD'])
        async def recording():
            return Response(headers={"X-Accel-Redirect":"/_protected_recordings/"+self.recording_path,"Content-Type":"audio/webm",
                "Content-Disposition":"attachment; filename=recording.webm","Cache-Control":"private, no-store"})
        @app.get('/untrusted')
        async def untrusted():return Response(headers={"X-Accel-Redirect":"https://evil.invalid/secret"})
        @app.get('/unauthorized')
        async def unauthorized():return Response(status_code=401)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="https://storage.4399.com")

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()

    async def test_opaque_full_range_head_conditional_and_cors_provenance(self):
        full=await self.client.get('/media');self.assertEqual(full.status_code,200);self.assertEqual(full.content,self.payload)
        self.assertEqual(full.headers['content-type'],'audio/mpeg');self.assertEqual(full.headers['x-media-object-id'],'d'*64)
        self.assertEqual(full.headers['access-control-allow-origin'],'https://master.4399.com')
        self.assertNotIn('x-accel-redirect',full.headers)
        partial=await self.client.get('/media',headers={'Range':'bytes=3-11'});self.assertEqual(partial.status_code,206);self.assertEqual(partial.content,self.payload[3:12])
        head=await self.client.head('/media');self.assertEqual(head.status_code,200);self.assertEqual(head.content,b'');self.assertEqual(int(head.headers['content-length']),len(self.payload))
        modified=await self.client.get('/media',headers={'If-None-Match':full.headers['etag']});self.assertEqual(modified.status_code,304)
        stale=await self.client.get('/media',headers={'Range':'bytes=0-3','If-Range':'"changed"'});self.assertEqual(stale.status_code,200);self.assertEqual(stale.content,self.payload)
        multiple=await self.client.get('/media',headers={'Range':'bytes=0-3,8-11'});self.assertEqual(multiple.status_code,206);self.assertIn(b'opaque'[0:4],multiple.content)
        bad=await self.client.get('/media',headers={'Range':'bytes=999-'});self.assertEqual(bad.status_code,416)

    async def test_recording_range_download_and_client_redirect_header_cannot_choose_file(self):
        result=await self.client.get('/recording',headers={'Range':'bytes=-7'});self.assertEqual(result.status_code,206);self.assertEqual(result.content,self.payload[-7:])
        self.assertIn('attachment',result.headers['content-disposition'])
        denied=await self.client.get('/unauthorized',headers={'X-Accel-Redirect':'/_protected_media/music/artist/cipher.mp3'})
        self.assertEqual(denied.status_code,401);self.assertEqual(denied.content,b'')
        unavailable=await self.client.get('/untrusted');self.assertEqual(unavailable.status_code,404);self.assertEqual(unavailable.content,b'')

    async def test_symlink_substitution_is_rejected_and_pinned_descriptor_survives_rename(self):
        target=self.media/'music/artist/cipher.mp3'
        outside=self.root/'unmanaged';outside.write_bytes(b'never expose')
        target.unlink();target.symlink_to(outside)
        denied=await self.client.get('/media');self.assertEqual(denied.status_code,404)
        target.unlink();target.write_bytes(self.payload)
        original=native._open_internal
        def after_open(value):
            opened=original(value);target.unlink();target.write_bytes(b'replacement');return opened
        with patch.object(native,'_open_internal',side_effect=after_open):
            old=await self.client.get('/media')
        self.assertEqual(old.content,self.payload)
        self.assertEqual((await self.client.get('/media')).content,b'replacement')

    async def test_business_node_retains_nginx_redirect_and_descriptors_close(self):
        before=len(list(Path('/proc/self/fd').iterdir()))
        for _ in range(10):self.assertEqual((await self.client.get('/media')).status_code,200)
        after=len(list(Path('/proc/self/fd').iterdir()));self.assertLessEqual(after,before+1)
        with patch.object(native.settings,'DEPLOYMENT_MODE','only_biz'):
            response=await self.client.get('/media')
        self.assertEqual(response.content,b'');self.assertIn('x-accel-redirect',response.headers)


if __name__=='__main__':unittest.main()
