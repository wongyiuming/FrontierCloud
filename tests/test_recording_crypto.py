"""Encrypted lyric snapshots stay browser-owned throughout real SQL/storage mutations."""
import base64
import json
import os
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from sqlalchemy import insert, select, text, update

from app.api.v1 import karaoke, karaoke_integrity, karaoke_users
from app.services import karaoke_identity, karaoke_schema as ks, karaoke_storage, recording_crypto as recording
from app.services import media_crypto as crypto, resource_pool
from app.services.federation import protocol as p, schema as fs
from app.services.federation.state import state
from tests import test_media_crypto_http as http_fixture
from tests.test_media_crypto import browser_wrapping
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class RecordingSnapshotFormatTests(unittest.TestCase):
    def snapshot(self):
        descriptor = crypto.Metadata.new(3)
        return {"encryption": descriptor.to_dict(), "ciphertext": base64.b64encode(bytes(descriptor.ciphertext_size)).decode()}

    def test_opaque_format_and_plaintext_coexistence_rejected(self):
        snapshot = self.snapshot()
        valid = recording.metadata({"title": "recording", "lyrics": [], "encrypted_lyrics": snapshot})
        self.assertEqual(recording.decode_lyrics({"encrypted_lyrics": snapshot}), ([], snapshot, None))
        expected = recording.reservation(snapshot,"recording",1000)
        self.assertEqual(recording.decode_lyrics(expected)[2],expected)
        self.assertTrue(recording.receipt_matches({"lyrics": expected, "title":"recording"},valid))
        self.assertFalse(recording.receipt_matches({"lyrics": expected, "title":"recording"},{**valid,"title":"changed"}))
        self.assertFalse(recording.receipt_matches({"lyrics": expected, "title":"recording"},None))
        for value in ({**valid,"lyrics":[{"time":0,"text":"plaintext"}]}, {**valid,"encrypted_lyrics":{**snapshot,"ciphertext":"bad!"}}, {**valid,"unimplemented":True}):
            with self.assertRaises(crypto.MetadataError):
                recording.metadata(value)
        with self.assertRaises(crypto.MetadataError):
            recording.reservation(snapshot,"recording",10)

    def test_snapshot_capability_is_bounded_signed_and_never_contains_ciphertext(self):
        snapshot = self.snapshot()
        digest = recording.metadata_sha256({"title":"recording","lyrics":[],"encrypted_lyrics":snapshot})
        credential = p.encode(b"k" * 48)
        token = p.recording_token(credential,"a"*32,"b"*32,"c"*32,"d"*32,"upload",1000,size=1000,
            encrypted_lyrics_encryption=snapshot["encryption"],encrypted_lyrics_sha256=digest)
        value = p.verify_recording_token(credential,token,1001)
        self.assertEqual(recording.token_snapshot(value)[0].file_id,snapshot["encryption"]["file_id"])
        self.assertNotIn('"ciphertext":',json.dumps(value))
        self.assertLess(len(token),2048)
        with self.assertRaises(p.ProtocolError):
            p.verify_recording_token(p.encode(b"x"*48),token,1001)
        with self.assertRaises(p.ProtocolError):
            p.verify_recording_token(credential,token,1300)
        with self.assertRaises(p.ProtocolError):
            p.recording_token(credential,"a"*32,"b"*32,"c"*32,"d"*32,"upload",1000,size=1000,
                encrypted_lyrics_encryption=snapshot["encryption"],encrypted_lyrics_sha256="invalid")
        self.assertNotEqual(recording.preparation_binding("user","browser:one","source","a"*32),
                            recording.preparation_binding("user","browser:one","source","b"*32))


class RecordingCryptoHTTPTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = http_fixture.MediaCryptoHTTPTests.asyncSetUp
    asyncTearDown = http_fixture.MediaCryptoHTTPTests.asyncTearDown
    register = http_fixture.MediaCryptoHTTPTests.register
    session = http_fixture.MediaCryptoHTTPTests.session

    async def setup_recordings(self):
        from app.services import lyrics
        self.user = {"user_id":"2"*32,"username":"recording_owner","status":"active","quota_bytes":16*1024**2,
                     "used_bytes":0,"password_hash":"unused","username_key":"recording_owner","created_at":1,"updated_at":1}
        self.member = {"member_id":"1"*32,"relationship_id":None,"member_kind":"MasterLocal","transport":"Local",
                       "storage_enabled":1,"allocated_bytes":4*1024**3,"used_bytes":0,"reserved_bytes":0,
                       "physical_free_bytes":8*1024**3,"health":"online","writable":1,"updated_at":1}
        async with self.database.begin() as conn:
            await conn.run_sync(ks.metadata.create_all)
            await conn.execute(update(fs.identity).values(role="Master"))
            await conn.execute(insert(ks.users).values(**self.user))
            await conn.execute(insert(fs.storage_members).values(**self.member))
        state.node["role"]="Master"
        for item in (patch.object(karaoke_users,"_user",AsyncMock(side_effect=lambda *args: self.user)),
                     patch.object(karaoke_storage,"ROOT",self.root/"recordings"),
                     patch.object(lyrics,"engine",self.database), patch.object(lyrics,"MEDIA_ROOT",self.media),
                     patch.object(lyrics,"LYRICS_ROOT",self.media/"lyrics"), patch.object(lyrics,"MUSIC_ROOT",self.media/"music"),
                     patch.object(karaoke_users.accounts,"audit",AsyncMock()),
                     patch.object(resource_pool,"choose_member",AsyncMock(return_value=self.member))):
            item.start();self.patches.append(item)
        self.client._transport.app.include_router(karaoke_integrity.router,prefix="/api/v1/karaoke")
        self.client._transport.app.include_router(karaoke_users.router,prefix="/api/v1/karaoke")

    async def source(self, *, encrypted=True):
        identifier, _meta, ciphertext = await self.register("music/artist/song.mp3",encrypted=False)
        lyric_id, descriptor, _bytes = await self.register("lyrics/artist/song.lrc",encrypted=encrypted,plaintext=b"[00:01.00]private lyric source\n")
        async with self.database.begin() as conn:
            await conn.execute(insert(fs.global_media).values(media_id=identifier,object_id=identifier,
                storage_member_id="1"*32,media_path="music/artist/song.mp3",path_locator=resource_pool.path_locator("music/artist/song.mp3"),
                object_kind="audio",size_bytes=len(ciphertext),etag="source",state="active",created_at=1,updated_at=1))
            await conn.execute(text("INSERT INTO media_lyric_links(media_id,media_path,lyric_id,lyric_path) VALUES(:id,:track,:lyric_id,:lyric)"),
                {"id":identifier,"track":"music/artist/song.mp3","lyric_id":lyric_id,"lyric":"lyrics/artist/song.lrc"})
        token="opaque-source-"+"x"*90
        mocked=patch.object(karaoke_identity,"resolve",return_value=("global",identifier));mocked.start();self.patches.append(mocked)
        return token,identifier,descriptor

    async def prepared(self, token):
        grant=await self.session()
        plaintext=json.dumps([{"time":1,"text":"browser saved lyric snapshot"}],separators=(",",":")).encode()
        response=await self.client.post("/api/v1/karaoke/account/recordings/crypto/prepare",json={"media":token,
            "session_id":grant["session_id"],"plaintext_size":len(plaintext)})
        self.assertEqual(response.status_code,200,response.text)
        body=response.json();meta=crypto.Metadata.from_dict(body["encryption"])
        envelope=body["key_envelope"]
        key=AESGCM(browser_wrapping(self.private,grant)).decrypt(base64.b64decode(envelope["iv"]),
            base64.b64decode(envelope["wrapped_key"]),f"frontiercloud:key-envelope:v1:{grant['session_id']}:{meta.file_id}".encode())
        encrypted=AESGCM(key).encrypt(meta.chunk_nonce(0),plaintext,meta.chunk_aad(0))
        snapshot={"encryption":meta.to_dict(),"ciphertext":base64.b64encode(encrypted).decode()}
        metadata={"version":1,"title":"immutable browser title","lyrics":[],"encrypted_lyrics":snapshot}
        footer=json.dumps(metadata,ensure_ascii=False,separators=(",",":")).encode()
        data=b"\x1a\x45\xdf\xa3"+bytes(20)+footer+len(footer).to_bytes(8,"big")+karaoke_storage.TRAILER_MAGIC
        return grant,snapshot,body["preparation_token"],data,plaintext

    async def ticket(self, token, snapshot, preparation, data):
        return await self.client.post("/api/v1/karaoke/account/recordings/ticket",json={"size_bytes":len(data),"content_type":"audio/webm",
            "media":token,"title":"immutable browser title","lyrics":[],"encrypted_lyrics":snapshot,"preparation_token":preparation})

    async def test_browser_snapshot_local_upload_finalize_owner_key_and_deletion(self):
        await self.setup_recordings();token,source_id,_=await self.source()
        grant,snapshot,preparation,data,plaintext=await self.prepared(token)
        response=await self.ticket(token,snapshot,preparation,data)
        self.assertEqual(response.status_code,200,response.text);ticket=response.json();recording_id=ticket["recording_id"]
        async with self.database.connect() as conn:
            pending=(await conn.execute(select(ks.recordings).where(ks.recordings.c.recording_id==recording_id))).mappings().one()
            self.assertIn("expected_encrypted_lyrics",pending["lyrics"])
            self.assertNotIn('"ciphertext":',json.dumps(pending["lyrics"]))
        response=await self.client.put(ticket["upload_url"],content=data)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["metadata"]["encrypted_lyrics"],snapshot)
        response=await self.client.post(f"/api/v1/karaoke/account/recordings/{recording_id}/finalize")
        self.assertEqual(response.status_code,200,response.text)
        response=await self.client.get("/api/v1/karaoke/account/recordings")
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["items"][0]["lyrics"],[])
        self.assertEqual(response.json()["items"][0]["encrypted_lyrics"],snapshot)
        # Historical owner snapshots survive current source visibility changes.
        async with self.database.begin() as conn:
            await conn.execute(text("INSERT INTO media_visibility(relative_path,hidden) VALUES('music/artist/song.mp3',1)"))
        response=await self.client.post(f"/api/v1/karaoke/account/recordings/{recording_id}/lyrics-key",json={"session_id":grant["session_id"]})
        self.assertEqual(response.status_code,200,response.text)
        envelope=response.json()["key_envelope"];meta=crypto.Metadata.from_dict(snapshot["encryption"])
        key=AESGCM(browser_wrapping(self.private,grant)).decrypt(base64.b64decode(envelope["iv"]),
            base64.b64decode(envelope["wrapped_key"]),f"frontiercloud:key-envelope:v1:{grant['session_id']}:{meta.file_id}".encode())
        self.assertEqual(AESGCM(key).decrypt(meta.chunk_nonce(0),base64.b64decode(snapshot["ciphertext"]),meta.chunk_aad(0)),plaintext)
        self.user={**self.user,"user_id":"3"*32}
        response=await self.client.post(f"/api/v1/karaoke/account/recordings/{recording_id}/lyrics-key",json={"session_id":grant["session_id"]})
        self.assertEqual(response.status_code,404)
        self.user={**self.user,"user_id":"2"*32}
        response=await self.client.delete(f"/api/v1/karaoke/account/recordings/{recording_id}")
        self.assertEqual(response.status_code,200,response.text)
        async with self.database.connect() as conn:
            self.assertIsNone(await crypto.encryption_for_id(conn,recording_id,object_kind="recording_lyric"))
            self.assertEqual(await conn.scalar(select(ks.users.c.used_bytes)),0)

    async def test_hidden_changed_relation_wrong_account_and_browser_reject_preparation(self):
        await self.setup_recordings();token,source_id,_=await self.source()
        grant,snapshot,preparation,data,_=await self.prepared(token)
        self.user={**self.user,"user_id":"3"*32}
        response=await self.ticket(token,snapshot,preparation,data)
        self.assertEqual(response.status_code,401,response.text)
        self.user={**self.user,"user_id":"2"*32}
        async with self.database.begin() as conn:
            await conn.execute(text("INSERT INTO media_visibility(relative_path,hidden) VALUES('music/artist/song.mp3',1)"))
        response=await self.ticket(token,snapshot,preparation,data)
        self.assertEqual(response.status_code,404,response.text)
        async with self.database.begin() as conn:
            await conn.execute(text("DELETE FROM media_visibility"))
            await conn.execute(text("DELETE FROM media_lyric_links"))
        response=await self.ticket(token,snapshot,preparation,data)
        self.assertIn(response.status_code,(400,404),response.text)

    async def test_forged_or_plaintext_footer_cannot_finalize_encrypted_reservation(self):
        await self.setup_recordings();token,_,_=await self.source()
        _,snapshot,preparation,data,_=await self.prepared(token)
        response=await self.ticket(token,snapshot,preparation,data)
        self.assertEqual(response.status_code,200,response.text);ticket=response.json()
        # Same byte count is not a proof of the signed footer contents.
        tampered=data.replace(b"immutable browser title",b"tampered! browser title")
        self.assertEqual(len(tampered),len(data))
        response=await self.client.put(ticket["upload_url"],content=tampered)
        self.assertEqual(response.status_code,400,response.text)
        self.assertFalse(karaoke_storage._path("1"*32,"2"*32,ticket["recording_id"]).exists())
        response=await self.client.post(f"/api/v1/karaoke/account/recordings/{ticket['recording_id']}/finalize")
        self.assertEqual(response.status_code,404,response.text)
        response=await self.ticket(token,snapshot,preparation,data)
        self.assertEqual(response.status_code,400,response.text)
        response=await self.client.delete(f"/api/v1/karaoke/account/recordings/{ticket['recording_id']}/pending")
        self.assertEqual(response.status_code,200,response.text)
        # Cancellation keeps the nonce/file ID consumed even though quota is freed.
        response=await self.ticket(token,snapshot,preparation,data)
        self.assertEqual(response.status_code,400,response.text)


if __name__ == "__main__":
    unittest.main()
