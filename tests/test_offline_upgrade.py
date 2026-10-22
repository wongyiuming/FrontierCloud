"""Cold inspection preserves original values and never initializes missing keys."""
import hashlib
from dataclasses import replace
import hmac
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import db,schema_migrations
from app.core.sqlite_compat import create_sqlite_engine
from app.services import offline_upgrade as offline
from app.services.federation.state import State


class OfflineDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.secrets = self.root / 'secrets'
        self.keys = self.root / 'media-keys'
        self.secrets.mkdir()
        self.keys.mkdir()
        self.vault = Fernet.generate_key()
        self.premaster = os.urandom(32)
        for path, value in ((self.secrets/'node-vault.key', self.vault), (self.keys/'media-premaster.key', self.premaster)):
            path.write_bytes(value)
            path.chmod(0o600)
        self.path = self.root/'existing.db'
        self.engine = create_sqlite_engine('sqlite+aiosqlite:///' + self.path.as_posix())
        self.addAsyncCleanup(self.engine.dispose)
        with patch.object(db, 'engine', self.engine), patch.object(db, 'SECRET_DIR', self.root/'unused'):
            await db.init_db()
        await State(self.engine, self.vault).initialize()
        async with self.engine.begin() as conn:
            await conn.execute(text('INSERT INTO media_crypto_keys(singleton,key_id,created_at) VALUES (1,:key,42)'),
                {'key': hmac.digest(self.premaster, b'frontiercloud:premaster-id:v1', 'sha256').hex()})
            await conn.exec_driver_sql('CREATE TABLE `original:custom` (`id` INTEGER, `value:original` BLOB, `name` TEXT)')
            await conn.exec_driver_sql('INSERT INTO `original:custom` VALUES (1,?,?),(2,?,?)', (b'\x00\xff', 'same', b'\x00\xff', 'same'))
        self.readonly = create_async_engine('sqlite+aiosqlite:///' + self.path.as_uri() + '?mode=ro&uri=true')
        self.addAsyncCleanup(self.readonly.dispose)

    async def scan(self, baseline=None):
        return await offline.inspect_database(self.readonly, self.secrets, self.keys, baseline=baseline)

    async def test_readonly_hashes_all_original_tables_with_types_and_duplicates(self):
        before = self.path.read_bytes()
        baseline = await self.scan()
        self.assertEqual(baseline['tables']['original:custom']['rows'], 2)
        self.assertNotIn('private_key', baseline)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(await self.scan(baseline), baseline)
        async with self.readonly.connect() as conn:
            with self.assertRaises(Exception):
                await conn.exec_driver_sql('DELETE FROM node_identity')

    async def test_live_preflight_skips_table_fingerprints_and_runtime_exclusive_lease(self):
        from types import SimpleNamespace
        from app.services import runtime_gate
        from app.core import config
        options = SimpleNamespace(command='preflight', node_id=None)
        settings = config.settings
        # The helper process has its own unused writer pool. Disposing the live
        # fixture's pool would checkpoint its WAL and invalidate a byte proof.
        helper_engine = create_sqlite_engine(self.engine.url)
        self.addAsyncCleanup(helper_engine.dispose)
        with (patch.object(runtime_gate, 'DATA_ROOT', self.root), patch.object(config, 'SECRET_DIR', self.secrets),
              patch.object(db, 'engine', helper_engine), patch.object(settings, 'SQLITE_PATH', str(self.path)),
              patch.object(settings, 'DEPLOYMENT_MODE', ''), patch.object(settings, 'TLS_ENABLED', False),
              patch.object(offline, 'fingerprint', side_effect=AssertionError('live preflight scanned whole tables'))):
            with runtime_gate.acquire():
                before = self.path.read_bytes()
                result = await offline.execute(options)
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual((result['phase'], result['readonly'], result['tables']), ('preflight_passed', True, {}))

    async def test_live_fenced_verification_preserves_original_baseline_with_runtime_lease_held(self):
        from types import SimpleNamespace
        from app.services import runtime_gate
        from app.core import config
        import json
        baseline = await self.scan()
        path = self.root/'baseline.json'
        raw = json.dumps(baseline).encode()
        path.write_bytes(raw);path.chmod(0o600)
        marker = self.root/'.frontiercloud-native-maintenance'
        marker.write_bytes(b'owned test fence');marker.chmod(0o600)
        options = SimpleNamespace(command='verify',node_id=baseline['node_id'],baseline=str(path),
            baseline_sha256=hashlib.sha256(raw).hexdigest(),fence_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())
        helper = create_sqlite_engine(self.engine.url)
        self.addAsyncCleanup(helper.dispose)
        with (patch.object(runtime_gate,'DATA_ROOT',self.root),patch.object(config,'SECRET_DIR',self.secrets),
              patch.object(db,'engine',helper),patch.object(config.settings,'SQLITE_PATH',str(self.path)),
              patch.object(config.settings,'DEPLOYMENT_MODE',''),patch.object(config.settings,'TLS_ENABLED',False)):
            with runtime_gate.acquire():
                result = await offline.execute(options)
        self.assertEqual(result['phase'],'fenced_verified')
        self.assertTrue(result['original_state_preserved'])
        self.assertFalse(result['business_opened'])
        self.assertEqual(await self.scan(),baseline)

    async def test_partial_payload_and_quarantine_are_observed_not_deleted(self):
        media = self.root/'media'
        media.mkdir()
        item = media/'.upload-incomplete.part'
        item.write_bytes(b'owner temporary bytes')
        with self.assertRaisesRegex(offline.PreparationError, 'Partial media'):
            offline.pending_files(self.root)
        self.assertEqual(item.read_bytes(), b'owner temporary bytes')
        item.unlink()
        hidden = media/'.private'
        hidden.mkdir()
        hidden_part = hidden/'.upload-hidden.part'
        hidden_part.write_bytes(b'retain hidden recovery material')
        with self.assertRaisesRegex(offline.PreparationError, 'Partial media'):
            offline.pending_files(self.root)
        self.assertEqual(hidden_part.read_bytes(), b'retain hidden recovery material')
        hidden_part.unlink()
        quarantine = media/'.delete-owned-journal'
        quarantine.mkdir()
        with self.assertRaisesRegex(offline.PreparationError, 'quarantine'):
            offline.pending_files(self.root)
        self.assertTrue(quarantine.is_dir())

    async def test_original_value_or_extra_original_row_cannot_be_hidden(self):
        baseline = await self.scan()
        async with self.engine.begin() as conn:
            await conn.exec_driver_sql('UPDATE `original:custom` SET `value:original`=NULL WHERE id=1')
        with self.assertRaisesRegex(offline.PreparationError, 'Original database values'):
            await self.scan(baseline)

    async def test_changed_key_never_regenerates_or_adopts(self):
        before = (self.keys/'media-premaster.key').read_bytes()
        (self.keys/'media-premaster.key').write_bytes(os.urandom(32))
        with self.assertRaisesRegex(offline.PreparationError, 'premaster'):
            await self.scan()
        self.assertNotEqual((self.keys/'media-premaster.key').read_bytes(), before)
        (self.secrets/'node-vault.key').unlink()
        with self.assertRaises(FileNotFoundError):
            await self.scan()
        self.assertFalse((self.secrets/'node-vault.key').exists())

    async def test_unfinished_delete_is_a_gate_not_automatic_cleanup(self):
        async with self.engine.begin() as conn:
            await conn.exec_driver_sql("INSERT INTO media_delete_operations VALUES ('pending-op','pending','[]','2026-10-20')")
        with self.assertRaisesRegex(offline.PreparationError, 'media_delete_operations'):
            await self.scan()
        async with self.engine.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT COUNT(*) FROM media_delete_operations')), 1)

    async def test_unknown_aborted_upload_is_not_idle_with_or_without_s3_schema(self):
        from sqlalchemy import insert
        from app.services.federation import schema as fs
        from app.services.s3_schema import metadata as s3
        async with self.engine.begin() as conn:
            await conn.execute(insert(fs.upload_sessions).values(upload_id='a'*32,storage_member_id='b'*32,media_id='c'*64,
                media_path='music/album/unknown.mp3',path_locator=None,object_kind='audio',expected_bytes=31,
                state='aborted',created_at=1000,updated_at=1000,expires_at=2800))
        for with_s3 in (True,False):
            if not with_s3:
                async with self.engine.begin() as conn:
                    await conn.run_sync(s3.drop_all)
            with self.subTest(with_s3=with_s3),self.assertRaises(offline.PreparationError) as failure:
                await self.scan()
            self.assertEqual(failure.exception.code,'unfinished_transactions')
            self.assertIn('cluster_upload_sessions',str(failure.exception))
            async with self.engine.connect() as conn:
                self.assertEqual(await conn.scalar(text("SELECT COUNT(*) FROM cluster_upload_sessions WHERE state='aborted'")),1)

    async def test_opened_identity_observation_allows_new_work_without_relaxing_upgrade_preflight(self):
        before = await offline.inspect_database(self.readonly,self.secrets,self.keys,include_tables=False)
        async with self.engine.begin() as conn:
            await conn.exec_driver_sql("INSERT INTO media_delete_operations VALUES ('new-post-open','pending','[]','2026-10-20')")
        after = await offline.inspect_database(self.readonly,self.secrets,self.keys,include_tables=False,require_idle=False)
        self.assertEqual(after,before)
        with self.assertRaisesRegex(offline.PreparationError,'media_delete_operations'):
            await offline.inspect_database(self.readonly,self.secrets,self.keys,include_tables=False)

    async def test_synthetic_generation4_to_current_preserves_old_projection_and_retries(self):
        # The native gate separately bootstraps the actual published old source.
        from app.services.resource_migration_schema import metadata as migration
        from app.services.s3_schema import metadata as s3
        from app.services.node_retirement_schema import metadata as retirement
        async with self.engine.begin() as conn:
            await conn.run_sync(retirement.drop_all)
            await conn.run_sync(s3.drop_all)
            await conn.run_sync(migration.drop_all)
            await conn.exec_driver_sql('UPDATE frontiercloud_schema SET generation=4')
        baseline = await self.scan()
        self.assertEqual(baseline['generation'], 4)
        with patch.object(db, 'engine', self.engine):
            await db.init_db()
            after = await self.scan(baseline)
            await db.init_db()
        self.assertEqual(after['generation'], schema_migrations.CURRENT_SCHEMA_GENERATION)
        self.assertEqual(after['tables'], baseline['tables'])
        self.assertEqual(await self.scan(baseline), after)

    async def test_interrupted_forward_migration_keeps_baseline_and_retries(self):
        from app.core import schema_migrations as migrations
        from app.services.resource_migration_schema import metadata as migration
        from app.services.s3_schema import metadata as s3
        from app.services.node_retirement_schema import metadata as retirement
        async with self.engine.begin() as conn:
            await conn.run_sync(retirement.drop_all)
            await conn.run_sync(s3.drop_all)
            await conn.run_sync(migration.drop_all)
            await conn.exec_driver_sql('UPDATE frontiercloud_schema SET generation=4')
        baseline = await self.scan()
        async def interrupted(conn):
            await conn.run_sync(lambda sync: s3.tables['s3_storage_members'].create(sync, checkfirst=True))
            raise RuntimeError('injected schema interruption')
        altered = {**migrations.MIGRATIONS, 6: replace(migrations.MIGRATIONS[6], apply=interrupted)}
        with patch.object(db, 'engine', self.engine), patch.object(migrations, 'MIGRATIONS', altered):
            with self.assertRaisesRegex(RuntimeError, 'generation 5 -> 6') as caught:
                await db.init_db()
        self.assertEqual(str(caught.exception.__cause__), 'injected schema interruption')
        intermediate = await self.scan(baseline)
        self.assertEqual(intermediate['generation'], 5)
        with patch.object(db, 'engine', self.engine):
            await db.init_db()
        after = await self.scan(baseline)
        self.assertEqual(after['generation'], schema_migrations.CURRENT_SCHEMA_GENERATION)
        self.assertEqual(after['tables'], baseline['tables'])

    async def test_retired_compute_lease_is_preserved_without_runtime_revival(self):
        from sqlalchemy import insert
        from app.services.federation.schema import worker_jobs
        async with self.engine.begin() as conn:
            await conn.execute(insert(worker_jobs).values(job_id='a'*32, idempotency_key='historical-only', job_type='legacy',
                media_id=None, member_id=None, payload={}, result={}, state='leased', lease_token_hash='b'*64,
                lease_expires_at=9999999999, attempts=7, created_at=42, updated_at=43))
        baseline = await self.scan()
        self.assertEqual(baseline['tables']['cluster_worker_jobs']['rows'], 1)
        with patch.object(db, 'engine', self.engine):
            await db.init_db()
        self.assertEqual(await self.scan(baseline), baseline)

    async def test_history_change_is_not_an_allowed_schema_change(self):
        baseline = await self.scan()
        async with self.engine.begin() as conn:
            await conn.exec_driver_sql("INSERT INTO frontiercloud_schema_migrations VALUES (2,'wrong','wrong',42)")
        with self.assertRaisesRegex(RuntimeError, 'generation=2'):
            await self.scan(baseline)

    async def test_dropped_original_column_is_refused(self):
        baseline = await self.scan()
        async with self.engine.begin() as conn:
            await conn.exec_driver_sql('ALTER TABLE `original:custom` DROP COLUMN `value:original`')
        with self.assertRaisesRegex(offline.PreparationError, 'column disappeared'):
            await self.scan(baseline)


