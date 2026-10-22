"""Real SQLite commit acknowledgement and crash recovery for browser ciphertext."""
import asyncio
import io
import os
from unittest.mock import patch
import unittest

from fastapi import UploadFile
from sqlalchemy import text

from app.services import media_crypto as crypto, media_manager as media
from app.services import media_publication_recovery as publication
from tests import test_encrypted_media_lifecycle as fixtures
from tests.test_lyric_publication import _LostAck


class MediaPublicationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.EncryptedMediaLifecycleTests.asyncSetUp
    asyncTearDown = fixtures.EncryptedMediaLifecycleTests.asyncTearDown
    encrypted_payload = fixtures.EncryptedMediaLifecycleTests.encrypted_payload

    async def upload(self, prepared, cipher):
        upload = UploadFile(filename='cipher.mp3', file=io.BytesIO(cipher))
        self.addAsyncCleanup(upload.close)
        return await media.MediaManager.upload_one(upload, self.media/'music/artist',
            encryption=crypto.Metadata.from_dict(prepared['encryption']))

    async def stored_descriptor(self):
        async with self.database.connect() as conn:
            identifier = await conn.scalar(text("SELECT media_id FROM media_objects WHERE media_path='music/artist/cipher.mp3'"))
            return await crypto.encryption_for_id(conn, identifier) if identifier else None

    async def test_real_committed_lost_ack_retains_ciphertext_and_recovers_proof(self):
        prepared, cipher, _ = await self.encrypted_payload()
        with patch.object(media, 'engine', _LostAck(self.database)):
            with self.assertRaises(publication.PublicationCommitUnknown): await self.upload(prepared, cipher)
        self.assertEqual((self.media/'music/artist/cipher.mp3').read_bytes(), cipher)
        self.assertIsNotNone(await self.stored_descriptor())
        self.assertTrue(publication.pending(self.media))
        journal = publication.pending(self.media)[0]
        original = journal.read_bytes()
        with patch('app.services.site_control.hard_fence_active', return_value=True):
            await media.recover_interrupted_media_deletions()
        self.assertEqual(journal.read_bytes(), original)
        self.assertEqual((self.media/'music/artist/cipher.mp3').read_bytes(), cipher)
        await media.recover_interrupted_media_deletions()
        self.assertEqual((self.media/'music/artist/cipher.mp3').read_bytes(), cipher)
        self.assertFalse(publication.pending(self.media)); media.ensure_media_mutations_ready()

    async def test_real_rolled_back_lost_ack_removes_only_original_fresh_file(self):
        prepared, cipher, _ = await self.encrypted_payload()
        with patch.object(media, 'engine', _LostAck(self.database, committed=False)):
            with self.assertRaises(publication.PublicationCommitUnknown): await self.upload(prepared, cipher)
        self.assertIsNone(await self.stored_descriptor())
        self.assertTrue((self.media/'music/artist/cipher.mp3').exists())
        await media.recover_interrupted_media_deletions()
        self.assertFalse((self.media/'music/artist/cipher.mp3').exists())
        self.assertFalse(publication.pending(self.media)); media.ensure_media_mutations_ready()

    async def test_cancel_waits_for_real_registration_then_keeps_committed_file(self):
        prepared, cipher, _ = await self.encrypted_payload()
        entered, release = asyncio.Event(), asyncio.Event()
        register = publication.register
        async def pause(*args):
            entered.set(); await release.wait(); return await register(*args)
        with patch.object(publication, 'register', pause):
            operation = asyncio.create_task(self.upload(prepared, cipher))
            await asyncio.wait_for(entered.wait(), 5)
            operation.cancel(); await asyncio.sleep(0); self.assertFalse(operation.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError): await operation
        self.assertEqual((self.media/'music/artist/cipher.mp3').read_bytes(), cipher)
        self.assertIsNotNone(await self.stored_descriptor()); self.assertFalse(publication.pending(self.media))

    async def test_crash_after_durable_commit_before_journal_clear_recovers_without_delete(self):
        prepared, cipher, _ = await self.encrypted_payload()
        with patch.object(publication, 'finish', side_effect=OSError('Injected interrupted journal cleanup')):
            with self.assertRaises(OSError): await self.upload(prepared, cipher)
        self.assertTrue(publication.pending(self.media))
        await media.recover_interrupted_media_deletions()
        self.assertEqual((self.media/'music/artist/cipher.mp3').read_bytes(), cipher)
        self.assertFalse(publication.pending(self.media))

    async def test_crash_before_registration_rolls_back_verified_cipher_inode(self):
        prepared, cipher, _ = await self.encrypted_payload()
        staged = self.media/'.upload-test.part'; staged.write_bytes(cipher)
        record = publication.create(self.media, 'music/artist/cipher.mp3', staged, cipher,
                                    crypto.Metadata.from_dict(prepared['encryption']))
        os.link(staged, self.media/'music/artist/cipher.mp3'); staged.unlink()
        await media.recover_interrupted_media_deletions()
        self.assertFalse((self.media/'music/artist/cipher.mp3').exists())
        self.assertFalse(publication.journal_path(self.media, record).exists())

    async def test_replaced_inode_even_with_equal_bytes_is_never_deleted(self):
        prepared, cipher, _ = await self.encrypted_payload()
        staged = self.media/'.upload-test.part'; staged.write_bytes(cipher)
        publication.create(self.media, 'music/artist/cipher.mp3', staged, cipher,
                           crypto.Metadata.from_dict(prepared['encryption']))
        destination = self.media/'music/artist/cipher.mp3'; destination.write_bytes(cipher)
        with self.assertRaises(RuntimeError): await media.recover_interrupted_media_deletions()
        self.assertEqual(destination.read_bytes(), cipher)
        self.assertTrue(publication.pending(self.media))

    async def test_plain_journal_preserves_bigint_sizes_without_changing_fresh_upload_limits(self):
        staged = self.media/'.upload-test.part'; payload = b'ID3 bounded media'; staged.write_bytes(payload)
        record = publication.create(self.media, 'music/artist/cipher.mp3', staged, payload, None)
        publication.finish(self.media, record)
        for size in (10 * 1024 ** 3 + 1, (1 << 63) - 1):
            self.assertEqual(publication.validate({**record, 'size': size})['size'], size)
        with self.assertRaises(RuntimeError): publication.validate({**record, 'size': 1 << 63})
        upload = UploadFile(filename='plain.mp3', file=io.BytesIO(payload))
        self.addAsyncCleanup(upload.close)
        with patch.object(media.settings, 'ADMIN_MAX_UPLOAD_FILE_SIZE', len(payload)-1):
            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as error:
                await media.MediaManager.upload_one(upload, self.media/'music/artist')
        self.assertEqual(error.exception.status_code, 413)
        self.assertFalse((self.media/'music/artist/plain.mp3').exists())
        self.assertFalse(publication.pending(self.media))
