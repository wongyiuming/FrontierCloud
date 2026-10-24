"""Actual verified S3 direct bytes, SQL publication and separate-process recovery.

This exercises the service layer; browser/Admin HTTP acceptance is separate.
All credentials, keys, SQLite files and owned objects are disposable fixture data.
"""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import time
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import httpx
from sqlalchemy import select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import db
from app.core.sqlite_compat import create_sqlite_engine
from app.services import media_crypto, s3_schema as s
from app.services.federation import schema as fs
from app.services.federation.state import State
from app.services.s3_control import Control, ControlError
from app.services.s3_members import Members
from app.services.s3_objects import Objects
from app.services.s3_recovery import Recovery
from app.services.s3_signing import Credentials, Location, Signer
from app.services.s3_uploads import Uploads, PART_BYTES


async def main():
    started = time.monotonic()
    fixture = Path(os.environ['S3_FIXTURE'])
    root = Path(os.environ['S3_TRANSACTION_ROOT'])
    if root.parent != fixture or not root.name.startswith('transactions-'):
        raise RuntimeError('invalid_disposable_transaction_root')
    root.mkdir(mode=0o700,exist_ok=True)
    context = ssl.create_default_context(cafile=str(fixture/'ca.pem'))
    wire = []
    class TrackedControl(Control):
        async def _request(self,method,key='',**kwargs):
            wire.append(dict(method=method,body_bytes=len(kwargs.get('body',b'')),response_kind=kwargs.get('root_name','')))
            return await super()._request(method,key,**kwargs)
    def factory(signer):
        return TrackedControl(signer,context=context)
    database = create_sqlite_engine(URL.create('sqlite+aiosqlite',database=str(root/'state.db')))
    secrets = json.loads((fixture/'credentials.json').read_text())['identities'][1]['credentials'][0]
    credentials = Credentials(secrets['accessKey'],secrets['secretKey'])
    bucket,endpoint = 'frontiercloud-python-acceptance','https://s3.4399.com:8333'
    recover = os.environ.get('S3_TRANSACTION_PHASE') == 'recover'
    key_file = root/'vault.key'
    if not recover:
        key_file.write_bytes(Fernet.generate_key())
        key_file.chmod(0o600)
    store = State(database,key_file.read_bytes())
    service = Uploads(store,control_factory=factory,lease_root=root/'leases')
    members = Members(store)
    checks = []
    def check(name,condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print(json.dumps(dict(passed=name)),file=sys.stderr,flush=True)
    try:
        with patch.object(db,'engine',database),patch.object(db,'SECRET_DIR',root/'secrets'):
            await db.init_db()
        if recover:
            pending = json.loads((root/'recovery.json').read_text())
            recovered = await Recovery(store,service=service).once()
            state = await service.status(pending['upload_id'])
            check('separate_process_recovers_completed_upload',state['state'] == 'published')
            check('background_recovery_selected_exact_transaction',recovered['closed'] == 1 and (await Recovery(store,service=service).once())['processed'] == 0)
            async with database.connect() as conn:
                shared = (await conn.execute(select(fs.global_media).where(fs.global_media.c.media_id == state['media_id']))).mappings().one()
                member = (await conn.execute(select(s.members).where(s.members.c.member_id == shared['storage_member_id']))).mappings().one()
                check('separate_process_exact_catalog_and_quota',shared['size_bytes'] == pending['bytes'] and
                    member['reserved_bytes'] == 0 and member['used_bytes'] == pending['expected_used'])
                check('separate_process_preserves_node_vault',bool(store.unseal(await conn.scalar(text('SELECT private_key FROM node_identity')))))
            return dict(phase='recover',checks=checks,wire=wire)
        await store.initialize()
        async with database.begin() as conn:
            await conn.execute(text("UPDATE node_identity SET role='Master'"))
        prefix = 'service-transactions/'+uuid.uuid4().hex
        async def add_member(name,quota,credential=credentials):
            member = await members.add(name,Location(bucket,'us-east-1',endpoint=endpoint,prefix=prefix+'/'+name),credential,quota,'native')
            signer,_ = await members.signer(member)
            control = factory(signer)
            try:
                for attempt in range(30):
                    try:
                        await control.head_bucket()
                        break
                    except ControlError:
                        if attempt == 29:
                            raise
                        await asyncio.sleep(1)
            finally:
                await control.close()
            revision = await members.probed(member,1)
            await members.configure(member,revision,name=name,quota=quota,enabled=True,actor='native')
            return member
        member_id = await add_member('main',1024**3)
        async def row(upload_id):
            async with database.connect() as conn:
                return dict((await conn.execute(select(s.uploads).where(s.uploads.c.session_id == upload_id))).mappings().one())
        async with httpx.AsyncClient(verify=context,trust_env=False,timeout=30,follow_redirects=False) as browser:
            class LostCreate(TrackedControl):
                async def create(self,*args,**kwargs):
                    await super().create(*args,**kwargs)
                    raise ControlError('timeout',uncertain=True)
            creation = await service.reserve('music/create/song.mp3',7,actor='native')
            try:
                await Uploads(store,control_factory=lambda signer:LostCreate(signer,context=context),lease_root=root/'leases').initialize(creation['upload_id'])
            except ControlError:
                pass
            else:
                raise AssertionError('missing_create_ack_fault')
            await Recovery(store,service=service).once()
            check('real_create_ack_lost_found_exact_multipart',(await service.status(creation['upload_id']))['state'] == 'uploading' and
                sum(item['response_kind'] == 'InitiateMultipartUploadResult' for item in wire) == 1)
            check('recovered_creation_abort_closed',(await service.cancel(creation['upload_id'],actor='native'))['state'] == 'aborted')
            class RejectedCreate(TrackedControl):
                async def create(self,*args,**kwargs):
                    # An actual provider refusal over ordinary verified HTTPS,
                    # not a fabricated control exception or SQL state change.
                    bad = TrackedControl(Signer(self.signer.location,
                        Credentials('INVALIDFIXTUREACCESS','invalid-fixture-secret')),context=context)
                    try:
                        return await bad.create(*args,**kwargs)
                    finally:
                        await bad.close()
            rejected = Uploads(store,control_factory=lambda signer:RejectedCreate(signer,context=context),lease_root=root/'leases')
            async def refuse_create(upload_id):
                try:
                    await rejected.initialize(upload_id)
                except ControlError as error:
                    check('real_create_rejection_classified_'+str(len(checks)),error.status == 403 and
                        error.code in ('s3_credentials_invalid','s3_signature_mismatch','s3_access_denied') and
                        getattr(error,'creation_rejected',False))
                else:
                    raise AssertionError('missing_real_provider_create_refusal')
                current = await row(upload_id)
                check('real_rejected_create_retains_retryable_session_'+str(len(checks)),current['state'] == 'reserved' and
                    not current['provider_upload_id'] and current['grant_until'] == 0 and not current['checksum'])
                return current
            refused_meta = media_crypto.Metadata.new(7)
            refused = await service.reserve('music/rejected/song.mp3',refused_meta.ciphertext_size,refused_meta,actor='native')
            refused_row = await refuse_create(refused['upload_id'])
            async with database.connect() as conn:
                check('real_rejected_create_keeps_exact_quota_and_descriptor',
                    await conn.scalar(select(s.members.c.reserved_bytes).where(s.members.c.member_id == member_id)) == refused_meta.ciphertext_size and
                    await media_crypto.encryption_for_id(conn,refused['media_id']) == refused_meta)
            restarted = Uploads(store,control_factory=factory,lease_root=root/'leases')
            check('real_rejected_create_cancels_after_service_restart',
                (await restarted.cancel(refused['upload_id'],actor='native'))['state'] == 'aborted')
            async with database.connect() as conn:
                shared = (await conn.execute(select(fs.upload_sessions).where(fs.upload_sessions.c.upload_id == refused['upload_id']))).mappings().one()
                check('real_rejected_create_releases_quota_path_and_descriptor',shared['state'] == 'aborted' and shared['path_locator'] is None and
                    await conn.scalar(select(s.members.c.reserved_bytes).where(s.members.c.member_id == member_id)) == 0 and
                    await media_crypto.encryption_for_id(conn,refused['media_id']) is None)
            signer,_ = await members.signer(member_id)
            control = factory(signer)
            try:
                check('real_rejected_create_has_no_remote_upload',await control.uploads(refused_row['object_key']) == [])
            finally:
                await control.close()
            retried = await service.reserve('music/rejected/song.mp3',7,actor='native')
            before_retry = await refuse_create(retried['upload_id'])
            await restarted.initialize(retried['upload_id'])
            after_retry = await row(retried['upload_id'])
            check('real_rejected_create_retries_same_identity',after_retry['state'] == 'uploading' and
                bool(after_retry['provider_upload_id']) and all(before_retry[field] == after_retry[field]
                    for field in ('session_id','media_id','object_id','object_key','media_path','expected_bytes','expires_at')))
            check('real_retried_create_aborts_normally',
                (await restarted.cancel(retried['upload_id'],actor='native'))['state'] == 'aborted')
            async def send(path,data,meta=None):
                reserved = await service.reserve(path,len(data),meta,actor='native')
                upload_id = reserved['upload_id']
                await service.initialize(upload_id)
                for offset in range(0,len(data),PART_BYTES):
                    chunk = data[offset:offset+PART_BYTES]
                    number = offset//PART_BYTES+1
                    grant = await service.part_grant(upload_id,number,base64.b64encode(hashlib.sha256(chunk).digest()).decode())
                    response = await browser.put(grant.url,headers=grant.headers,content=chunk)
                    check('direct_part_'+str(len(checks)),response.status_code == 200)
                    await service.acknowledge(upload_id,number)
                return upload_id
            plain = b'p'*PART_BYTES+b'original-tail'
            plain_id = await send('vido/plain/movie.mp4',plain)
            published = await service.complete(plain_id,actor='native')
            check('multipart_catalog_published',published['state'] == 'published' and published['confirmed_bytes'] == len(plain))
            plain_row = await row(plain_id)
            signer,_ = await members.signer(member_id)
            grant = signer.presign('GET',plain_row['object_key'])
            response = await browser.get(grant.url,headers={'Range':'bytes=3-9'})
            check('plain_direct_range_original_bytes',response.status_code == 206 and response.content == plain[3:10])

            original = b'encrypted boundary original sample'
            meta = media_crypto.Metadata.new(len(original))
            browser_key = AESGCM.generate_key(bit_length=256)
            ciphertext = AESGCM(browser_key).encrypt(meta.chunk_nonce(0),original,meta.chunk_aad(0))
            encrypted_id = await send('music/encrypted/song.mp3',ciphertext,meta)
            class LostAcknowledgement(TrackedControl):
                async def complete(self,*args,**kwargs):
                    await super().complete(*args,**kwargs)
                    raise ControlError('timeout',uncertain=True)
            lost_service = Uploads(store,control_factory=lambda signer:LostAcknowledgement(signer,context=context),lease_root=root/'leases')
            try:
                await lost_service.complete(encrypted_id,actor='native')
            except ControlError as error:
                check('real_completion_ack_lost_is_uncertain',error.code == 's3_timeout' and (await row(encrypted_id))['state'] == 'uncertain')
            else:
                raise AssertionError('missing_lost_ack_fault')
            (root/'recovery.json').write_text(json.dumps(dict(upload_id=encrypted_id,bytes=len(ciphertext),expected_used=len(plain)+len(ciphertext))))
            await database.dispose()
            child = await asyncio.to_thread(subprocess.run,[sys.executable,'-m','tests.s3_uploads_native'],
                env={**os.environ,'S3_TRANSACTION_PHASE':'recover'},capture_output=True,text=True,timeout=40)
            (root/'child-private.log').write_text(child.stderr)
            if child.returncode:
                raise AssertionError('separate_process_recovery_failed')
            child_receipt = json.loads(child.stdout)
            check('actual_child_process_recovery_passed',len(child_receipt['checks']) == 4)
            encrypted_row = await row(encrypted_id)
            grant = signer.presign('GET',encrypted_row['object_key'])
            response = await browser.get(grant.url)
            check('encrypted_object_preserved_and_client_decrypted',response.content == ciphertext and
                AESGCM(browser_key).decrypt(meta.chunk_nonce(0),response.content,meta.chunk_aad(0)) == original)
            async with database.connect() as conn:
                check('original_descriptor_retained',await media_crypto.encryption_for_id(conn,encrypted_row['media_id']) == meta)

            # A local credential-expiry policy makes the real signed grant short
            # without inventing clock travel or claiming an AWS STS integration.
            deadline = int(time.time())+40
            temporary = await add_member('temporary',100,Credentials(credentials.access_key,credentials.secret_key,expires_at=deadline))
            reserved = await service.reserve('music/cancel/song.mp3',7,actor='native')
            cancel_id = reserved['upload_id']
            check('optional_member_selected_by_capacity', (await row(cancel_id))['member_id'] == temporary)
            await service.initialize(cancel_id)
            grant = await service.part_grant(cancel_id,1,base64.b64encode(hashlib.sha256(b'partial').digest()).decode())
            response = await browser.put(grant.url,headers=grant.headers,content=b'partial')
            check('unconfirmed_remote_part_uploaded',response.status_code == 200)
            await service.cancel(cancel_id,actor='native')
            check('cancel_retains_live_grant_reservation',(await service.status(cancel_id))['state'] == 'aborting')
            await asyncio.sleep(max(0,grant.expires_at-time.time()+1.2))
            response = await browser.put(grant.url,headers=grant.headers,content=b'partial')
            check('old_part_grant_really_expired',response.status_code == 403)
            revision = await members.configure(temporary,3,name='temporary',quota=100,enabled=False,actor='native')
            await members.recovery_credentials(temporary,revision,credentials,'native')
            recovered = await Recovery(store,service=service).once()
            check('real_abort_reconciliation_closed',recovered['closed'] == 1 and (await service.status(cancel_id))['state'] == 'aborted')
            async with database.connect() as conn:
                check('abort_releases_only_its_reservation',await conn.scalar(select(s.members.c.reserved_bytes).where(s.members.c.member_id == temporary)) == 0 and
                    await conn.scalar(select(s.members.c.used_bytes).where(s.members.c.member_id == member_id)) == len(plain)+len(ciphertext))
            # Existing reads survive a placement disable. Bound real grants to
            # roughly ten seconds with the supported credential-expiry policy,
            # which reserves thirty seconds for clock/network margin, then
            # restore permanent credentials for controlled cleanup. This is not
            # an AWS STS claim; the compatible provider uses the same key pair.
            deadline = int(time.time())+42
            revision = await members.configure(member_id,3,name='main',quota=1024**3,enabled=False,
                credentials=Credentials(credentials.access_key,credentials.secret_key,expires_at=deadline),actor='native')
            objects = Objects(store,control_factory=factory,lease_root=root/'leases')
            download = await objects.grant(plain_row['media_id'],download=True)
            head_grant = await objects.grant(plain_row['media_id'],method='HEAD')
            encrypted_grant = await objects.grant(encrypted_row['media_id'])
            response = await browser.get(download.url,headers={'Range':'bytes=3-9'})
            check('catalog_owned_download_direct_range',response.status_code == 206 and response.content == plain[3:10] and
                'movie.mp4' in response.headers.get('content-disposition','') and response.headers.get('content-type') == 'video/mp4')
            response = await browser.head(head_grant.url)
            check('catalog_owned_head_signature',response.status_code == 200 and int(response.headers['content-length']) == len(plain))
            response = await browser.get(encrypted_grant.url)
            check('catalog_owned_ciphertext_direct',response.status_code == 200 and response.content == ciphertext and
                response.headers.get('content-type') == 'application/octet-stream')
            await members.configure(member_id,revision,name='main',quota=1024**3,enabled=False,credentials=credentials,actor='native')
            check('published_delete_waits_for_issued_grants',(await objects.delete(plain_row['media_id']))['state'] == 'deleting')
            await asyncio.sleep(max(0,max(download.expires_at,head_grant.expires_at,encrypted_grant.expires_at)-time.time()+1.2))
            class LostDelete(TrackedControl):
                async def delete(self,*args,**kwargs):
                    await super().delete(*args,**kwargs)
                    raise ControlError('timeout',uncertain=True)
            try:
                await Objects(store,control_factory=lambda signer:LostDelete(signer,context=context),lease_root=root/'leases').delete(plain_row['media_id'])
            except ControlError as error:
                check('published_delete_lost_ack_preserved',error.code == 's3_timeout')
            else:
                raise AssertionError('missing_delete_ack_fault')
            check('background_published_delete_closed',(await Recovery(store,service=service).once())['closed'] == 1)
            check('encrypted_published_delete_closed',(await objects.delete(encrypted_row['media_id']))['state'] == 'deleted')
            async with database.connect() as conn:
                check('published_delete_quota_and_catalog_settled',await conn.scalar(select(s.members.c.used_bytes).where(s.members.c.member_id == member_id)) == 0 and
                    await conn.scalar(select(fs.global_media.c.media_id).limit(1)) is None and
                    await media_crypto.encryption_for_id(conn,encrypted_row['media_id']) is None)
            check('published_delete_replay_is_idempotent',(await objects.delete(plain_row['media_id']))['state'] == 'deleted')
            # Use the fixture's actual vault and normal offline admission after
            # all remote cleanup. No state rewrite or idle-check bypass is used.
            from app.services.offline_upgrade import inspect_database
            operator_secrets = root/'operator-secrets'
            operator_keys = root/'operator-media-keys'
            operator_secrets.mkdir(mode=0o700)
            operator_keys.mkdir(mode=0o700)
            vault = operator_secrets/'node-vault.key'
            vault.write_bytes(key_file.read_bytes())
            vault.chmod(0o600)
            readonly = create_async_engine(URL.create('sqlite+aiosqlite',database=(root/'state.db').as_uri(),
                query={'mode':'ro','uri':'true'}))
            try:
                inspected = await inspect_database(readonly,operator_secrets,operator_keys,include_tables=False)
            finally:
                await readonly.dispose()
            check('real_provider_cancelled_history_allows_offline_preflight',inspected['role'] == 'Master' and
                inspected['generation'] == 9 and inspected['tables'] == {})
        all_wire = wire+child_receipt['wire']
        check('master_has_no_media_get_or_payload',all(item['method'] != 'GET' or item['response_kind'] in
            ('ListPartsResult','ListMultipartUploadsResult') for item in all_wire) and max(item['body_bytes'] for item in all_wire) < 4096)
        return dict(format='frontiercloud-python-s3-native-upload-service',version=1,checks=checks,count=len(checks),
            child_checks=child_receipt['checks'],master_metadata_requests=len(all_wire),master_media_bytes=0,
            payload_bytes=len(plain)+len(ciphertext)+7,seconds=round(time.monotonic()-started,4),
            tls_verified=True,ordinary_media_decrypted_by_product=False,aws_real_bucket=False,browser_gui_acceptance=False)
    finally:
        await database.dispose()


if __name__ == '__main__':
    try:
        print(json.dumps(asyncio.run(main()),sort_keys=True))
    except Exception as error:
        code = str(error) if isinstance(error,AssertionError) else type(error).__name__
        print(json.dumps(dict(failed=code,code=getattr(error,'code',None))))
        raise SystemExit(1) from None
