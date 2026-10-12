"""Real SQLite/audit/filesystem proofs for interruption-safe lyric publication."""
import asyncio
import io
import json
import os
from unittest.mock import patch
import unittest

from fastapi import HTTPException, UploadFile
from sqlalchemy import text
from starlette.requests import Request

from app.api.v1 import admin_upload_guard as api
from app.services import media_crypto as crypto, media_manager as media
from app.services import media_publication_recovery as publication
from tests import test_encrypted_media_lifecycle as fixtures


class _LostAck:
    def __init__(self, database, *, committed=True):
        self.database, self.committed = database, committed

    def begin(self):
        actual = self.database.begin()
        committed = self.committed
        class Transaction:
            async def __aenter__(self):
                return await actual.__aenter__()
            async def __aexit__(self, kind, error, traceback):
                if kind is not None:
                    return await actual.__aexit__(kind, error, traceback)
                if committed:
                    await actual.__aexit__(None, None, None)
                else:
                    error = RuntimeError('Injected definite rollback before lost acknowledgement')
                    await actual.__aexit__(type(error), error, None)
                raise OSError('Injected unavailable SQL commit acknowledgement')
        return Transaction()


class LyricPublicationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.EncryptedMediaLifecycleTests.asyncSetUp
    asyncTearDown = fixtures.EncryptedMediaLifecycleTests.asyncTearDown
    encrypted_payload = fixtures.EncryptedMediaLifecycleTests.encrypted_payload

    def request(self):
        return Request({'type': 'http', 'method': 'POST', 'path': '/api/v1/media/admin/upload/lyric',
            'headers': [], 'client': ('127.0.0.1', 12345), 'scheme': 'https', 'server': ('test', 443)})

    async def publish(self, prepared=None, cipher=None):
        if prepared is None:
            prepared, cipher, _ = await self.encrypted_payload(b'[00:01]private lyric\n')
        upload = UploadFile(filename='song.lrc', file=io.BytesIO(cipher))
        return await api.upload_lyric(self.request(), upload, storage_mode='encrypted',
            encryption=json.dumps(prepared['encryption']), preparation_token=prepared['preparation_token'],
            relative_path='artist/song.lrc', session_hash='a'*64)

    async def rows(self):
        async with self.database.connect() as conn:
            obj = (await conn.execute(text("SELECT media_id FROM media_objects WHERE media_path='lyrics/artist/song.lrc'"))).scalar()
            descriptor = await crypto.encryption_for_id(conn, obj) if obj else None
            audits = (await conn.execute(text("SELECT detail FROM admin_audit_log WHERE action='upload_lyric' AND result='success'"))).scalars().all()
        return obj, descriptor, audits

    async def test_publication_and_descriptor_block_shared_readers_until_commit(self):
        entered, release, observed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        register, resolve = api._register_lyric_publication, crypto._crypto_object
        async def pause(*args):
            entered.set(); await release.wait(); return await register(*args)
        async def watch(*args, **kwargs):
            observed.set(); return await resolve(*args, **kwargs)
        with patch.object(api, '_register_lyric_publication', pause), patch.object(crypto, '_crypto_object', watch):
            writer = asyncio.create_task(self.publish())
            await asyncio.wait_for(entered.wait(), 5)
            reader = asyncio.create_task(crypto.crypto_object('lyrics/artist/song.lrc', admin=True))
            await asyncio.sleep(0)
            self.assertFalse(observed.is_set())
            self.assertTrue(publication.pending(self.media))
            release.set(); await writer; obj = await reader
        self.assertIsNotNone(obj['encryption'])
        self.assertFalse(publication.pending(self.media))

    async def test_concurrent_delete_waits_for_registered_publication(self):
        entered, release, deleted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        register, collect = api._register_lyric_publication, media.MediaManager._collect
        async def pause(*args):
            entered.set(); await release.wait(); return await register(*args)
        async def watch(*args, **kwargs):
            deleted.set(); return await collect(*args, **kwargs)
        with patch.object(api, '_register_lyric_publication', pause), patch.object(media.MediaManager, '_collect', watch):
            writer = asyncio.create_task(self.publish())
            await asyncio.wait_for(entered.wait(), 5)
            deletion = asyncio.create_task(media.MediaManager.delete(['lyrics/artist/song.lrc']))
            await asyncio.sleep(0); self.assertFalse(deleted.is_set())
            release.set(); await writer; await deletion
        self.assertFalse((self.media/'lyrics/artist/song.lrc').exists())
        self.assertIsNone((await self.rows())[0])

    async def test_cancelled_request_waits_for_commit_and_preserves_ciphertext(self):
        entered, release = asyncio.Event(), asyncio.Event()
        register = api._register_lyric_publication
        async def pause(*args):
            entered.set(); await release.wait(); return await register(*args)
        with patch.object(api, '_register_lyric_publication', pause):
            writer = asyncio.create_task(self.publish())
            await asyncio.wait_for(entered.wait(), 5)
            writer.cancel(); await asyncio.sleep(0); self.assertFalse(writer.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError): await writer
        obj, descriptor, audits = await self.rows()
        self.assertIsNotNone(obj); self.assertIsNotNone(descriptor); self.assertEqual(len(audits), 1)
        self.assertIn('publication_id', json.loads(audits[0]))
        self.assertTrue((self.media/'lyrics/artist/song.lrc').is_file())
        self.assertFalse(publication.pending(self.media))

    async def test_audit_failure_rolls_back_file_object_and_descriptor(self):
        original = api.legacy_admin._mutation_audit
        def broken(*args):
            actual = original(*args)
            async def audit(conn, result, count, detail):
                if result == 'success': raise RuntimeError('Injected success-audit failure')
                return await actual(conn, result, count, detail)
            return audit
        with patch.object(api.legacy_admin, '_mutation_audit', broken):
            with self.assertRaises(HTTPException): await self.publish()
        self.assertEqual(await self.rows(), (None, None, []))
        self.assertFalse((self.media/'lyrics/artist/song.lrc').exists())
        self.assertFalse(publication.pending(self.media))

    async def test_cache_invalidation_failure_keeps_committed_lyric(self):
        async def unavailable():
            raise RuntimeError('Injected cache failure after durable commit')
        with patch.object(api, 'invalidate_media_catalog', unavailable):
            result = await self.publish()
        self.assertEqual(result['path'], 'lyrics/artist/song.lrc')
        self.assertTrue((self.media/'lyrics/artist/song.lrc').is_file())
        self.assertIsNotNone((await self.rows())[1])
        self.assertFalse(publication.pending(self.media))

    async def test_committed_lost_ack_is_verified_and_recovered_without_unlink(self):
        with patch.object(api, 'engine', _LostAck(self.database)):
            with self.assertRaises(HTTPException): await self.publish()
        before = (self.media/'lyrics/artist/song.lrc').read_bytes()
        with self.assertRaises(HTTPException): media.ensure_media_mutations_ready()
        self.assertEqual(len(publication.pending(self.media)), 1)
        await media.recover_interrupted_media_deletions()
        self.assertEqual((self.media/'lyrics/artist/song.lrc').read_bytes(), before)
        self.assertIsNotNone((await self.rows())[1])
        self.assertFalse(publication.pending(self.media)); media.ensure_media_mutations_ready()

    async def test_uncommitted_lost_ack_rolls_back_only_proven_fresh_inode(self):
        with patch.object(api, 'engine', _LostAck(self.database, committed=False)):
            with self.assertRaises(HTTPException): await self.publish()
        self.assertTrue((self.media/'lyrics/artist/song.lrc').is_file())
        self.assertEqual(await self.rows(), (None, None, []))
        await media.recover_interrupted_media_deletions()
        self.assertFalse((self.media/'lyrics/artist/song.lrc').exists())
        self.assertFalse(publication.pending(self.media)); media.ensure_media_mutations_ready()

    async def test_missing_success_audit_retains_partial_state_and_recovery_fence(self):
        with patch.object(api, 'engine', _LostAck(self.database)):
            with self.assertRaises(HTTPException): await self.publish()
        async with self.database.begin() as conn:
            await conn.execute(text("DELETE FROM admin_audit_log WHERE action='upload_lyric' AND result='success'"))
        with self.assertRaises(RuntimeError): await media.recover_interrupted_media_deletions()
        self.assertTrue((self.media/'lyrics/artist/song.lrc').is_file())
        self.assertTrue(publication.pending(self.media))

    async def test_crash_after_link_before_sql_is_recovered_by_fresh_inode_proof(self):
        staged = self.media/'.upload-review.part'; payload = b'[00:01]uncommitted lyric\n'; staged.write_bytes(payload)
        record = publication.create(self.media, 'lyrics/crash.lrc', staged, payload, None)
        os.link(staged, self.media/'lyrics/crash.lrc'); staged.unlink()
        await media.recover_interrupted_media_deletions()
        self.assertFalse((self.media/'lyrics/crash.lrc').exists())
        self.assertFalse(publication.journal_path(self.media, record).exists())
