"""Actual SQLite configuration, encrypted secrets and retained S3 state."""
import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import insert, select, text, update
from sqlalchemy.engine import URL

from app.core import db, schema_migrations
from app.core.sqlite_compat import create_sqlite_engine
from app.services import s3_schema as s
from app.services.federation.state import State
from app.services.s3_members import Conflict, Members, namespace
from app.services.s3_signing import Credentials, Location, S3Error


class S3NamespaceTests(unittest.TestCase):
    def test_aws_service_aliases_keep_the_same_partition_bucket_and_prefix(self):
        endpoints = [
            ('https://s3.amazonaws.com','us-east-1','aws'),
            ('https://s3-us-west-2.amazonaws.com','us-west-2','aws'),
            ('https://s3.dualstack.us-east-1.amazonaws.com','us-east-1','aws'),
            ('https://s3-fips.us-east-1.amazonaws.com','us-east-1','aws'),
            ('https://s3-fips.dualstack.us-west-2.amazonaws.com','us-west-2','aws'),
            ('https://s3-accelerate.amazonaws.com','us-east-1','aws'),
            ('https://s3-accelerate.dualstack.amazonaws.com','us-east-1','aws'),
            ('https://s3.dualstack.us-gov-west-1.amazonaws.com','us-gov-west-1','aws-us-gov'),
            ('https://s3-fips.us-gov-west-1.amazonaws.com','us-gov-west-1','aws-us-gov'),
            ('https://s3-fips.dualstack.us-gov-east-1.amazonaws.com','us-gov-east-1','aws-us-gov'),
            ('https://s3-fips-us-gov-west-1.amazonaws.com','us-gov-west-1','aws-us-gov'),
            ('https://s3.dualstack.cn-north-1.amazonaws.com.cn','cn-north-1','aws-cn'),
            ('https://s3.us-east-1.amazonaws.com','us-gov-west-1','aws'),
            ('https://s3.amazonaws.com','us-gov-west-1','aws'),
            ('https://s3.us-gov-west-1.amazonaws.com','us-east-1','aws-us-gov'),
        ]
        for endpoint,region,partition in endpoints:
            with self.subTest(endpoint=endpoint,region=region):
                location = Location('private-bucket',region,endpoint=endpoint,style='virtual',prefix='frontier')
                self.assertEqual(namespace(location),(partition,'private-bucket','frontier'))

    def test_custom_origins_and_aws_lookalikes_are_not_aws_namespaces(self):
        for endpoint in ('https://s3.example.test',
                'https://s3.dualstack.us-east-1.amazonaws.com.example.test',
                'https://s3-fips.us-east-1.example.test',
                'https://s3.dualstack.us-east-1.amazonaws.com:9443',
                'https://s3-accesspoint.us-east-1.amazonaws.com',
                'https://s3.dualstack.us-east-1.example.test'):
            with self.subTest(endpoint=endpoint):
                expected = (endpoint,'private-bucket','frontier')
                self.assertEqual(namespace(Location('private-bucket','us-east-1',endpoint=endpoint,
                    style='path',prefix='frontier')),expected)
                self.assertEqual(namespace(Location('private-bucket','us-gov-west-1',endpoint=endpoint,
                    style='virtual',prefix='frontier')),expected)


