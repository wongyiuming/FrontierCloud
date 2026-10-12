"""Real SQLite, upload leases and filesystem proofs for MasterLocal publication."""
import asyncio
import hashlib
import json
import os
from unittest.mock import patch
import unittest

from fastapi import HTTPException
from sqlalchemy import text
from starlette.requests import Request

from app.api.v1 import admin_cluster_integrity as cluster
from app.services import media_crypto as crypto, media_manager as media, resource_pool, upload_claims
from app.services import media_publication_recovery as publication
from app.services.federation.state import state
from tests import test_encrypted_media_lifecycle as fixtures
from tests.test_lyric_publication import _LostAck


class _LostMasterAck(_LostAck):
    def connect(self):
        return self.database.connect()


@unittest.skipIf(os.name == 'nt', 'Product upload leases require Linux kernel flock')
class MasterLocalPublicationTests(unittest.IsolatedAsyncioTestCase):
    encrypted_payload = fixtures.EncryptedMediaLifecycleTests.encrypted_payload

    async def asyncSetUp(self):
        await fixtures.EncryptedMediaLifecycleTests.asyncSetUp(self)
        lease = patch.object(upload_claims, 'ROOT', self.root / 'upload-leases')
        lease.start(); self.patches.append(lease)
        await state.promote('Master', 'https://master.4399.com', 'test', 2 * 1024 ** 3)
        self.path = 'music/NewCategory/Album/song.mp3'
        self.data = b'ID3 master local durable bytes' * 30
        self.reservation = None

    async def asyncTearDown(self):
        media._RECOVERY_REQUIRED_ROOTS.discard(self.media)
        await fixtures.EncryptedMediaLifecycleTests.asyncTearDown(self)

    async def reserve(self, *, encrypted=True):
        metadata = None
        if encrypted:
            prepared, self.data, _ = await self.encrypted_payload(self.data)
            metadata = crypto.Metadata.from_dict(prepared['encryption'])
        self.reservation = await cluster._reserve_upload(self.path, len(self.data), state.node['node_id'], metadata)
        return self.reservation

    def request(self, *, receive=None):
        sent = False
        async def body():
            nonlocal sent
            if sent:
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            sent = True
            return {'type': 'http.request', 'body': self.data, 'more_body': False}
        return Request({'type': 'http', 'method': 'PUT', 'path': '/api/v1/media/admin/upload/session/bytes',
            'headers': [], 'client': ('127.0.0.1', 12345), 'scheme': 'https',
            'server': ('master.4399.com', 443)}, receive=receive or body)

    async def upload(self, request=None):
        return await cluster.upload_session_bytes(self.reservation['upload_id'], request or self.request(), 'a' * 64)

    async def facts(self):
        async with self.database.connect() as conn:
            values = {'id': self.reservation['media_id'], 'upload': self.reservation['upload_id'], 'member': state.node['node_id']}
            row = (await conn.execute(text('SELECT state FROM cluster_upload_sessions WHERE upload_id=:upload'), values)).scalar()
            obj = (await conn.execute(text('SELECT media_id FROM media_objects WHERE media_id=:id'), values)).scalar()
            global_row = (await conn.execute(text('SELECT media_id FROM global_media_objects WHERE media_id=:id'), values)).scalar()
            descriptor = await crypto.encryption_for_id(conn, self.reservation['media_id'])
            quota = (await conn.execute(text('SELECT used_bytes,reserved_bytes FROM cluster_storage_members WHERE member_id=:member'), values)).one()
            audits = (await conn.execute(text("SELECT detail FROM admin_audit_log WHERE action='upload-finalized' AND result='success'"))).scalars().all()
        return {'state': row, 'object': obj, 'global': global_row,
            'descriptor': descriptor, 'quota': tuple(quota), 'audits': [json.loads(value) for value in audits]}

    async def lose_ack(self, *, committed=True):
        with patch.object(state, 'database', _LostMasterAck(self.database, committed=committed)):
            with self.assertRaises(HTTPException) as error:
                await self.upload()
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual((self.media / self.path).read_bytes(), self.data)
        self.assertEqual(len(publication.pending(self.media)), 1)
        with self.assertRaises(HTTPException) as fenced:
            media.ensure_media_mutations_ready()
        self.assertEqual(fenced.exception.status_code, 503)

    def assert_committed(self, facts):
        self.assertEqual(facts['state'], 'complete')
        self.assertEqual(facts['object'], self.reservation['media_id'])
        self.assertEqual(facts['global'], self.reservation['media_id'])
        self.assertEqual(facts['quota'], (len(self.data), 0))
        self.assertEqual(len(facts['audits']), 1)
        self.assertEqual(facts['audits'][0]['upload_id'], self.reservation['upload_id'])
        self.assertEqual(facts['audits'][0]['storage_member_id'], state.node['node_id'])
        self.assertEqual(len(facts['audits'][0]['publication_id']), 32)

    async def test_real_commit_then_ack_error_keeps_bytes_catalog_quota_and_audit(self):
        await self.reserve()
        await self.lose_ack()
        before = await self.facts(); self.assert_committed(before)
        self.assertIsNotNone(before['descriptor'])
        self.assertFalse(await cluster._cleanup_upload_session(self.reservation['upload_id'], allow_unindexed_local=True))
        await media.recover_interrupted_media_deletions()
        self.assertEqual((self.media / self.path).read_bytes(), self.data)
        self.assertFalse(publication.pending(self.media))
        self.assertEqual(await self.facts(), before)
        media.ensure_media_mutations_ready()
        await media.recover_interrupted_media_deletions()
        self.assertEqual(await self.facts(), before)

    async def test_real_rollback_then_ack_error_releases_only_uncommitted_reserved_object(self):
        await self.reserve()
        await self.lose_ack(committed=False)
        facts = await self.facts()
        self.assertEqual(facts['state'], 'reserved'); self.assertEqual(facts['quota'], (0, len(self.data)))
        self.assertIsNone(facts['object']); self.assertIsNone(facts['global']); self.assertFalse(facts['audits'])
        self.assertIsNotNone(facts['descriptor'])
        self.assertFalse(await cluster._cleanup_upload_session(self.reservation['upload_id'], expired_only=True))
        await media.recover_interrupted_media_deletions()
        self.assertFalse((self.media / self.path).exists()); self.assertFalse(publication.pending(self.media))
        facts = await self.facts()
        self.assertIsNone(facts['state']); self.assertIsNone(facts['descriptor']); self.assertEqual(facts['quota'], (0, 0))
        media.ensure_media_mutations_ready()

    async def test_success_audit_error_definitely_rolls_back_registration_and_releases_quota(self):
        await self.reserve()
        with patch.object(publication, 'write_audit', side_effect=RuntimeError('Injected transactional audit failure')):
            with self.assertRaises(HTTPException): await self.upload()
        facts = await self.facts()
        self.assertIsNone(facts['state']); self.assertIsNone(facts['object']); self.assertIsNone(facts['global'])
        self.assertIsNone(facts['descriptor']); self.assertEqual(facts['quota'], (0, 0)); self.assertFalse(facts['audits'])
        self.assertFalse((self.media / self.path).exists()); self.assertFalse(publication.pending(self.media))

    async def test_collision_never_deletes_an_unregistered_existing_file(self):
        await self.reserve(encrypted=False)
        target = self.media / self.path; target.parent.mkdir(parents=True)
        target.write_bytes(b'ID3 existing bytes outside this publication')
        with self.assertRaises(HTTPException): await self.upload()
        self.assertEqual(target.read_bytes(), b'ID3 existing bytes outside this publication')
        facts = await self.facts()
        self.assertEqual(facts['state'], 'reserved'); self.assertEqual(facts['quota'], (0, len(self.data)))
        self.assertFalse(publication.pending(self.media))

    async def test_rollback_proof_never_deletes_a_replacement_inode_with_equal_bytes(self):
        await self.reserve()
        await self.lose_ack(committed=False)
        target = self.media / self.path
        replacement = self.media / '.replacement'; replacement.write_bytes(self.data)
        os.replace(replacement, target)
        before = await self.facts()
        with self.assertRaisesRegex(RuntimeError, 'uncertain'):
            await media.recover_interrupted_media_deletions()
        self.assertFalse(await cluster._cleanup_upload_session(self.reservation['upload_id'], allow_unindexed_local=True))
        self.assertEqual(target.read_bytes(), self.data)
        self.assertEqual(await self.facts(), before); self.assertTrue(publication.pending(self.media))

    async def test_missing_commit_audit_and_unavailable_db_keep_unknown_bytes_and_fence(self):
        await self.reserve()
        await self.lose_ack()
        async with self.database.begin() as conn:
            await conn.execute(text("DELETE FROM admin_audit_log WHERE action='upload-finalized' AND result='success'"))
        with self.assertRaisesRegex(RuntimeError, 'uncertain'):
            await media.recover_interrupted_media_deletions()
        class Unavailable:
            def connect(self): raise RuntimeError('Database unavailable')
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            await publication.recover(self.media, Unavailable())
        before = await self.facts()
        self.assertFalse(await cluster._cleanup_upload_session(self.reservation['upload_id'], allow_unindexed_local=True))
        self.assertEqual(await self.facts(), before)
        self.assertEqual((self.media / self.path).read_bytes(), self.data)
        self.assertTrue(publication.pending(self.media))

    async def crash_publication(self, *, linked):
        await self.reserve()
        row = await resource_pool.upload_session(self.reservation['upload_id'], self.database)
        staged = self.media / ('.cluster-upload-' + row['upload_id'] + '.part')
        staged.write_bytes(self.data)
        with staged.open('rb') as stream: os.fsync(stream.fileno())
        async with self.database.connect() as conn:
            metadata = await crypto.encryption_for_id(conn, row['media_id'])
        journal = publication.create(self.media, self.path, staged, self.data, metadata, reservation=row)
        if linked:
            destination = self.media / self.path; destination.parent.mkdir(parents=True)
            os.link(staged, destination); publication.sync_hierarchy(self.media, destination.parent)
        await media.recover_interrupted_media_deletions()
        self.assertFalse(staged.exists()); self.assertFalse((self.media / self.path).exists())
        self.assertFalse(publication.journal_path(self.media, journal).exists())
        facts = await self.facts(); self.assertEqual(facts['quota'], (0, 0)); self.assertIsNone(facts['descriptor'])
        self.assertIsNone(facts['state']); self.assertFalse(facts['audits'])

    async def test_crash_before_physical_link_recovers_journal_and_releases_reservation(self):
        await self.crash_publication(linked=False)

    async def test_crash_after_link_before_registration_removes_only_known_fresh_inode(self):
        await self.crash_publication(linked=True)

    async def test_committed_crash_before_journal_clear_restores_without_double_quota(self):
        await self.reserve(encrypted=False)
        def crash_before_finish(root, record):
            staged = root / ('.cluster-upload-' + record['upload_id'] + '.part')
            self.assertFalse(staged.exists())
            self.assertEqual((root / self.path).read_bytes(), self.data)
            self.assertTrue(publication.journal_path(root, record).exists())
            raise OSError('Injected crash before journal clear')
        with patch.object(publication, 'finish', side_effect=crash_before_finish):
            with self.assertRaises(HTTPException): await self.upload()
        before = await self.facts(); self.assert_committed(before)
        self.assertTrue(publication.pending(self.media))
        await media.recover_interrupted_media_deletions()
        self.assertEqual((self.media / self.path).read_bytes(), self.data)
        self.assertEqual(await self.facts(), before); self.assertFalse(publication.pending(self.media))

    async def test_cancel_waits_for_real_finalize_and_preserves_committed_audit_and_bytes(self):
        await self.reserve()
        entered, release = asyncio.Event(), asyncio.Event()
        finalize = resource_pool.finalize_upload
        async def pause(*args, **kwargs):
            entered.set(); await release.wait(); return await finalize(*args, **kwargs)
        with patch.object(resource_pool, 'finalize_upload', pause):
            task = asyncio.create_task(self.upload())
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel(); await asyncio.sleep(0); task.cancel(); await asyncio.sleep(0)
            self.assertFalse(task.done())
            with self.assertRaises(upload_claims.UploadBusy):
                with upload_claims.claim(self.reservation['upload_id']): pass
            release.set()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assert_committed(await self.facts())
        self.assertEqual((self.media / self.path).read_bytes(), self.data)
        self.assertFalse(publication.pending(self.media))

    async def test_cancel_during_stream_removes_partial_and_releases_reserved_quota(self):
        await self.reserve(encrypted=False)
        entered, block = asyncio.Event(), asyncio.Event()
        received = False
        async def body():
            nonlocal received
            if not received:
                received = True
                return {'type': 'http.request', 'body': self.data[:20], 'more_body': True}
            entered.set(); await block.wait()
            return {'type': 'http.request', 'body': self.data[20:], 'more_body': False}
        task = asyncio.create_task(self.upload(self.request(receive=body)))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        facts = await self.facts(); self.assertEqual(facts['quota'], (0, 0)); self.assertIsNone(facts['state'])
        self.assertFalse((self.media / self.path).exists()); self.assertFalse(publication.pending(self.media))
        self.assertFalse((self.media / ('.cluster-upload-' + self.reservation['upload_id'] + '.part')).exists())

    async def test_private_journal_precedes_link_and_all_directory_levels_precede_sql_commit(self):
        await self.reserve(encrypted=False)
        synced, linked = [], []
        sync, link, finalize = publication.sync_directory, os.link, resource_pool.finalize_upload
        def observe_sync(directory):
            sync(directory); synced.append(directory)
        def observe_link(source, destination):
            journals = publication.pending(self.media)
            self.assertEqual(len(journals), 1)
            record = publication.load(journals[0])
            self.assertEqual(record['upload_id'], self.reservation['upload_id'])
            self.assertEqual(record['sha256'], hashlib.sha256(self.data).hexdigest())
            self.assertEqual(journals[0].stat().st_mode & 0o777, 0o600)
            link(source, destination); linked.append(destination)
        async def observe_finalize(*args, **kwargs):
            parent = (self.media / self.path).parent
            self.assertEqual(synced[-4:], [parent, parent.parent, self.media / 'music', self.media])
            self.assertEqual(linked, [self.media / self.path])
            return await finalize(*args, **kwargs)
        with patch.object(publication, 'sync_directory', observe_sync), patch.object(os, 'link', observe_link), patch.object(resource_pool, 'finalize_upload', observe_finalize):
            await self.upload()
        self.assert_committed(await self.facts()); self.assertFalse(publication.pending(self.media))


if __name__ == '__main__':
    unittest.main()
