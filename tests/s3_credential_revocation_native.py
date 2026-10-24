"""Real provider key revocation across two native processes and a live grant.

The host fixture alone changes provider credentials. Application state, clocks,
deadlines and request signers are never rewritten to simulate revocation.
"""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
import uuid
from urllib.parse import parse_qs,urlsplit
from unittest.mock import patch

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import httpx
from sqlalchemy import select,text
from sqlalchemy.engine import URL

from app.core import db
from app.core.sqlite_compat import create_sqlite_engine
from app.services import media_crypto,s3_schema as s
from app.services.federation import schema as fs
from app.services.federation.state import State
from app.services.s3_control import Control,ControlError
from app.services.s3_members import Members,Conflict
from app.services.s3_recovery import Recovery
from app.services.s3_signing import Credentials,Location,Signer
from app.services.s3_uploads import Uploads


def safe_failure(error,fallback):
    code=error.__dict__.get('code')
    kind=type(error).__name__
    return dict(error_type=kind if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}',kind) else 'Exception',
        code=code if isinstance(code,str) and re.fullmatch(r'(?:s3|native)_[a-z0-9_]{1,60}',code) else fallback)


async def dispose_preserving_primary(database,primary):
    try:await database.dispose()
    except BaseException as failure:
        if primary is None:raise
        try:
            print(json.dumps(dict(cleanup_failed=True,**safe_failure(failure,'native_cleanup_failed'))),file=sys.stderr)
        except Exception:
            pass  # A broken diagnostic stream cannot replace the business error.