class S3MembersTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = create_sqlite_engine(URL.create('sqlite+aiosqlite',database=str(self.root/'state.db')))
        self.patches = [patch.object(db,'engine',self.database),patch.object(db,'SECRET_DIR',self.root/'secret')]
        for item in self.patches:
            item.start()
        await db.init_db()
        self.key = Fernet.generate_key()
        self.store = State(self.database,self.key)
        await self.store.initialize()
        async with self.database.begin() as conn:
            await conn.execute(text("UPDATE node_identity SET role='Master'"))
        self.now = 1000
        self.members = Members(self.store,clock=lambda:self.now)
        self.location = Location('private-bucket','us-east-1',prefix='frontier')
        self.credentials = Credentials('fixtureAccess123','private-secret-not-for-audit')
        self.member = await self.members.add('Archive',self.location,self.credentials,1024**3,'admin')

    async def asyncTearDown(self):
        await self.database.dispose()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def row(self):
        return (await self.members.list())[0]

    async def configure(self,expected,**changes):
        return await self.members.configure(self.member,expected,name='Archive',quota=1024**3,
            enabled=changes.pop('enabled',False),actor='admin',**changes)

    async def upload(self,state='uncertain'):
        async with self.database.begin() as conn:
            await conn.execute(insert(s.uploads).values(session_id='a'*32,member_id=self.member,
                master_id=self.store.node['node_id'],media_id='b'*64,object_id='c'*64,path_locator='d'*64,
                media_path='music/album/song.mp3',object_key='opaque',state=state,expected_bytes=123,
                part_bytes=8*1024**2,part_count=1,credential_revision=1,created_at=1000,updated_at=1000,expires_at=1300))

    async def test_credentials_are_encrypted_survive_restart_and_never_reach_public_status_or_audit(self):
        row = await self.row()
        self.assertEqual((row['enabled'],row['probe_state'],row['revision']),(False,'unchecked',1))
        self.assertIsNone(row['physical_free_bytes'])
        self.assertIsNone(row['traffic_bytes_per_second'])
        async with self.database.connect() as conn:
            stored = await conn.scalar(select(s.members.c.credentials_sealed))
            audit = await conn.scalar(text('SELECT detail FROM node_audit'))
        self.assertNotIn(self.credentials.secret_key,stored+str(row)+str(audit))
        self.assertNotIn(self.credentials.access_key,str(row)+str(audit))
        self.assertEqual(json.loads(self.store.unseal(stored))['secret_key'],self.credentials.secret_key)
        restarted = State(self.database,self.key)
        signer,rev = await Members(restarted,clock=lambda:self.now).signer(self.member)
        self.assertEqual((signer.credentials,rev),(self.credentials,1))
        with self.assertRaises(Exception):
            await Members(State(self.database,Fernet.generate_key())).signer(self.member)

    async def test_enabled_requires_probe_and_stale_probe_cannot_overwrite_edited_credentials(self):
        with self.assertRaisesRegex(Conflict,'probe_required'):
            await self.configure(1,enabled=True)
        rev = await self.members.probed(self.member,1)
        rev = await self.configure(rev,enabled=True)
        self.assertTrue((await self.row())['enabled'])
        rev = await self.configure(rev,credentials=Credentials('replacement123','another-private-secret-value'))
        self.assertEqual((await self.row())['probe_state'],'unchecked')
        with self.assertRaisesRegex(Conflict,'revision_conflict'):
            await self.members.probed(self.member,rev-1)
        with self.assertRaisesRegex(S3Error,'error_code_invalid'):
            await self.members.probed(self.member,rev,error_code='private secret URL')
        await self.members.probed(self.member,rev,error_code='s3_access_denied')
        self.assertEqual((await self.row())['error_code'],'s3_access_denied')

    async def test_namespace_prefix_overlap_and_aws_endpoint_style_alias_are_not_two_members(self):
        for location in (self.location,Location('private-bucket','us-west-2',prefix='frontier/child',style='path'),
                         Location('private-bucket','us-east-1',endpoint='https://s3.amazonaws.com',prefix='frontier'),
                         Location('private-bucket','us-east-1',prefix=''),
                         *[Location('private-bucket','us-east-1',endpoint=endpoint,style='virtual',prefix='frontier/child')
                           for endpoint in ('https://s3.dualstack.us-east-1.amazonaws.com',
                               'https://s3-fips.us-east-1.amazonaws.com','https://s3-fips.dualstack.us-east-1.amazonaws.com',
                               'https://s3-accelerate.amazonaws.com','https://s3-accelerate.dualstack.amazonaws.com')],
                         Location('private-bucket','us-gov-west-1',endpoint='https://s3.us-east-1.amazonaws.com',prefix='frontier')):
            with self.assertRaisesRegex(Conflict,'namespace_conflict'):
                await self.members.add('Other',location,self.credentials,1024,'admin')
        await self.members.add('Sibling',Location('private-bucket','us-east-1',prefix='frontiers'),self.credentials,1024,'admin')
        self.assertEqual(len(await self.members.list()),2)

    async def test_preexisting_alias_digest_cannot_bypass_standard_namespace_admission(self):
        location = Location('alias-bucket','us-west-2',endpoint='https://s3-fips.dualstack.us-west-2.amazonaws.com',
                            style='virtual',prefix='owned')
        member = await self.members.add('Existing alias',location,self.credentials,1024,'admin')
        # Simulate the persisted admission digest from before alias recognition.
        legacy = json.dumps((location.endpoint,location.bucket,location.prefix),separators=(',',':'))
        async with self.database.begin() as conn:
            await conn.execute(update(s.members).where(s.members.c.member_id==member).values(
                namespace_sha256=hashlib.sha256(legacy.encode()).hexdigest()))
            before = list((await conn.execute(select(s.members).order_by(s.members.c.member_id))).tuples())
        for prefix in ('','owned','owned/child'):
            with self.assertRaisesRegex(Conflict,'namespace_conflict'):
                await self.members.add('Conflicting',Location('alias-bucket','us-east-1',prefix=prefix),self.credentials,1024,'admin')
        async with self.database.connect() as conn:
            self.assertEqual(list((await conn.execute(select(s.members).order_by(s.members.c.member_id))).tuples()),before)

    async def test_partitions_and_custom_stores_keep_distinct_bucket_owners(self):
        for region in ('us-gov-west-1','cn-north-1'):
            await self.members.add('Separate partition',Location('private-bucket',region,prefix='frontier'),self.credentials,1024,'admin')
            suffix = 'amazonaws.com.cn' if region.startswith('cn-') else 'amazonaws.com'
            with self.assertRaisesRegex(Conflict,'namespace_conflict'):
                await self.members.add('Same partition',Location('private-bucket',region,
                    endpoint=f'https://s3.dualstack.{region}.{suffix}',prefix='frontier/child'),self.credentials,1024,'admin')
        endpoint = 'https://s3.dualstack.us-east-1.amazonaws.com.example.test'
        await self.members.add('Compatible store',Location('private-bucket','us-east-1',endpoint=endpoint,
            prefix='frontier'),self.credentials,1024,'admin')
        with self.assertRaisesRegex(Conflict,'namespace_conflict'):
            await self.members.add('Same compatible store',Location('private-bucket','us-east-1',endpoint=endpoint,
                style='virtual',prefix='frontier/child'),self.credentials,1024,'admin')
        self.assertEqual(len(await self.members.list()),4)

    async def test_reconfigure_to_overlapping_aws_alias_preserves_existing_configuration(self):
        other = await self.members.add('Other',Location('other-bucket','us-east-1'),self.credentials,1024,'admin')
        async with self.database.connect() as conn:
            before = (await conn.execute(select(s.members).where(s.members.c.member_id==other))).one()
        for endpoint in ('https://s3.dualstack.us-east-1.amazonaws.com',
                         'https://s3-fips.dualstack.us-east-1.amazonaws.com'):
            with self.assertRaisesRegex(Conflict,'namespace_conflict'):
                await self.members.configure(other,1,name='Other',quota=1024,enabled=False,actor='admin',
                    location=Location('private-bucket','us-east-1',endpoint=endpoint,style='virtual',prefix='frontier/child'))
            async with self.database.connect() as conn:
                self.assertEqual((await conn.execute(select(s.members).where(s.members.c.member_id==other))).one(),before)

    async def test_parallel_namespace_registration_has_only_one_winner(self):
        location = Location('parallel-bucket','us-east-1')
        results = await asyncio.gather(*[self.members.add('Parallel',location,self.credentials,1024,'admin') for _ in range(2)],return_exceptions=True)
        self.assertEqual(sum(isinstance(value,Conflict) for value in results),1)

    async def test_ambiguous_upload_blocks_credential_change_location_change_and_removal(self):
        await self.upload()
        for operation in (self.configure(1,credentials=Credentials('replacement123','another-private-secret-value')),
            self.configure(1,location=Location('other-bucket','us-east-1')),
            self.members.remove(self.member,1,'admin')):
            with self.assertRaisesRegex(Conflict,'upload_unreconciled'):
                await operation
        # Renaming or disabling a node is still possible without touching bytes.
        await self.configure(1)
        self.assertEqual((await self.row())['revision'],2)

    async def test_owned_objects_freeze_location_but_allow_new_probe_after_credential_rotation(self):
        await self.upload('published')
        async with self.database.begin() as conn:
            await conn.execute(insert(s.objects).values(media_id='b'*64,member_id=self.member,object_id='c'*64,
                session_id='a'*32,object_key='opaque',size_bytes=123,checksum='digest',etag='"etag"',
                version_id='',state='active',created_at=1000,updated_at=1000))
            await conn.execute(update(s.members).values(used_bytes=123))
        for operation in (self.configure(1,location=Location('other-bucket','us-east-1')),
                          self.members.remove(self.member,1,'admin')):
            with self.assertRaisesRegex(Conflict,'objects_owned'):
                await operation
        await self.configure(1,credentials=Credentials('replacement123','another-private-secret-value'))
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(s.objects.c.state)),'active')

    async def test_lower_quota_cannot_discard_reservations_or_used_bytes(self):
        async with self.database.begin() as conn:
            await conn.execute(update(s.members).values(used_bytes=100,reserved_bytes=50))
        with self.assertRaisesRegex(Conflict,'quota_below_committed'):
            await self.members.configure(self.member,1,name='Archive',quota=149,enabled=False,actor='admin')
        with self.assertRaisesRegex(Conflict,'capacity_unreconciled'):
            await self.members.remove(self.member,1,'admin')
        self.assertEqual((await self.row())['reserved_bytes'],50)

    async def test_member_revision_role_and_identity_are_not_taken_from_cached_state(self):
        with self.assertRaisesRegex(S3Error,'revision_invalid'):
            await self.configure(True)
        with self.assertRaisesRegex(Conflict,'revision_conflict'):
            await self.configure(2)
        async with self.database.begin() as conn:
            await conn.execute(text("UPDATE node_identity SET role='Follower'"))
        with self.assertRaisesRegex(S3Error,'master_required'):
            await self.members.list()

    async def test_generation_five_upgrade_adds_only_s3_tables_and_keeps_existing_values(self):
        async with self.database.begin() as conn:
            for table in reversed(s.metadata.sorted_tables):
                await conn.run_sync(table.drop)
            await conn.execute(text('UPDATE frontiercloud_schema SET generation=5'))
            before = list((await conn.execute(text('SELECT * FROM node_identity'))).tuples())
        await db.init_db()
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(text('SELECT generation FROM frontiercloud_schema')),schema_migrations.CURRENT_SCHEMA_GENERATION)
            self.assertEqual(list((await conn.execute(text('SELECT * FROM node_identity'))).tuples()),before)
            self.assertEqual(await conn.scalar(text('SELECT checksum FROM frontiercloud_schema_migrations WHERE generation=6')),
                schema_migrations.MIGRATIONS[6].checksum)
        await db.init_db()

    async def test_current_generation_missing_structure_is_refused_not_silently_recreated(self):
        async with self.database.begin() as conn:
            await conn.execute(text('DROP INDEX idx_s3_upload_recovery'))
        with self.assertRaisesRegex(RuntimeError,'S3 transaction index missing'):
            await db.init_db()

    async def test_identity_reset_refuses_registered_s3_configuration_without_discarding_secrets(self):
        from app.services.federation.protocol import ProtocolError
        with self.assertRaisesRegex(ProtocolError,'仍配置有 S3'):
            await self.store.reset('admin',self.store.node['node_id'])
        self.assertEqual(len(await self.members.list()),1)

    async def test_reset_after_explicit_member_removal_clears_cached_adapter_state(self):
        self.assertTrue(self.store._s3_configured)
        await self.members.remove(self.member,1,'admin')
        await self.store.reset('admin',self.store.node['node_id'])
        self.assertEqual(self.store.node['role'],'Standalone')
        self.assertFalse(self.store._s3_configured)

    async def test_follower_admission_never_hides_s3_configuration_as_empty_business_state(self):
        from app.services.business_lifecycle import ensure_follower_business_empty
        from app.services.federation.protocol import ProtocolError
        with self.assertRaisesRegex(ProtocolError,'仍有业务数据'):
            await ensure_follower_business_empty(self.database,media_root=self.root/'media',recordings_root=self.root/'recordings')
        self.assertEqual(len(await self.members.list()),1)

    async def test_pairing_cannot_reuse_s3_identity_and_s3_cannot_alias_a_pending_peer(self):
        from app.services.federation import protocol as p,schema as fs
        key=p.new_key()
        peer=dict(node_id=self.member,endpoint='https://peer.4399.com',public_key=p.public_key(key),app_version=p.APP_VERSION)
        with self.assertRaisesRegex(p.ProtocolError,'S3'):
            await self.store.prepare('a'*32,peer,'c'*64,'admin')
        peer['node_id']='b'*32
        await self.store.prepare('a'*32,peer,'c'*64,'admin')
        for blocked in (self.store.node['node_id'],peer['node_id']):
            with patch('app.services.s3_members.uuid.uuid4',return_value=SimpleNamespace(hex=blocked)):
                with self.assertRaisesRegex(Conflict,'member_identity_conflict'):
                    await self.members.add('Other',Location('other-bucket','us-east-1'),self.credentials,100,'admin')
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(fs.relationships.c.peer_id)),peer['node_id'])
        self.assertEqual(len(await self.members.list()),1)
