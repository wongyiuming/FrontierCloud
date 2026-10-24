"""Real SQLite transactions and FastAPI HTTP crypto authorization/Range tests."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.v1 import media_crypto as api
from app.core import db
from app.services import media_crypto as crypto, media_manager, media_objects
from app.services.federation import schema as fs
from app.services.federation.state import state
from app.services.media_crypto_schema import encryption, keys, metadata
from tests.test_media_crypto import browser_public, browser_wrapping
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class MediaCryptoHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        (self.media / "music" / "artist").mkdir(parents=True)
        (self.media / "lyrics" / "artist").mkdir(parents=True)
        # Apply the production adapter to legacy media-object lookup SQL as well.
        from app.core.sqlite_compat import create_sqlite_engine
        self.database = create_sqlite_engine("sqlite+aiosqlite:///" + str(self.root / "test.sqlite"))
        async with self.database.begin() as conn:
            from app.services.s3_schema import metadata as s3_metadata
            await conn.run_sync(s3_metadata.create_all)
            await conn.run_sync(metadata.create_all)
            await conn.run_sync(fs.metadata.create_all)
            from app.services.resource_migration_schema import metadata as migration_metadata
            await conn.run_sync(migration_metadata.create_all)
            from app.services.node_retirement_schema import metadata as retirement_metadata
            await conn.run_sync(retirement_metadata.create_all)
            await conn.execute(text("CREATE TABLE media_objects(media_id TEXT PRIMARY KEY,object_kind TEXT,media_path TEXT,path_locator TEXT UNIQUE,created_at TIMESTAMP,updated_at TIMESTAMP)"))
            await conn.execute(text("CREATE TABLE media_playback_stats(media_id TEXT PRIMARY KEY,media_path TEXT,play_score INTEGER DEFAULT 0,preference INTEGER DEFAULT 0,created_at TIMESTAMP,updated_at TIMESTAMP)"))
            await conn.execute(text("CREATE TABLE media_playback_events(media_id TEXT)"))
            await conn.execute(text("CREATE TABLE media_lyric_links(media_id TEXT PRIMARY KEY,media_path TEXT,lyric_id TEXT,lyric_path TEXT,created_at TIMESTAMP,updated_at TIMESTAMP)"))
            await conn.execute(text("CREATE TABLE media_visibility(relative_path TEXT PRIMARY KEY,hidden INTEGER)"))
            await conn.execute(insert(fs.identity).values(singleton=1,node_id="1" * 32,role="Standalone",endpoint="",private_key="unused",created_at=1))
        self.patches = [patch.object(db, "engine", self.database), patch.object(media_objects, "engine", self.database),
                        patch.object(media_manager, "engine", self.database), patch.object(media_manager, "MEDIA_ROOT", self.media),
                        patch.object(state, "database", self.database), patch.object(state, "node", {"role": "Standalone", "node_id": "1" * 32}),
                        patch.object(api.admin_service, "require_admin", AsyncMock(return_value="a" * 64)),
                        patch.object(api.admin_service, "audit", AsyncMock()),
                        patch.object(crypto, "creation_admission", crypto.CreationAdmission())]
        for item in self.patches:
            item.start()
        await crypto.initialize()
        app = FastAPI()
        app.include_router(api.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),base_url="http://localhost")
        self.private = ec.generate_private_key(ec.SECP256R1())

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()
        crypto.manager = None
        await self.database.dispose()
        self.temp.cleanup()

    async def register(self, path, *, encrypted=True, plaintext=b"ID3 browser encrypted sample"):
        (self.media / path).parent.mkdir(parents=True, exist_ok=True)
        meta = crypto.Metadata.new(len(plaintext)) if encrypted else None
        data = AESGCM(crypto.manager.file_key(meta)).encrypt(meta.chunk_nonce(0), plaintext, meta.chunk_aad(0)) if meta else plaintext
        (self.media / path).write_bytes(data)
        kind = crypto.managed_path(path)[1]
        async with self.database.begin() as conn:
            identifier = await media_objects.ensure_object(conn, path, kind)
            await crypto.put_encryption(conn, identifier, meta)
        return identifier, meta, data

    async def session(self, *, admin=False):
        url = "/api/v1/media/admin/crypto/session" if admin else "/api/v1/media/crypto/session"
        response = await self.client.post(url, json={"public_key": browser_public(self.private)})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("no-store", response.headers["cache-control"])
        return response.json()

    async def test_http_ecdh_prepare_descriptor_token_and_upload_mode(self):
        grant = await self.session(admin=True)
        response = await self.client.post("/api/v1/media/admin/crypto/prepare", json={"session_id": grant["session_id"],"plaintext_size": 123})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        meta = crypto.Metadata.from_dict(body["encryption"])
        wrapping = browser_wrapping(self.private, grant)
        envelope = body["key_envelope"]
        key = AESGCM(wrapping).decrypt(base64.b64decode(envelope["iv"]),base64.b64decode(envelope["wrapped_key"]),f"frontiercloud:key-envelope:v1:{grant['session_id']}:{meta.file_id}".encode())
        self.assertEqual(key, crypto.manager.file_key(meta))
        self.assertEqual(crypto.upload_encryption("encrypted",body["encryption"],body["preparation_token"],"admin:" + "a" * 64),meta)
        with self.assertRaises(crypto.SessionError):
            crypto.upload_encryption("encrypted",body["encryption"],body["preparation_token"],"admin:other")
        response = await self.client.post("/api/v1/media/admin/crypto/prepare",json={"session_id":grant["session_id"],"plaintext_size":True})
        self.assertEqual(response.status_code,422)

    async def test_key_rechecks_hidden_identity_role_and_session_binding(self):
        identifier, meta, _ = await self.register("music/artist/song.mp3")
        grant = await self.session()
        body = {"session_id":grant["session_id"],"file_path":"music/artist/song.mp3"}
        response = await self.client.post("/api/v1/media/crypto/key",json=body)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["encryption"]["file_id"],meta.file_id)
        self.client.cookies.set(api.COOKIE_NAME,"f" * 64, domain="localhost.local", path="/")
        response = await self.client.post("/api/v1/media/crypto/key",json=body)
        self.assertEqual(response.status_code,401,response.text)
        grant = await self.session()
        body["session_id"] = grant["session_id"]
        async with self.database.begin() as conn:
            await conn.execute(text("INSERT INTO media_visibility(relative_path,hidden) VALUES('music/artist',1)"))
        response = await self.client.post("/api/v1/media/crypto/key",json=body)
        self.assertEqual(response.status_code,404,response.text)
        admin_grant = await self.session(admin=True)
        response = await self.client.post("/api/v1/media/admin/crypto/key",json={**body,"session_id":admin_grant["session_id"]})
        self.assertEqual(response.status_code,200,response.text)
        async with self.database.begin() as conn:
            await conn.execute(update(fs.identity).values(role="Follower"))
        self.assertIsNotNone(crypto.manager)
        response = await self.client.post("/api/v1/media/admin/crypto/key",json={**body,"session_id":admin_grant["session_id"]})
        self.assertEqual(response.status_code,409,response.text)

    async def test_origin_transport_oversize_and_administrator_auth_fail_closed(self):
        response = await self.client.post("/api/v1/media/crypto/session",json={"public_key":browser_public(self.private)},headers={"Origin":"https://attacker.invalid"})
        self.assertEqual(response.status_code,403)
        response = await self.client.post("/api/v1/media/crypto/session",json={"public_key":browser_public(self.private)},headers={"Sec-Fetch-Site":"cross-site"})
        self.assertEqual(response.status_code,403)
        response = await self.client.post("/api/v1/media/crypto/session",json={"public_key":"x" * 9000})
        self.assertEqual(response.status_code,413)
        self.assertIn("no-store",response.headers["cache-control"])
        with patch.object(api, "secure_admin_transport",return_value=False):
            response = await self.client.post("/api/v1/media/crypto/session",json={"public_key":browser_public(self.private)})
        self.assertEqual(response.status_code,426)
        with patch.object(api.admin_service,"require_admin",AsyncMock(side_effect=HTTPException(401,"Expired"))):
            response = await self.client.post("/api/v1/media/admin/crypto/session",json={"public_key":browser_public(self.private)})
        self.assertEqual(response.status_code,401)

    async def test_lyric_range_head_and_visibility_source_rechecks(self):
        track_id, _, _ = await self.register("music/artist/song.mp3")
        lyric_id, descriptor, ciphertext = await self.register("lyrics/artist/song.lrc",plaintext=b"[00:01.00]browser only lyrics\n")
        async with self.database.begin() as conn:
            await conn.execute(text("INSERT INTO media_lyric_links(media_id,media_path,lyric_id,lyric_path) VALUES(:id,:track,:lyric_id,:lyric)"),
                {"id":track_id,"track":"music/artist/song.mp3","lyric_id":lyric_id,"lyric":"lyrics/artist/song.lrc"})
        url = "/api/v1/media/crypto/bytes?file_path=lyrics/artist/song.lrc"
        response = await self.client.get(url,headers={"Range":"bytes=3-11"})
        self.assertEqual(response.status_code,206,response.text)
        self.assertEqual(response.content,ciphertext[3:12])
        self.assertEqual(response.headers["content-range"],f"bytes 3-11/{len(ciphertext)}")
        response = await self.client.head(url,headers={"Range":"bytes=-5"})
        self.assertEqual(response.status_code,206)
        self.assertEqual(response.content,b"")
        self.assertEqual(response.headers["content-length"],"5")
        response = await self.client.get(url,headers={"Range":"bytes=999-"})
        self.assertEqual(response.status_code,416)
        async with self.database.begin() as conn:
            await conn.execute(text("INSERT INTO media_visibility(relative_path,hidden) VALUES('music/artist/song.mp3',1)"))
        response = await self.client.get(url)
        self.assertEqual(response.status_code,404,response.text)
        with self.assertRaises(HTTPException):
            await crypto.encrypted_lyric("music/artist/song.mp3")

    async def test_mixed_download_snapshot_replacement_and_no_server_decryption(self):
        encrypted_id, meta, data = await self.register("music/artist/encrypted.mp3")
        plain_id, _, plain = await self.register("music/artist/plain.mp3",encrypted=False)
        query = {"paths":json.dumps(["music/artist"])}
        with patch.object(AESGCM,"decrypt",side_effect=AssertionError("Server decrypted media")):
            response = await self.client.get("/api/v1/media/admin/download/plan",params=query)
            self.assertEqual(response.status_code,200,response.text)
            items = {item["path"]:item for item in response.json()["items"]}
            self.assertEqual(len(items),2)
            plain_item = items["music/artist/plain.mp3"]
            self.assertIn("snapshot=",plain_item["url"])
            response = await self.client.get(plain_item["url"],headers={"Range":"bytes=0-2"})
            self.assertEqual(response.status_code,206,response.text)
            self.assertEqual(response.content,b"ID3")
            (self.media / "music/artist/plain.mp3").write_bytes(b"ID3 replacement with same admin authority")
            response = await self.client.get(plain_item["url"])
            self.assertEqual(response.status_code,409,response.text)
            response = await self.client.get("/api/v1/media/admin/crypto/bytes?file_path=music/artist/encrypted.mp3",headers={"Range":"bytes=0-5"})
            self.assertEqual(response.status_code,206,response.text)
            self.assertEqual(response.content,data[:6])
        self.assertEqual(api.admin_service.audit.await_count,1)

    async def test_descriptor_tombstone_restore_and_unique_nonce_identity(self):
        identifier, meta, _ = await self.register("music/artist/song.mp3")
        async with self.database.begin() as conn:
            await crypto.put_encryption(conn,identifier,meta)
            await crypto.forget_encryption(conn,identifier)
        async with self.database.begin() as conn:
            self.assertIsNone(await crypto.encryption_for_id(conn,identifier))
            with self.assertRaises(crypto.MetadataError):
                await crypto.put_encryption(conn,identifier,meta)
            with self.assertRaises(crypto.MetadataError):
                await crypto.put_encryption(conn,"b" * 64,meta)
            await crypto.put_encryption(conn,identifier,meta,restore=True)
            self.assertEqual(await crypto.encryption_for_id(conn,identifier),meta)
        async with self.database.begin() as conn:
            await conn.execute(update(encryption).values(descriptor_json="{}"))
        with self.assertRaises(crypto.MetadataError):
            await crypto.browser_download_plan(["music/artist"])

    async def test_premaster_database_identity_restore_failure_and_restart(self):
        first = crypto.manager.key_id()
        await crypto.initialize()
        self.assertEqual(crypto.manager.key_id(),first)
        async with self.database.begin() as conn:
            await conn.execute(update(keys).values(key_id="0" * 64))
        with self.assertRaises(crypto.PremasterError):
            await crypto.initialize()
        async with self.database.begin() as conn:
            await conn.execute(update(keys).values(key_id=first))
        (self.root / "media-keys" / "media-premaster.key").unlink()
        with self.assertRaises(crypto.PremasterError):
            await crypto.initialize()

    async def test_plaintext_key_conflict_and_oversize_selection_policy(self):
        await self.register("music/artist/one.mp3",encrypted=False)
        await self.register("music/artist/two.mp3",encrypted=False)
        grant = await self.session()
        response = await self.client.post("/api/v1/media/crypto/key",json={"session_id":grant["session_id"],"file_path":"music/artist/one.mp3"})
        self.assertEqual(response.status_code,409)
        self.assertEqual(await crypto.browser_download_plan(["music/artist"],limit=1),[])
        await self.register("music/artist/encrypted.mp3")
        with self.assertRaises(HTTPException) as context:
            await crypto.browser_download_plan(["music/artist"],limit=1)
        self.assertEqual(context.exception.status_code,413)

    async def test_hidden_plaintext_source_preserves_existing_lyric_fallback(self):
        await self.register("music/artist/plain.mp3",encrypted=False)
        self.assertIsNone(await crypto.encrypted_lyric("music/artist/plain.mp3"))
        async with self.database.begin() as conn:
            await conn.execute(text("INSERT INTO media_visibility(relative_path,hidden) VALUES('music/artist',1)"))
        self.assertIsNone(await crypto.encrypted_lyric("music/artist/plain.mp3"))


if __name__ == "__main__":
    unittest.main()
