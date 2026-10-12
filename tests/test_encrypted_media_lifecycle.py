"""Real Python route graph, SQLite and managed filesystem encryption lifecycle."""
import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, UploadFile
from sqlalchemy import select, text

from app.core import db
from app.core.sqlite_compat import create_sqlite_engine
from app.services import media_crypto as crypto, media_manager, media_objects, lyrics, media_directories, admin_service, playback
from app.services.federation.state import state
from tests.test_media_crypto import browser_public, browser_wrapping


class EncryptedMediaLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        for root in ("music/artist", "vido/artist", "lyrics"):
            (self.media/root).mkdir(parents=True)
        self.database=create_sqlite_engine("sqlite+aiosqlite:///"+str(self.root/"business.sqlite"))
        self.patches=[patch.object(db,"engine",self.database),patch.object(media_manager,"engine",self.database),
                      patch.object(admin_service,"engine",self.database),patch.object(playback,"engine",self.database),
                      patch.object(media_manager,"MEDIA_ROOT",self.media),patch.object(media_manager,"LYRICS_ROOT",self.media/"lyrics"),
                      patch.object(media_objects,"engine",self.database),patch.object(lyrics,"engine",self.database),
                      patch.object(lyrics,"MEDIA_ROOT",self.media),patch.object(lyrics,"LYRICS_ROOT",self.media/"lyrics"),
                      patch.object(lyrics,"MUSIC_ROOT",self.media/"music"),patch.object(media_directories,"engine",self.database),
                      patch.object(media_directories,"MEDIA_ROOT",self.media),patch.object(state,"database",self.database),
                      patch.object(state,"node",{}),patch.object(state,"_vault",Fernet(Fernet.generate_key())),
                      patch.object(media_manager,"invalidate_media_catalog",AsyncMock()),
                      patch.object(media_directories,"invalidate_media_catalog",AsyncMock())]
        for item in self.patches:item.start()
        await db.init_db();await state.initialize();await crypto.initialize()
        # Use the exact existing API router graph, including the precedence of
        # integrity/upload wrappers. Only Redis Admin authentication is isolated;
        # the real SQL audit, permission checks and object mutations still run.
        from app.api.v1.endpoints import router as routes
        from app.api.v1 import media_crypto as crypto_api, admin, admin_upload_guard, admin_cluster_integrity
        from app.api.v1 import media as public_media
        for module in (admin,admin_upload_guard,admin_cluster_integrity,public_media):
            for name in ("MEDIA_ROOT","MUSIC_ROOT","VIDEO_ROOT","LYRICS_ROOT"):
                if hasattr(module,name):
                    value=self.media if name=="MEDIA_ROOT" else self.media/{"MUSIC_ROOT":"music","VIDEO_ROOT":"vido","LYRICS_ROOT":"lyrics"}[name]
                    item=patch.object(module,name,value);item.start();self.patches.append(item)
            if hasattr(module,"engine"):
                item=patch.object(module,"engine",self.database);item.start();self.patches.append(item)
        app=FastAPI();app.include_router(crypto_api.router);app.include_router(routes,prefix="/api/v1")
        async def administrator(_request=None):return "a"*64
        app.dependency_overrides[admin.require_session]=administrator
        item=patch.object(crypto_api.admin_service,"require_admin",AsyncMock(return_value="a"*64));item.start();self.patches.append(item)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=("127.0.0.1",12345)),base_url="http://localhost")
        self.private=ec.generate_private_key(ec.SECP256R1())

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):item.stop()
        crypto.manager=None
        await self.database.dispose();self.temp.cleanup()

    async def encrypted_payload(self, plain=b"ID3 opaque media encrypted in browser tests"):
        response=await self.client.post("/api/v1/media/admin/crypto/session",json={"public_key":browser_public(self.private)})
        self.assertEqual(response.status_code,200,response.text);grant=response.json()
        response=await self.client.post("/api/v1/media/admin/crypto/prepare",json={"session_id":grant["session_id"],"plaintext_size":len(plain)})
        self.assertEqual(response.status_code,200,response.text);prepared=response.json();descriptor=crypto.Metadata.from_dict(prepared["encryption"])
        envelope=prepared["key_envelope"]
        key=AESGCM(browser_wrapping(self.private,grant)).decrypt(base64.b64decode(envelope["iv"]),base64.b64decode(envelope["wrapped_key"]),
            f"frontiercloud:key-envelope:v1:{grant['session_id']}:{descriptor.file_id}".encode())
        cipher=AESGCM(key).encrypt(descriptor.chunk_nonce(0),plain,descriptor.chunk_aad(0))
        return prepared,cipher,plain

    async def upload(self,filename,prepared,cipher):
        return await self.client.post("/api/v1/media/admin/upload/item",files={"file":(filename,cipher,"application/octet-stream")},
            data={"target_dir":"music/artist","site_type":"primary","storage_mode":"encrypted","encryption":json.dumps(prepared["encryption"]),
                  "preparation_token":prepared["preparation_token"]})

    async def test_explicit_upload_modes_no_fallback_and_cipher_size(self):
        response=await self.client.post("/api/v1/media/admin/upload/item",files={"file":("plain.mp3",b"ID3 plain","audio/mpeg")},
            data={"target_dir":"music/artist","site_type":"primary"})
        self.assertEqual(response.status_code,422,response.text)
        response=await self.client.post("/api/v1/media/admin/upload/item",files={"file":("plain.mp3",b"ID3 plain","audio/mpeg")},
            data={"target_dir":"music/artist","site_type":"primary","storage_mode":"plain"})
        self.assertEqual(response.status_code,200,response.text)
        prepared,cipher,_=await self.encrypted_payload()
        response=await self.upload("encrypted.mp3",prepared,cipher[:-1])
        self.assertEqual(response.status_code,400,response.text)
        self.assertFalse((self.media/"music/artist/encrypted.mp3").exists())
        response=await self.upload("encrypted.mp3",prepared,cipher)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual((self.media/"music/artist/encrypted.mp3").read_bytes(),cipher)
        self.assertFalse(cipher.startswith(b"ID3"))

    async def test_encrypted_upload_catalog_range_download_rename_and_delete(self):
        prepared,cipher,plain=await self.encrypted_payload()
        response=await self.upload("encrypted.mp3",prepared,cipher)
        self.assertEqual(response.status_code,200,response.text)
        async with self.database.connect() as conn:
            identifier=await conn.scalar(text("SELECT media_id FROM media_objects WHERE media_path='music/artist/encrypted.mp3'"))
            stored=await crypto.encryption_for_id(conn,identifier)
        self.assertEqual(stored.to_dict(),prepared["encryption"])
        response=await self.client.get("/api/v1/media/admin/crypto/bytes?file_path=music/artist/encrypted.mp3",headers={"Range":"bytes=4-18"})
        self.assertEqual(response.status_code,206,response.text);self.assertEqual(response.content,cipher[4:19])
        response=await self.client.get("/api/v1/media/admin/download/plan",params={"paths":json.dumps(["music/artist"])} )
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["items"][0]["encryption"],prepared["encryption"])
        await media_directories.rename_directory("music/artist","renamed")
        self.assertEqual((self.media/"music/renamed/encrypted.mp3").read_bytes(),cipher)
        obj=await crypto.crypto_object("music/renamed/encrypted.mp3",admin=True)
        self.assertEqual(obj["media_id"],identifier);self.assertEqual(obj["encryption"],stored)
        response=await self.client.post("/api/v1/media/admin/delete",json={"paths":["music/renamed/encrypted.mp3"]})
        self.assertEqual(response.status_code,200,response.text)
        self.assertFalse((self.media/"music/renamed/encrypted.mp3").exists())
        async with self.database.connect() as conn:
            self.assertIsNone(await crypto.encryption_for_id(conn,identifier))
            used=await conn.scalar(text("SELECT file_id FROM media_encryption WHERE object_id=:id"),{"id":identifier})
        self.assertEqual(used,stored.file_id)

    async def test_failed_delete_restores_ciphertext_descriptor_and_identity(self):
        prepared,cipher,_=await self.encrypted_payload()
        response=await self.upload("encrypted.mp3",prepared,cipher)
        self.assertEqual(response.status_code,200,response.text)
        async with self.database.connect() as conn:
            identifier=await conn.scalar(text("SELECT media_id FROM media_objects WHERE media_path='music/artist/encrypted.mp3'"))
        async def fail_metadata(*_args):raise RuntimeError("Injected metadata commit failure")
        with patch.object(media_manager.MediaManager,"_delete_metadata",side_effect=fail_metadata):
            with self.assertRaises(RuntimeError):
                await media_manager.MediaManager.delete(["music/artist/encrypted.mp3"])
        self.assertEqual((self.media/"music/artist/encrypted.mp3").read_bytes(),cipher)
        async with self.database.connect() as conn:
            self.assertEqual((await crypto.encryption_for_id(conn,identifier)).to_dict(),prepared["encryption"])
            self.assertEqual(await conn.scalar(text("SELECT COUNT(*) FROM media_delete_operations")),0)

    async def test_encrypted_lyric_is_never_parsed_on_server_and_hidden_source_denies_key(self):
        prepared,cipher,_=await self.encrypted_payload(b"[00:01.00]secret UTF-8 lyric\n")
        with patch.object(lyrics,"parse_lrc_bytes",side_effect=AssertionError("Server parsed ciphertext")):
            response=await self.client.post("/api/v1/media/admin/upload/lyric",files={"file":("song.lrc",cipher,"application/octet-stream")},
                data={"storage_mode":"encrypted","encryption":json.dumps(prepared["encryption"]),"preparation_token":prepared["preparation_token"]})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual((self.media/"lyrics/song.lrc").read_bytes(),cipher)
        plain=UploadFile(filename="source.mp3",file=io.BytesIO(b"ID3 source"))
        await media_manager.MediaManager.upload_one(plain,self.media/"music/artist")
        await lyrics.replace_relations("track","music/artist/source.mp3",["lyrics/song.lrc"])
        response=await self.client.get("/api/v1/media/lyrics/content",params={"track":"music/artist/source.mp3"})
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()["encrypted"])
        page = await self.client.get("/api/v1/media/lyrics", params={"track":"music/artist/source.mp3"})
        self.assertEqual(page.status_code,200,page.text)
        self.assertIn('"encrypted": true',page.text)
        await media_manager.MediaManager.set_hidden(["music/artist"],True)
        response=await self.client.get("/api/v1/media/crypto/bytes?file_path=lyrics/song.lrc")
        self.assertEqual(response.status_code,404,response.text)

    async def test_invalid_lyric_paths_and_resource_ids_are_http_404(self):
        for endpoint in ("/api/v1/media/lyrics", "/api/v1/media/lyrics/content"):
            for track in ("foo", "../music/a.mp3", "music/artist/bad.txt"):
                response = await self.client.get(endpoint, params={"track":track})
                self.assertEqual(response.status_code,404,response.text)
            with patch.object(db.settings,"TLS_ENABLED",True):
                response = await self.client.get("https://localhost" + endpoint,
                    params={"track":"music/artist/source.mp3","resource_id":"not-an-id"})
            self.assertEqual(response.status_code,404,response.text)

    async def test_plaintext_lyric_page_and_content_remain_available(self):
        await media_manager.MediaManager.upload_one(
            UploadFile(filename="source.mp3",file=io.BytesIO(b"ID3 source")),self.media/"music/artist")
        await media_manager.MediaManager.upload_lyric(
            UploadFile(filename="plain.lrc",file=io.BytesIO(b"[00:01.00]plain lyric\n")))
        await lyrics.replace_relations("track","music/artist/source.mp3",["lyrics/plain.lrc"])
        content = await self.client.get("/api/v1/media/lyrics/content",params={"track":"music/artist/source.mp3"})
        self.assertEqual(content.status_code,200,content.text)
        self.assertEqual(content.json()["entries"][0]["text"],"plain lyric")
        page = await self.client.get("/api/v1/media/lyrics",params={"track":"music/artist/source.mp3"})
        self.assertEqual(page.status_code,200,page.text)
        self.assertIn("plain lyric",page.text)

    async def test_encrypted_upload_limit_counts_plaintext_and_accepts_tag_overhead(self):
        oversized,cipher_large,_=await self.encrypted_payload(b"ID3"+b"x"*22)
        with patch.object(media_manager.settings,"ADMIN_MAX_UPLOAD_FILE_SIZE",24):
            prepared,cipher,_=await self.encrypted_payload(b"ID3"+b"x"*21)
            self.assertEqual(len(cipher),40)
            response=await self.upload("boundary.mp3",prepared,cipher)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual((self.media/"music/artist/boundary.mp3").read_bytes(),cipher)
            response=await self.upload("oversized.mp3",oversized,cipher_large)
            self.assertEqual(response.status_code,413,response.text)
            self.assertFalse((self.media/"music/artist/oversized.mp3").exists())
            session=await self.client.post("/api/v1/media/admin/crypto/session",json={"public_key":browser_public(self.private)})
            response=await self.client.post("/api/v1/media/admin/crypto/prepare",json={"session_id":session.json()["session_id"],"plaintext_size":25})
            self.assertEqual(response.status_code,413,response.text)

    async def test_oversize_encrypted_lyric_rejected_before_reading_upload(self):
        from fastapi import HTTPException
        descriptor=crypto.Metadata.new(2*1024*1024+1)
        upload=UploadFile(filename="oversized.lrc",file=io.BytesIO(b"unused"))
        with patch.object(upload,"read",AsyncMock(side_effect=AssertionError("Oversize lyric read"))) as read:
            with self.assertRaises(HTTPException) as error:
                await media_manager.MediaManager.upload_lyric(upload,encryption=descriptor)
        self.assertEqual(error.exception.status_code,413)
        read.assert_not_awaited()
        self.assertFalse((self.media/"lyrics/oversized.lrc").exists())


if __name__=="__main__":unittest.main()
