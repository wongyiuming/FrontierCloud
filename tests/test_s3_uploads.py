"""Actual SQL multipart recovery; provider faults never become file success."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import db
from app.core.sqlite_compat import create_sqlite_engine
from app.services import media_crypto, offline_upgrade, s3_schema as s
from app.services.media_crypto_schema import encryption as encryption_table
from app.services import resource_pool
from app.services.federation import schema as fs
from app.services.federation.state import State
from app.services.s3_control import Control, ControlError
from app.services.s3_members import Members, Conflict
from app.services.s3_signing import Credentials, Location, S3Error
from app.services.s3_uploads import Uploads, PART_BYTES


def digest(data):
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


class Provider:
    """Fault injector and independent remote byte store, never product I/O."""
    def __init__(self):
        self.uploads,self.objects = {},{}
        self.created = 0
        self.create_lost = self.complete_lost = self.abort_race = False
        self.head_change = None
        self.delete_lost = False

    def control(self,signer):
        provider = self
        class Client:
            async def close(self):
                pass

            async def create(self,key,session,object_id):
                provider.created += 1
                upload_id = 'upload-'+str(provider.created)
                provider.uploads[upload_id] = dict(key=key,session_id=session,object_id=object_id,parts={})
                if provider.create_lost:
                    raise ControlError('timeout',uncertain=True)
                return upload_id

            async def uploads(self,key):
                return [name for name,value in provider.uploads.items() if value['key'] == key]

            async def parts(self,key,upload_id,*,after=0,limit=1000):
                upload = provider.uploads.get(upload_id)
                if not upload or upload['key'] != key:
                    raise ControlError('upload_missing',status=404)
                parts = [dict(number=number,bytes=len(data),etag='"'+hashlib.md5(data).hexdigest()+'"',checksum=digest(data))
                    for number,data in sorted(upload['parts'].items()) if number > after]
                selected = parts[:limit]
                return dict(parts=selected,complete=len(parts) <= limit,next=selected[-1]['number'] if selected else after)

            async def complete(self,key,upload_id,parts):
                upload = provider.uploads.get(upload_id)
                if upload is None:
                    raise ControlError('upload_missing',status=404)
                if key in provider.objects:
                    raise ControlError('conflict',status=412,uncertain=True)
                actual = []
                for part in parts:
                    data = upload['parts'][part['number']]
                    if digest(data) != part['checksum'] or part['etag'] != '"'+hashlib.md5(data).hexdigest()+'"':
                        raise ControlError('part_invalid',uncertain=True)
                    actual.append(data)
                composite = digest(b''.join(hashlib.sha256(data).digest() for data in actual))+'-'+str(len(actual))
                provider.objects[key] = dict(bytes=sum(map(len,actual)),checksum=composite,session_id=upload['session_id'],
                    object_id=upload['object_id'],etag='"completed"',version_id='',checksum_type='COMPOSITE')
                del provider.uploads[upload_id]
                if provider.complete_lost:
                    raise ControlError('timeout',uncertain=True)
                return dict(checksum=composite,etag='"completed"',version_id='')

            async def head(self,key,*,version_id=''):
                if key not in provider.objects:
                    raise ControlError('not_found',status=404)
                result = dict(provider.objects[key])
                if provider.head_change:
                    result.update(provider.head_change)
                return result

            async def delete(self,key,*,version_id=''):
                provider.objects.pop(key,None)
                if provider.delete_lost:
                    raise ControlError('timeout',uncertain=True)

            async def abort(self,key,upload_id):
                if provider.abort_race:
                    provider.abort_race = False
                    return
                if upload_id not in provider.uploads:
                    raise ControlError('upload_missing',status=404)
                del provider.uploads[upload_id]
        return Client()


class S3UploadTests(unittest.IsolatedAsyncioTestCase):
    async def open_database(self):
        return create_sqlite_engine(URL.create('sqlite+aiosqlite',database=str(self.root/'state.db')))

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = await self.open_database()
        self.patches = [patch.object(db,'engine',self.database),patch.object(db,'SECRET_DIR',self.root/'secrets'),
            patch.object(resource_pool,'storage_write_lock',asyncio.Lock())]
        for item in self.patches:
            item.start()
        await db.init_db()
        self.vault_key=Fernet.generate_key()
        self.store = State(self.database,self.vault_key)
        await self.store.initialize()
        async with self.database.begin() as conn:
            await conn.execute(text("UPDATE node_identity SET role='Master'"))
        self.now = 1000
        self.members = Members(self.store,clock=lambda:self.now)
        self.member = await self.member_add('one',100*1024**2)
        self.provider = Provider()
        self.service = self.restarted()

    def restarted(self):
        return Uploads(self.store,clock=lambda:self.now,control_factory=self.provider.control,lease_root=self.root/'leases')

    async def member_add(self,prefix,quota):
        member = await self.members.add(prefix,Location('private-bucket','us-east-1',prefix=prefix),
            Credentials('fixtureAccess123','private-secret-not-for-audit'),quota,'admin')
        revision = await self.members.probed(member,1)
        await self.members.configure(member,revision,name=prefix,quota=quota,enabled=True,actor='admin')
        return member

    async def asyncTearDown(self):
        if hasattr(self,'offline_engine'):
            await self.offline_engine.dispose()
        await self.database.dispose()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def row(self,upload_id):
        async with self.database.connect() as conn:
            return dict((await conn.execute(select(s.uploads).where(s.uploads.c.session_id == upload_id))).mappings().one())

    async def capacity(self):
        async with self.database.connect() as conn:
            return (await conn.execute(select(s.members.c.used_bytes,s.members.c.reserved_bytes).where(s.members.c.member_id == self.member))).one()

    async def prepared(self,data=b'original bytes',meta=None,path='music/album/song.mp3'):
        reserved = await self.service.reserve(path,len(data),meta,actor='admin')
        upload_id = reserved['upload_id']
        await self.service.initialize(upload_id)
        for offset in range(0,len(data),PART_BYTES):
            number = offset//PART_BYTES+1
            chunk = data[offset:offset+PART_BYTES]
            await self.service.part_grant(upload_id,number,digest(chunk))
            row = await self.row(upload_id)
            self.provider.uploads[row['provider_upload_id']]['parts'][number] = chunk
            await self.service.acknowledge(upload_id,number)
        return upload_id

    async def offline_fixture(self):
        self.offline_secrets=self.root/'operator-secrets'
        self.offline_keys=self.root/'operator-media-keys'
        self.offline_secrets.mkdir(mode=0o700)
        self.offline_keys.mkdir(mode=0o700)
        vault=self.offline_secrets/'node-vault.key'
        vault.write_bytes(self.vault_key);vault.chmod(0o600)
        premaster=os.urandom(32)
        key=self.offline_keys/'media-premaster.key'
        key.write_bytes(premaster);key.chmod(0o600)
        async with self.database.begin() as conn:
            await conn.execute(text('INSERT INTO media_crypto_keys(singleton,key_id,created_at) VALUES(1,:id,42)'),
                {'id':hmac.digest(premaster,b'frontiercloud:premaster-id:v1','sha256').hex()})
        # The cold CLI owns an explicit read-only transaction. Do not reuse
        # the application's SQLite adapter, whose begin hook already owns one.
        if self.database.dialect.name=='sqlite':
            self.offline_engine=create_async_engine('sqlite+aiosqlite:///'+(self.root/'state.db').as_uri()+'?mode=ro&uri=true')
        else:
            self.offline_engine=create_async_engine(self.database.url,pool_size=1,max_overflow=0)

    async def offline_scan(self):
        return await offline_upgrade.inspect_database(self.offline_engine,self.offline_secrets,self.offline_keys,include_tables=False)

    async def offline_snapshot(self):
        async with self.database.connect() as conn:
            return [list((await conn.execute(select(table).order_by(*table.primary_key.columns))).tuples())
                for table in (s.members,s.uploads,s.parts,s.objects,fs.upload_sessions,fs.global_media,encryption_table)]

    async def settled_abort(self,*,encrypted=False,parts=False,path='music/upgrade/cancelled.mp3'):
        metadata=media_crypto.Metadata.new(31) if encrypted else None
        data=b'x'*(metadata.ciphertext_size if metadata else 31)
        if parts:
            upload_id=await self.prepared(data,metadata,path)
        else:
            upload_id=(await self.service.reserve(path,len(data),metadata,actor='admin'))['upload_id']
        result=await self.service.cancel(upload_id,actor='admin')
        if parts:
            self.assertEqual(result['state'],'aborting')
            self.now+=301
            result=await self.restarted().reconcile(upload_id)
        self.assertEqual(result['state'],'aborted')
        self.assertEqual(await self.capacity(),(0,0))
        return await self.row(upload_id),metadata

    async def test_settled_s3_abort_is_idle_for_offline_upgrade_with_retained_parts_and_reused_path(self):
        await self.offline_fixture()
        for encrypted,parts,name in ((False,False,'plain'),(True,True,'encrypted')):
            row,_=await self.settled_abort(encrypted=encrypted,parts=parts,path='music/upgrade/'+name+'.mp3')
            async with self.database.connect() as conn:
                self.assertEqual(await conn.scalar(select(func.count()).select_from(s.parts).where(s.parts.c.session_id==row['session_id'])),int(parts))
            before=await self.offline_snapshot()
            self.assertEqual((await self.offline_scan())['role'],'Master')
            self.assertEqual(await self.offline_snapshot(),before)
        # A released path may legitimately have a new immutable owner; only
        # bindings for the cancelled identity would contradict its settlement.
        replacement=await self.prepared(b'new current bytes',path='music/upgrade/encrypted.mp3')
        await self.service.complete(replacement,actor='admin')
        self.assertEqual((await self.offline_scan())['role'],'Master')

    async def test_settled_s3_abort_is_idle_after_normal_member_removal(self):
        await self.offline_fixture()
        await self.settled_abort(encrypted=True,parts=True)
        revision=await self.members.configure(self.member,3,name='one',quota=100*1024**2,enabled=False,actor='admin')
        await self.members.remove(self.member,revision,'admin')
        before=await self.offline_snapshot()
        self.assertEqual((await self.offline_scan())['role'],'Master')
        self.assertEqual(await self.offline_snapshot(),before)

    async def test_offline_upgrade_rejects_inconsistent_s3_abort_without_rewriting_any_rows(self):
        await self.offline_fixture()
        row,metadata=await self.settled_abort(encrypted=True,parts=True)
        shared_id=fs.upload_sessions.c.upload_id==row['session_id']
        s3_id=s.uploads.c.session_id==row['session_id']
        async with self.database.connect() as conn:
            shared=dict((await conn.execute(select(fs.upload_sessions).where(shared_id))).mappings().one())
            member=dict((await conn.execute(select(s.members).where(s.members.c.member_id==self.member))).mappings().one())
            part=dict((await conn.execute(select(s.parts).where(s.parts.c.session_id==row['session_id']))).mappings().one())
        future=int(offline_upgrade.clock.time())+301
        changes=[(fs.upload_sessions,shared_id,field,value,shared[field]) for field,value in
            (('storage_member_id','f'*32),('media_id','f'*64),('media_path','music/other/changed.mp3'),
             ('expected_bytes',row['expected_bytes']+1),('path_locator',row['path_locator']))]
        changes += [(s.uploads,s3_id,field,value,row[field]) for field,value in
            (('master_id','f'*32),('state','uploading'),('error_code','cleanup_s3_timeout'),('grant_until',future))]
        changes += [(s.parts,s.parts.c.session_id==row['session_id'],'grant_until',future,part['grant_until']),
                    (s.members,s.members.c.member_id==self.member,'master_id','f'*32,member['master_id']),
                    (s.members,s.members.c.member_id==self.member,'reserved_bytes',1,0),
                    (encryption_table,encryption_table.c.object_id==row['media_id'],'descriptor_json',
                     json.dumps(metadata.to_dict()),None)]
        for table,where,field,value,original in changes:
            with self.subTest(table=table.name,field=field):
                async with self.database.begin() as conn:
                    await conn.execute(update(table).where(where).values({field:value}))
                before=await self.offline_snapshot()
                with self.assertRaises(offline_upgrade.PreparationError) as failure:
                    await self.offline_scan()
                self.assertEqual(failure.exception.code,'unfinished_transactions')
                self.assertEqual(await self.offline_snapshot(),before)
                async with self.database.begin() as conn:
                    await conn.execute(update(table).where(where).values({field:original}))
        from sqlalchemy import delete,insert
        for table,where,values in ((s.objects,s.objects.c.media_id==row['media_id'],dict(media_id=row['media_id'],member_id=self.member,
            object_id=row['object_id'],session_id=row['session_id'],object_key=row['object_key'],size_bytes=row['expected_bytes'],
            checksum='retained',etag='"retained"',version_id='',state='active',created_at=1000,updated_at=1000)),
            (fs.global_media,fs.global_media.c.media_id==row['media_id'],dict(media_id=row['media_id'],storage_member_id=self.member,
             object_id=row['object_id'],media_path=row['media_path'],path_locator=row['path_locator'],object_kind='audio',
             size_bytes=row['expected_bytes'],etag='"retained"',state='active',created_at=1000,updated_at=1000))):
            with self.subTest(binding=table.name):
                async with self.database.begin() as conn:
                    await conn.execute(insert(table).values(**values))
                before=await self.offline_snapshot()
                with self.assertRaises(offline_upgrade.PreparationError) as failure:
                    await self.offline_scan()
                self.assertEqual(failure.exception.code,'unfinished_transactions')
                self.assertEqual(await self.offline_snapshot(),before)
                async with self.database.begin() as conn:
                    await conn.execute(delete(table).where(where))
        async with self.database.begin() as conn:
            await conn.execute(delete(s.uploads).where(s3_id))
        before=await self.offline_snapshot()
        with self.assertRaises(offline_upgrade.PreparationError) as failure:
            await self.offline_scan()
        self.assertEqual(failure.exception.code,'unfinished_transactions')
        self.assertEqual(await self.offline_snapshot(),before)

    async def test_direct_parts_publish_once_without_product_payload_or_key_access(self):
        data = b'a'*PART_BYTES+b'last part'
        upload_id = await self.prepared(data)
        result = await self.service.complete(upload_id,actor='admin')
        self.assertEqual((result['state'],result['confirmed_bytes']),('published',len(data)))
        self.assertEqual(await self.capacity(),(len(data),0))
        await self.restarted().complete(upload_id,actor='admin')
        async with self.database.connect() as conn:
            self.assertEqual(await conn.scalar(select(func.count()).select_from(fs.global_media)),1)
            self.assertEqual(await conn.scalar(select(func.count()).select_from(s.objects)),1)
        with self.assertRaisesRegex(Conflict,'published_object_cannot_abort'):
            await self.service.cancel(upload_id,actor='admin')
        self.assertEqual(await self.capacity(),(len(data),0))

    async def test_expired_credentials_reject_new_placement_without_reserving_capacity(self):
        await self.members.configure(self.member,3,name='one',quota=100*1024**2,enabled=False,
            credentials=Credentials('fixtureAccess123','private-secret-not-for-audit',expires_at=self.now+45),actor='admin')
        revision=await self.members.probed(self.member,4)
        await self.members.configure(self.member,revision,name='one',quota=100*1024**2,enabled=True,actor='admin')
        self.now+=16
        with self.assertRaisesRegex(S3Error,'credentials_expired'):
            await self.service.reserve('music/expired/song.mp3',20,actor='admin')
        self.assertEqual(await self.capacity(),(0,0))
        row=(await self.members.list())[0]
        self.assertEqual((row['credential_state'],row['placement_ready']),('expired',False))

    async def test_expiry_before_initialize_keeps_never_sent_marker_and_cancels_without_provider(self):
        await self.members.configure(self.member,3,name='one',quota=100*1024**2,enabled=False,
            credentials=Credentials('fixtureAccess123','private-secret-not-for-audit',expires_at=self.now+45),actor='admin')
        revision=await self.members.probed(self.member,4)
        await self.members.configure(self.member,revision,name='one',quota=100*1024**2,enabled=True,actor='admin')
        reserved=await self.service.reserve('music/expired/song.mp3',20,actor='admin')
        self.now+=16
        with self.assertRaisesRegex(S3Error,'credentials_expired'):
            await self.service.initialize(reserved['upload_id'])
        self.assertEqual((await self.row(reserved['upload_id']))['state'],'reserved')
        self.assertEqual(self.provider.created,0)
        self.assertEqual((await self.service.cancel(reserved['upload_id'],actor='admin'))['state'],'aborted')
        self.assertEqual(await self.capacity(),(0,0))

    async def test_lost_completion_ack_recovers_original_encryption_and_exactly_once_quota(self):
        meta = media_crypto.Metadata.new(31)
        data = b'opaque ciphertext only'.ljust(meta.ciphertext_size,b'!')
        upload_id = await self.prepared(data,meta)
        self.provider.complete_lost = True
        with self.assertRaisesRegex(ControlError,'timeout'):
            await self.service.complete(upload_id,actor='admin')
        self.assertEqual((await self.row(upload_id))['state'],'uncertain')
        self.assertEqual(await self.capacity(),(0,len(data)))
        result = await self.restarted().reconcile(upload_id)
        self.assertEqual(result['state'],'published')
        await self.restarted().reconcile(upload_id)
        async with self.database.connect() as conn:
            self.assertEqual(await media_crypto.encryption_for_id(conn,result['media_id']),meta)
        self.assertEqual(await self.capacity(),(len(data),0))

    async def test_lost_create_ack_adopts_exact_upload_without_duplicate_creation(self):
        result = await self.service.reserve('music/album/song.mp3',20,actor='admin')
        self.provider.create_lost = True
        with self.assertRaisesRegex(ControlError,'timeout'):
            await self.service.initialize(result['upload_id'])
        result = await self.restarted().reconcile(result['upload_id'])
        self.assertEqual(result['state'],'uploading')
        self.assertEqual(self.provider.created,1)
        self.assertEqual(await self.capacity(),(0,20))

    async def test_unknown_create_never_releases_capacity_or_recreates_upload(self):
        result = await self.service.reserve('music/album/song.mp3',20,actor='admin')
        self.provider.create_lost = True
        with self.assertRaises(ControlError):
            await self.service.initialize(result['upload_id'])
        self.provider.uploads.clear()
        with self.assertRaisesRegex(Conflict,'creation_unresolved'):
            await self.restarted().reconcile(result['upload_id'])
        self.assertEqual(self.provider.created,1)
        self.assertEqual(await self.capacity(),(0,20))
        revision = await self.members.configure(self.member,3,name='one',quota=100*1024**2,enabled=False,actor='admin')
        with self.assertRaisesRegex(Conflict,'upload_unreconciled'):
            await self.members.remove(self.member,revision,'admin')

    def rejecting_create(self,signer):
        # Exercise the actual control client's classification; no upload ID or
        # remote object was created by this definitive permission refusal.
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'<Error><Code>AccessDenied</Code></Error>'
        def request(request):
            return httpx.Response(403,stream=Body())
        return Control(signer,transport=httpx.MockTransport(request))

    async def test_rejected_create_survives_restart_and_cancellation_releases_exact_resources(self):
        metadata=media_crypto.Metadata.new(31)
        result=await self.service.reserve('music/album/rejected.mp3',metadata.ciphertext_size,metadata,actor='admin')
        upload_id=result['upload_id']
        self.service.control_factory=self.rejecting_create
        with self.assertRaisesRegex(ControlError,'access_denied'):
            await self.service.initialize(upload_id)
        row=await self.row(upload_id)
        self.assertEqual((row['state'],row['provider_upload_id'],row['grant_until']),('reserved','',0))
        self.assertEqual(await self.capacity(),(0,metadata.ciphertext_size))
        async with self.database.connect() as conn:
            self.assertEqual(await media_crypto.encryption_for_id(conn,row['media_id']),metadata)
            self.assertEqual(await conn.scalar(select(fs.upload_sessions.c.state).where(fs.upload_sessions.c.upload_id==upload_id)),'reserved')
        await self.restarted().cancel(upload_id,actor='admin')
        self.assertEqual(await self.capacity(),(0,0))
        async with self.database.connect() as conn:
            self.assertIsNone(await media_crypto.encryption_for_id(conn,row['media_id']))
            shared=(await conn.execute(select(fs.upload_sessions).where(fs.upload_sessions.c.upload_id==upload_id))).mappings().one()
            self.assertEqual((shared['state'],shared['path_locator']),('aborted',None))
        replacement=await self.restarted().reserve('music/album/rejected.mp3',metadata.ciphertext_size,actor='admin')
        self.assertNotEqual(replacement['upload_id'],upload_id)
        self.assertFalse(self.provider.uploads)
        self.assertFalse(self.provider.objects)

    async def test_rejected_create_expires_at_original_deadline_without_manual_cancellation(self):
        result=await self.service.reserve('music/album/rejected.mp3',20,actor='admin')
        self.service.control_factory=self.rejecting_create
        with self.assertRaisesRegex(ControlError,'access_denied'):
            await self.service.initialize(result['upload_id'])
        row=await self.row(result['upload_id'])
        self.assertEqual(row['expires_at'],result['expires_at'])
        self.now=row['expires_at']-1
        self.assertEqual((await self.restarted().reconcile(result['upload_id']))['state'],'reserved')
        self.assertEqual(await self.capacity(),(0,20))
        self.now+=1
        self.assertEqual((await self.restarted().reconcile(result['upload_id']))['state'],'aborted')
        self.assertEqual(await self.capacity(),(0,0))
        self.assertFalse(self.provider.uploads)
        await self.restarted().reserve('music/album/rejected.mp3',20,actor='admin')

    async def test_rejected_create_can_retry_same_immutable_session_after_permission_repair(self):
        result=await self.service.reserve('music/album/rejected.mp3',20,actor='admin')
        original=await self.row(result['upload_id'])
        self.service.control_factory=self.rejecting_create
        with self.assertRaisesRegex(ControlError,'access_denied'):
            await self.service.initialize(result['upload_id'])
        # Provider permissions are repaired out of band; credentials and the
        # immutable key/session stay the same and no duplicate reserve occurs.
        retry=await self.restarted().initialize(result['upload_id'])
        self.assertEqual(retry['state'],'uploading')
        row=await self.row(result['upload_id'])
        self.assertEqual((row['object_key'],row['media_id'],row['expected_bytes']),
                         (original['object_key'],original['media_id'],20))
        self.assertEqual(row['error_code'],'')
        self.assertEqual((self.provider.created,len(self.provider.uploads)),(1,1))
        self.assertEqual(await self.capacity(),(0,20))
        await self.restarted().initialize(result['upload_id'])
        self.assertEqual(self.provider.created,1)

    async def test_failed_rejection_record_keeps_outbound_marker_and_reservation(self):
        import app.services.s3_uploads as module
        result=await self.service.reserve('music/album/rejected.mp3',20,actor='admin')
        original_update=module.update
        updates=0
        def fail_rejection_write(table):
            nonlocal updates
            updates+=1
            if updates==2:
                raise RuntimeError('rejection write failed')
            return original_update(table)
        self.service.control_factory=self.rejecting_create
        with patch.object(module,'update',side_effect=fail_rejection_write),self.assertRaisesRegex(RuntimeError,'rejection write failed'):
            await self.service.initialize(result['upload_id'])
        self.assertEqual((await self.row(result['upload_id']))['state'],'creating')
        self.assertEqual(await self.capacity(),(0,20))
        with self.assertRaisesRegex(Conflict,'creation_unresolved'):
            await self.restarted().reconcile(result['upload_id'])
        self.assertEqual(await self.capacity(),(0,20))

    async def test_expiry_between_preflight_and_signing_keeps_safe_reserved_cancellation(self):
        await self.members.configure(self.member,3,name='one',quota=100*1024**2,enabled=False,
            credentials=Credentials('fixtureAccess123','private-secret-not-for-audit',expires_at=self.now+45),actor='admin')
        revision=await self.members.probed(self.member,4)
        await self.members.configure(self.member,revision,name='one',quota=100*1024**2,enabled=True,actor='admin')
        metadata=media_crypto.Metadata.new(31)
        result=await self.service.reserve('music/album/rejected.mp3',metadata.ciphertext_size,metadata,actor='admin')
        calls=[]
        def after_preflight(signer):
            self.now+=16
            return Control(signer,transport=httpx.MockTransport(lambda request:calls.append(request)))
        self.service.control_factory=after_preflight
        with self.assertRaisesRegex(ControlError,'credentials_expired'):
            await self.service.initialize(result['upload_id'])
        row=await self.row(result['upload_id'])
        self.assertEqual((row['state'],row['provider_upload_id'],row['grant_until']),('reserved','',0))
        self.assertEqual(calls,[])
        self.assertEqual(await self.capacity(),(0,metadata.ciphertext_size))
        await self.restarted().cancel(result['upload_id'],actor='admin')
        self.assertEqual(await self.capacity(),(0,0))
        async with self.database.connect() as conn:
            self.assertIsNone(await media_crypto.encryption_for_id(conn,row['media_id']))
            shared=(await conn.execute(select(fs.upload_sessions).where(fs.upload_sessions.c.upload_id==result['upload_id']))).mappings().one()
            self.assertEqual((shared['state'],shared['path_locator']),('aborted',None))

    async def test_part_content_is_immutable_and_outstanding_grants_are_bounded(self):
        result = await self.service.reserve('vido/album/movie.mp4',3*PART_BYTES,actor='admin')
        upload_id = result['upload_id']
        await self.service.initialize(upload_id)
        grant = await self.service.part_grant(upload_id,1,digest(b'a'))
        self.assertIn('x-amz-checksum-sha256',grant.headers)
        self.assertEqual(grant.headers['content-length'],str(PART_BYTES))
        self.assertNotIn('private-secret',str(grant))
        with self.assertRaisesRegex(Conflict,'content_conflict'):
            await self.service.part_grant(upload_id,1,digest(b'b'))
        await self.service.part_grant(upload_id,2,digest(b'c'))
        with self.assertRaisesRegex(Conflict,'concurrency_limit'):
            await self.service.part_grant(upload_id,3,digest(b'd'))
        self.assertEqual((await self.service.status(upload_id))['confirmed_bytes'],0)
        with self.assertRaisesRegex(Conflict,'part_not_verified'):
            await self.service.acknowledge(upload_id,1)

    async def test_cancel_waits_grants_and_retries_abort_race_before_releasing_quota(self):
        upload_id = await self.prepared()
        result = await self.service.cancel(upload_id,actor='admin')
        self.assertEqual(result['state'],'aborting')
        self.assertEqual(await self.capacity(),(0,14))
        with self.assertRaisesRegex(Conflict,'state_conflict'):
            await self.service.part_grant(upload_id,1,digest(b'original bytes'))
        self.now += 301
        self.provider.abort_race = True
        with self.assertRaisesRegex(Conflict,'abort_pending'):
            await self.restarted().reconcile(upload_id)
        self.assertEqual(await self.capacity(),(0,14))
        self.assertEqual((await self.restarted().reconcile(upload_id))['state'],'aborted')
        await self.service.cancel(upload_id,actor='admin')
        self.assertEqual(await self.capacity(),(0,0))
        self.assertFalse(self.provider.uploads)

    async def test_never_started_cancel_is_local_and_consumes_encryption_file_id(self):
        meta = media_crypto.Metadata.new(20)
        reserved = await self.service.reserve('music/album/song.mp3',meta.ciphertext_size,meta,actor='admin')
        self.assertEqual((await self.service.cancel(reserved['upload_id'],actor='admin'))['state'],'aborted')
        self.assertEqual(self.provider.created,0)
        self.assertEqual(await self.capacity(),(0,0))
        with self.assertRaisesRegex(media_crypto.MetadataError,'retired|consumed'):
            await self.service.reserve('music/album/song.mp3',meta.ciphertext_size,meta,actor='admin')
        self.assertEqual(await self.capacity(),(0,0))

    async def test_wrong_provider_metadata_never_publishes_or_becomes_cleanup_target(self):
        upload_id = await self.prepared()
        self.provider.head_change = dict(session_id='f'*32)
        with self.assertRaisesRegex(ControlError,'publication_identity'):
            await self.service.complete(upload_id,actor='admin')
        with self.assertRaisesRegex(ControlError,'publication_identity'):
            await self.restarted().reconcile(upload_id)
        self.now += 301
        with self.assertRaisesRegex(ControlError,'publication_identity'):
            await self.service.cancel(upload_id,actor='admin')
        self.assertTrue(self.provider.objects)
        self.assertEqual(await self.capacity(),(0,14))

    async def test_sql_failure_after_real_provider_completion_is_recoverable(self):
        upload_id = await self.prepared()
        original = self.store.log
        async def fail_publish(conn,action,*args,**kwargs):
            if action == 's3_upload_published':
                raise RuntimeError('audit commit failure')
            return await original(conn,action,*args,**kwargs)
        with patch.object(self.store,'log',side_effect=fail_publish):
            with self.assertRaisesRegex(RuntimeError,'audit commit failure'):
                await self.service.complete(upload_id,actor='admin')
        self.assertEqual(await self.capacity(),(0,14))
        self.assertEqual((await self.restarted().reconcile(upload_id))['state'],'published')
        self.assertEqual(await self.capacity(),(14,0))

    async def test_parallel_same_path_has_one_reservation_and_folder_capacity_does_not_scatter(self):
        results = await asyncio.gather(*[self.service.reserve('music/album/song.mp3',20,actor='admin') for _ in range(2)],return_exceptions=True)
        self.assertEqual(sum(isinstance(value,Conflict) for value in results),1)
        await self.member_add('two',200)
        async with self.database.begin() as conn:
            await conn.execute(update(s.members).where(s.members.c.member_id == self.member).values(quota_bytes=100))
        with self.assertRaisesRegex(Conflict,'parent_unavailable'):
            await self.service.reserve('music/album/next.mp3',81,actor='admin')
        child = await self.service.reserve('music/other/disc/next.mp3',81,actor='admin')
        self.assertNotEqual((await self.row(child['upload_id']))['member_id'],self.member)

    async def test_browser_activity_renews_idle_deadline_without_claiming_received_bytes(self):
        result = await self.service.reserve('music/album/song.mp3',20,actor='admin')
        upload_id = result['upload_id']
        await self.service.initialize(upload_id)
        self.now += 1700
        status = await self.service.touch(upload_id)
        self.assertEqual(status['expires_at'],self.now+1800)
        self.assertEqual((status['confirmed_bytes'],status['grant_until']),(0,0))
        self.now += 1801
        with self.assertRaisesRegex(Conflict,'state_conflict'):
            await self.service.touch(upload_id)
        self.assertEqual((await self.service.reconcile(upload_id))['state'],'aborted')
        self.assertEqual(await self.capacity(),(0,0))

    async def test_recovery_credentials_only_after_disabled_cancel_and_old_grant_expiry(self):
        upload_id = await self.prepared()
        replacement = Credentials('replacement123','replacement-private-secret-value')
        with self.assertRaisesRegex(Conflict,'recovery_credentials_not_safe'):
            await self.members.recovery_credentials(self.member,3,replacement,'admin')
        await self.service.cancel(upload_id,actor='admin')
        revision = await self.members.configure(self.member,3,name='one',quota=100*1024**2,enabled=False,actor='admin')
        with self.assertRaisesRegex(Conflict,'recovery_credentials_not_safe'):
            await self.members.recovery_credentials(self.member,revision,replacement,'admin')
        self.now += 301
        await self.members.recovery_credentials(self.member,revision,replacement,'admin')
        self.assertEqual((await self.members.list())[0]['probe_state'],'unchecked')
        self.assertEqual((await self.service.reconcile(upload_id))['state'],'aborted')
        self.assertEqual(await self.capacity(),(0,0))

    async def test_mixed_category_layout_is_refused_before_provider_or_quota_change(self):
        await self.service.reserve('music/album/song.mp3',20,actor='admin')
        with self.assertRaisesRegex(Conflict,'category_layout_conflict'):
            await self.service.reserve('music/album/disc/song.mp3',30,actor='admin')
        self.assertEqual(await self.capacity(),(0,20))
        self.assertEqual(self.provider.created,0)

    async def test_background_recovery_closes_expired_never_started_upload(self):
        from app.services.s3_recovery import Recovery
        reserved = await self.service.reserve('music/album/song.mp3',20,actor='admin')
        self.now += 1801
        recovery = Recovery(self.store,service=self.service,clock=lambda:self.now)
        self.assertEqual(await recovery.once(),dict(processed=1,closed=1,pending=0))
        self.assertEqual((await self.service.status(reserved['upload_id']))['state'],'aborted')
        self.assertEqual(await self.capacity(),(0,0))

    async def test_ambiguous_creation_does_not_starve_later_expired_upload(self):
        from app.services.s3_recovery import Recovery
        first = await self.service.reserve('music/album/first.mp3',20,actor='admin')
        self.provider.create_lost = True
        with self.assertRaises(ControlError):
            await self.service.initialize(first['upload_id'])
        self.provider.uploads.clear()
        second = await self.service.reserve('music/album/second.mp3',30,actor='admin')
        async with self.database.begin() as conn:
            await conn.execute(update(s.uploads).where(s.uploads.c.session_id == first['upload_id']).values(updated_at=1))
            await conn.execute(update(s.uploads).where(s.uploads.c.session_id == second['upload_id']).values(updated_at=2))
        self.now += 1801
        recovery = Recovery(self.store,service=self.service,clock=lambda:self.now)
        self.assertEqual((await recovery.once(limit=1))['pending'],1)
        self.assertEqual((await self.row(first['upload_id']))['error_code'],'create_s3_creation_unresolved')
        self.assertEqual((await recovery.once(limit=1))['closed'],1)
        self.assertEqual(await self.capacity(),(0,20))

    async def test_background_recovery_skips_live_control_claim_without_resetting_state(self):
        from app.services.s3_recovery import Recovery
        from app.services.upload_claims import claim
        reserved = await self.service.reserve('music/album/song.mp3',20,actor='admin')
        async with self.database.begin() as conn:
            await conn.execute(update(s.uploads).values(state='creating'))
        with claim(reserved['upload_id'],root=self.root/'leases'):
            result = await Recovery(self.store,service=self.service,clock=lambda:self.now).once()
        self.assertEqual(result,dict(processed=1,closed=0,pending=1))
        self.assertEqual((await self.row(reserved['upload_id']))['state'],'creating')
        self.assertEqual(self.provider.created,0)

    async def test_expired_snapshot_cannot_abort_a_renewed_session(self):
        reserved = await self.service.reserve('music/album/song.mp3',20,actor='admin')
        upload_id = reserved['upload_id']
        await self.service.initialize(upload_id)
        snapshot = await self.row(upload_id)
        self.now += 1799
        await self.service.touch(upload_id)
        self.now += 2
        self.assertIsNone(await self.service._start_abort(snapshot,'system',expired=True))
        self.assertEqual((await self.row(upload_id))['state'],'uploading')

    async def test_unknown_completion_outcome_is_retained_until_late_output_can_be_removed(self):
        upload_id = await self.prepared()
        self.provider.complete_lost = True
        with self.assertRaises(ControlError):
            await self.service.complete(upload_id,actor='admin')
        object_key = (await self.row(upload_id))['object_key']
        delayed = self.provider.objects.pop(object_key)
        self.now += 301
        with self.assertRaisesRegex(Conflict,'completion_unresolved'):
            await self.service.cancel(upload_id,actor='admin')
        self.assertEqual(await self.capacity(),(0,14))
        self.provider.objects[object_key] = delayed
        self.assertEqual((await self.service.reconcile(upload_id))['state'],'aborted')
        self.assertEqual(await self.capacity(),(0,0))
        self.assertFalse(self.provider.objects)

    async def test_lost_delete_ack_uses_durable_verified_output_proof_after_restart(self):
        upload_id = await self.prepared()
        self.provider.complete_lost = True
        with self.assertRaises(ControlError):
            await self.service.complete(upload_id,actor='admin')
        self.now += 301
        self.provider.delete_lost = True
        with self.assertRaisesRegex(ControlError,'timeout'):
            await self.service.cancel(upload_id,actor='admin')
        self.assertEqual((await self.row(upload_id))['etag'],'"completed"')
        self.assertEqual(await self.capacity(),(0,14))
        self.assertEqual((await self.restarted().reconcile(upload_id))['state'],'aborted')
        self.assertEqual(await self.capacity(),(0,0))