async def main():
    started=time.monotonic()
    fixture=Path(os.environ['S3_FIXTURE'])
    root=Path(os.environ['S3_TRANSACTION_ROOT'])
    phase=os.environ['S3_FIXTURE_PHASE']
    if root.parent!=fixture or root.name!='transactions-credential-revocation' or phase not in {'prepare','resume'}:
        raise RuntimeError('invalid_revocation_transaction_root')
    context=ssl.create_default_context(cafile=str(fixture/'ca.pem'))
    wire=[];checks=[]
    class ObservedControl(Control):
        async def _request(self,method,key='',**kwargs):
            event=dict(method=method,body_bytes=len(kwargs.get('body',b'')))
            wire.append(event)
            try:
                result=await super()._request(method,key,**kwargs)
                event['status']='accepted';return result
            except ControlError as error:
                event.update(status=error.status,code=error.code);raise
    def factory(signer):return ObservedControl(signer,context=context)
    def check(name,condition):
        if not condition:raise AssertionError(name)
        checks.append(name);print(json.dumps(dict(passed=name)),file=sys.stderr,flush=True)
    def private(path,value):
        path.write_bytes(value);path.chmod(0o600)
    if phase=='prepare':
        root.mkdir(mode=0o700)
        private(root/'vault.key',Fernet.generate_key())
    state_file=root/'revocation.json'
    database=create_sqlite_engine(URL.create('sqlite+aiosqlite',database=str(root/'state.db')))
    store=State(database,(root/'vault.key').read_bytes())
    service=Uploads(store,control_factory=factory,lease_root=root/'leases')
    members=Members(store)
    configuration=json.loads((fixture/'credentials.json').read_text())['identities'][1]['credentials']
    recovery=Credentials(configuration[0]['accessKey'],configuration[0]['secretKey'])
    async def row(upload_id):
        async with database.connect() as conn:
            return dict((await conn.execute(select(s.uploads).where(s.uploads.c.session_id==upload_id))).mappings().one())
    async def retained(state,*,terminal=False):
        async with database.connect() as conn:
            member=(await conn.execute(select(s.members).where(s.members.c.member_id==state['member_id']))).mappings().one()
            shared=(await conn.execute(select(fs.upload_sessions).where(fs.upload_sessions.c.upload_id==state['upload_id']))).mappings().one()
            descriptor=await media_crypto.encryption_for_id(conn,state['media_id'])
            return (member['used_bytes']==0 and member['reserved_bytes']==(0 if terminal else state['bytes']) and
                shared['state']==('aborted' if terminal else 'reserved') and
                shared['path_locator']==(None if terminal else state['path_locator']) and
                (descriptor is None if terminal else descriptor is not None and
                    hashlib.sha256(json.dumps(descriptor.to_dict(),sort_keys=True).encode()).hexdigest()==state['descriptor_sha256']))
    primary=None
    try:
        if phase=='prepare':
            if len(configuration)!=2:raise RuntimeError('revocable_fixture_key_missing')
            old=Credentials(configuration[1]['accessKey'],configuration[1]['secretKey'])
            with patch.object(db,'engine',database),patch.object(db,'SECRET_DIR',root/'secrets'):
                await db.init_db()
            await store.initialize()
            async with database.begin() as conn:await conn.execute(text("UPDATE node_identity SET role='Master'"))
            member=await members.add('revocable',Location('frontiercloud-python-acceptance','us-east-1',
                endpoint='https://s3.4399.com:8333',prefix='credential-revocation/'+uuid.uuid4().hex),old,1024**2,'native')
            signer,_=await members.signer(member)
            control=factory(signer)
            try:await control.head_bucket()
            finally:await control.close()
            revision=await members.probed(member,1)
            await members.configure(member,revision,name='revocable',quota=1024**2,enabled=True,actor='native')
            plaintext=b'revoked-credential-encrypted-part'*1024
            descriptor=media_crypto.Metadata.new(len(plaintext))
            ciphertext=AESGCM(AESGCM.generate_key(bit_length=256)).encrypt(descriptor.chunk_nonce(0),plaintext,descriptor.chunk_aad(0))
            reserved=await service.reserve('music/revoked/partial.mp3',len(ciphertext),descriptor,actor='native')
            await service.initialize(reserved['upload_id'])
            grant=await service.part_grant(reserved['upload_id'],1,base64.b64encode(hashlib.sha256(ciphertext).digest()).decode())
            async with httpx.AsyncClient(verify=context,trust_env=False,follow_redirects=False,timeout=30) as browser:
                response=await browser.put(grant.url,headers=grant.headers,content=ciphertext)
            check('revocable_key_uploads_actual_encrypted_part',response.status_code==200)
            await service.acknowledge(reserved['upload_id'],1)
            current=await row(reserved['upload_id'])
            check('ordinary_full_300_second_grant',parse_qs(urlsplit(grant.url).query).get('X-Amz-Expires')==['300'] and
                grant.expires_at-int(time.time())>120 and signer.credentials.expires_at is None)
            private(root/'ciphertext.bin',ciphertext)
            state=dict(format='frontiercloud-s3-credential-revocation',version=1,phase='prepared',
                member_id=member,upload_id=reserved['upload_id'],media_id=reserved['media_id'],
                path_locator=current['path_locator'],object_key=current['object_key'],provider_upload_id=current['provider_upload_id'],
                bytes=len(ciphertext),ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest(),
                descriptor_sha256=hashlib.sha256(json.dumps(descriptor.to_dict(),sort_keys=True).encode()).hexdigest(),
                revoked_access_key_sha256=hashlib.sha256(old.access_key.encode()).hexdigest(),grant_until=grant.expires_at,
                vault_sha256=hashlib.sha256((root/'vault.key').read_bytes()).hexdigest(),
                grant=dict(url=grant.url,headers=grant.headers))
            check('prepared_reservation_path_and_descriptor_retained',await retained(state))
            check('prepare_control_has_no_media_payload',all(item['body_bytes']==0 for item in wire))
            private(state_file,json.dumps(state).encode())
            return dict(passed=True,phase='prepare',checks=checks,grant_until=grant.expires_at,
                encrypted_part_bytes=len(ciphertext),control_body_bytes=sum(item['body_bytes'] for item in wire),
                aws_real_bucket=False,tls_errors_ignored=False)
        state=json.loads(state_file.read_text())
        provider=json.loads((fixture/'credential-revocation-provider.json').read_text())
        check('same_persistent_vault_and_provider_process',state['vault_sha256']==hashlib.sha256((root/'vault.key').read_bytes()).hexdigest()
            and provider['passed'] and provider['pid_before']==provider['pid_after'] and not provider['provider_restarted']
            and provider['revoked_access_key_sha256']==state['revoked_access_key_sha256'])
        async with database.connect() as conn:
            identity=await store.lock(conn)
            check('restarted_process_opens_original_master_identity',identity['role']=='Master' and bool(store.unseal(identity['private_key'])))
        old_signer,_=await members.signer(state['member_id'])
        check('stored_old_key_really_revoked_without_local_expiry',old_signer.credentials.expires_at is None and
            hashlib.sha256(old_signer.credentials.access_key.encode()).hexdigest()==state['revoked_access_key_sha256'])
        ciphertext=(root/'ciphertext.bin').read_bytes()
        check('same_ciphertext_part_retained',hashlib.sha256(ciphertext).hexdigest()==state['ciphertext_sha256'])
        # HUP reload is asynchronous. Only refusal before the original deadline
        # establishes provider revocation, rather than ordinary grant expiry.
        async with httpx.AsyncClient(verify=context,trust_env=False,follow_redirects=False,timeout=15) as browser:
            reload_deadline=time.monotonic()+30
            while True:
                check('revocation_observed_before_grant_expiry_'+str(len(checks)),time.time()<state['grant_until'])
                response=await browser.put(state['grant']['url'],headers=state['grant']['headers'],content=ciphertext)
                if response.status_code==403:break
                if response.status_code!=200 or time.monotonic()>=reload_deadline:raise AssertionError('provider_revocation_not_observed')
                await asyncio.sleep(1)
        check('unexpired_old_part_grant_really_refused',response.status_code==403 and time.time()<state['grant_until'])
        control=factory(Signer(old_signer.location,recovery))
        try:await control.head_bucket()
        finally:await control.close()
        check('original_primary_provider_key_still_works',True)
        await service.cancel(state['upload_id'],actor='native')
        check('cancel_with_live_grant_retains_all_resources',await retained(state))
        revision=await members.configure(state['member_id'],3,name='revocable',quota=1024**2,enabled=False,actor='native')
        try:await members.recovery_credentials(state['member_id'],revision,recovery,'native')
        except Conflict as error:check('recovery_credentials_refused_before_old_grant_expiry',error.code=='s3_recovery_credentials_not_safe')
        else:raise AssertionError('premature_recovery_credentials_accepted')
        waiting=time.monotonic()
        while time.time()<=state['grant_until']+1:
            await asyncio.sleep(min(15,max(.1,state['grant_until']+1.2-time.time())))
        waited=time.monotonic()-waiting
        control=factory(old_signer)
        try:
            try:await control.abort(state['object_key'],state['provider_upload_id'])
            except ControlError as error:check('old_key_native_abort_really_forbidden',error.status==403)
            else:raise AssertionError('revoked_key_abort_was_not_refused')
        finally:await control.close()
        try:await service.reconcile(state['upload_id'])
        except ControlError as error:check('ordinary_reconciliation_keeps_revoked_transaction',error.status==403)
        else:raise AssertionError('revoked_credentials_unexpectedly_reconciled')
        check('failed_cleanup_keeps_exact_quota_path_and_descriptor',await retained(state))
        control=factory(Signer(old_signer.location,recovery))
        try:
            parts=await control.parts(state['object_key'],state['provider_upload_id'])
            check('failed_cleanup_keeps_actual_provider_part',len(parts['parts'])==1 and parts['parts'][0]['bytes']==state['bytes'])
        finally:await control.close()
        await members.recovery_credentials(state['member_id'],revision,recovery,'native')
        result=await Recovery(store,service=service).once()
        check('normal_recovery_settles_cancel_exactly_once',result['closed']==1 and await retained(state,terminal=True))
        check('normal_recovery_is_idempotent',(await Recovery(store,service=service).once())['processed']==0 and
            (await service.cancel(state['upload_id'],actor='native'))['state']=='aborted' and await retained(state,terminal=True))
        signer,_=await members.signer(state['member_id'])
        control=factory(signer)
        try:
            try:await control.parts(state['object_key'],state['provider_upload_id'])
            except ControlError as error:check('provider_multipart_really_absent',error.code=='s3_upload_missing')
            else:raise AssertionError('provider_parts_remain')
            check('provider_no_unpublished_replacement',await control.uploads(state['object_key'])==[])
        finally:await control.close()
        async with database.connect() as conn:
            member=(await conn.execute(select(s.members).where(s.members.c.member_id==state['member_id']))).mappings().one()
            check('member_remains_disabled_and_requires_new_probe',member['enabled']==0 and member['probe_state']=='unchecked')
            check('part_audit_and_encryption_tombstone_retained',
                await conn.scalar(text('SELECT COUNT(*) FROM s3_upload_parts WHERE session_id=:id'),{'id':state['upload_id']})==1 and
                await conn.scalar(text("SELECT descriptor_json IS NULL FROM media_encryption WHERE object_kind='media' AND object_id=:id"),
                    {'id':state['media_id']})==1)
        check('metadata_control_never_carries_media_payload',all(item['body_bytes']==0 for item in wire))
        return dict(passed=True,phase='resume',checks=checks,actual_deadline_wait_seconds=round(waited,3),
            grant_until=state['grant_until'],control_requests=wire,control_body_bytes=0,
            encrypted_part_bytes=state['bytes'],elapsed_seconds=round(time.monotonic()-started,3),
            aws_real_bucket=False,tls_errors_ignored=False,source_clock_or_sql_deadline_changed=False)
    except BaseException as failure:
        primary=failure
        raise
    finally:await dispose_preserving_primary(database,primary)


if __name__=='__main__':
    try:print(json.dumps(asyncio.run(main())))
    except Exception as error:
        print(json.dumps(dict(passed=False,**safe_failure(error,'native_revocation_failed'))),file=sys.stderr)
        raise SystemExit(1) from None