class TypedValueTests(unittest.TestCase):
    def test_distinct_storage_types_have_distinct_hashes(self):
        values = [None, 1, True, 1.0, '1', b'1']
        self.assertEqual(len({offline.sha(offline.typed(value)) for value in values}), len(values))

    def test_wrapped_database_errors_keep_driver_classification_without_values(self):
        from sqlalchemy.exc import OperationalError
        for code, expected in ((1045, 'database_authentication_failed'), (1049, 'database_missing'), (2003, 'database_unreachable')):
            driver = OperationalError('SELECT sensitive_column', {'secret': 'DO_NOT_PRINT'}, Exception(code, 'password DO_NOT_PRINT'))
            wrapped = RuntimeError('schema migration failed')
            wrapped.__cause__ = driver
            result = offline.error_result(wrapped)
            self.assertEqual(result['error'], expected)
            self.assertIn(str(code), result['detail'])
            self.assertNotIn('DO_NOT_PRINT', str(result))

    def test_configuration_error_omits_raw_input_values(self):
        from app.core.config import Settings
        from pydantic import ValidationError
        with self.assertRaises(ValidationError) as caught:
            Settings(_env_file=None, MYSQL_USER={'secret': 'DO_NOT_PRINT'})
        result = offline.error_result(caught.exception)
        self.assertEqual(result['error'], 'configuration_invalid')
        self.assertIn('MYSQL_USER', result['detail'])
        self.assertNotIn('DO_NOT_PRINT', str(result))
