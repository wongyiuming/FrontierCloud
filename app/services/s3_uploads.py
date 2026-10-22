"""Durable, bounded S3 multipart lifecycle. Media bytes never enter this service.

Callers must authorize Admin access before issuing grants. Parts bind one exact
checksum and length. Uncertain mutations and cancellation retain reservations
until provider metadata proves publication or controlled abort.
"""
import base64
from contextlib import asynccontextmanager
import hashlib
import json
import time
import uuid

from sqlalchemy import func, insert, select, update

from app.services import media_crypto, resource_fences, resource_pool, s3_schema as s
from app.services.federation import schema as fs
from app.services.media_manager import ensure_media_mutations_ready, media_mutation_lock
from app.services.media_catalog_cache import invalidate_media_catalog
from app.services.s3_control import Control, ControlError, checksum
from app.services.s3_members import Members, Conflict, identifier
from app.services.s3_signing import Credentials, Location, Signer, S3Error
from app.services.upload_claims import claim

PART_BYTES = 8*1024**2
MAX_BYTES = 10*1024**3
IDLE_SECONDS = 30*60
MAX_SESSION_SECONDS = 24*60*60
MAX_PART_GRANTS = 2


def _composite(parts):
    return base64.b64encode(hashlib.sha256(b''.join(base64.b64decode(part['checksum']) for part in parts)).digest()).decode()+'-'+str(len(parts))


class Uploads:
    def __init__(self,store,*,clock=time.time,control_factory=Control,lease_root=None):
        self.store,self.clock,self.control_factory,self.lease_root = store,clock,control_factory,lease_root
        self.members = Members(store,clock=clock)

    async def _row(self,conn,session_id):
        node = await self.members._master(conn)
        row = (await conn.execute(select(s.uploads).where(s.uploads.c.session_id == identifier(session_id))
                                  .with_for_update())).mappings().first()
        if row is None or row['master_id'] != node['node_id']:
            raise S3Error('s3_upload_missing')
        return dict(row)

    async def _reservation(self,conn,row):
        shared = (await conn.execute(select(fs.upload_sessions).where(
            fs.upload_sessions.c.upload_id == row['session_id']).with_for_update())).mappings().first()
        if (shared is None or shared['state'] != 'reserved' or shared['media_id'] != row['media_id'] or
            shared['storage_member_id'] != row['member_id'] or shared['expected_bytes'] != row['expected_bytes'] or
            shared['media_path'] != row['media_path'] or shared['path_locator'] != row['path_locator']):
            raise Conflict('s3_reservation_inconsistent')
        return dict(shared)

    def _signer(self,member):
        return Signer(Location(**json.loads(member['location_json'])),
            Credentials(**json.loads(self.store.unseal(member['credentials_sealed']))),clock=self.clock)

    @asynccontextmanager
    async def _control(self,row):
        async with self.store.database.begin() as conn:
            member = await self.members._row(conn,row['member_id'])
            signer = self._signer(member)
        control = self.control_factory(signer)
        try:
            yield control
        finally:
            await control.close()

    async def status(self,session_id):
        async with self.store.database.begin() as conn:
            row = await self._row(conn,session_id)
            received = int(await conn.scalar(select(func.coalesce(func.sum(s.parts.c.size_bytes),0)).where(
                s.parts.c.session_id == session_id,s.parts.c.etag != '')) or 0)
            return dict(upload_id=session_id,media_id=row['media_id'],path=row['media_path'],state=row['state'],
                expected_bytes=row['expected_bytes'],confirmed_bytes=received,part_bytes=row['part_bytes'],
                part_count=row['part_count'],expires_at=row['expires_at'],grant_until=row['grant_until'],error_code=row['error_code'])

    async def reserve(self,path,size,encryption=None,*,actor):
        path,kind = resource_pool.validate_media_path(path)
        if type(size) is not int or not 0 < size <= MAX_BYTES:
            raise S3Error('s3_upload_size_invalid')
        if encryption is not None and (not isinstance(encryption,media_crypto.Metadata) or encryption.ciphertext_size != size):
            raise S3Error('s3_upload_encryption_invalid')
        now,session_id = int(self.clock()),uuid.uuid4().hex
        media_id = hashlib.sha256((session_id+':'+path).encode()).hexdigest()
        object_id = uuid.uuid4().hex+uuid.uuid4().hex
        locator = resource_pool.path_locator(path)
        async with media_mutation_lock.shared(),resource_pool.storage_write_lock:
            ensure_media_mutations_ready()
            async with self.store.database.begin() as conn:
                node = await self.members._master(conn)
                parent,depth = path.rsplit('/',1)[0],path.count('/')
                owners = set()
                for table in (fs.global_media,fs.upload_sessions):
                    states = ('active','pending_delete') if table is fs.global_media else ('reserved',)
                    columns = table.c
                    category = '/'.join(path.split('/')[:2])+'/'
                    stored_depth = func.length(columns.media_path)-func.length(func.replace(columns.media_path,'/',''))
                    if await conn.scalar(select(columns.storage_member_id).where(columns.state.in_(states),
                        columns.media_path.startswith(category,autoescape=True),stored_depth != depth).limit(1).with_for_update()):
                        raise Conflict('s3_category_layout_conflict')
                    values = (await conn.execute(select(columns.storage_member_id).where(columns.state.in_(states),
                        columns.media_path.startswith(parent+'/',autoescape=True),
                        stored_depth == depth)
                        .distinct().limit(2).with_for_update())).scalars()
                    owners.update(values)
                if len(owners) > 1:
                    raise Conflict('s3_parent_ownership_conflict')
                owner = next(iter(owners),None)
                candidates = [dict(row) for row in (await conn.execute(select(s.members).where(
                    s.members.c.master_id == node['node_id'],s.members.c.enabled == 1,s.members.c.probe_state == 'ready',
                    s.members.c.quota_bytes-s.members.c.used_bytes-s.members.c.reserved_bytes >= size)
                    .limit(64).with_for_update())).mappings() if owner is None or row['member_id'] == owner]
                if not candidates:
                    raise Conflict('s3_parent_unavailable' if owner else 's3_capacity_unavailable')
                candidates.sort(key=lambda row:((row['used_bytes']+row['reserved_bytes'])/row['quota_bytes'],
                    -(row['quota_bytes']-row['used_bytes']-row['reserved_bytes']),row['member_id']))
                member=None
                for candidate in candidates:
                    try:
                        self._signer(candidate)._time()
                    except S3Error as error:
                        if error.code != 's3_credentials_expired':
                            raise
                    else:
                        member=candidate
                        break
                if member is None:
                    raise S3Error('s3_credentials_expired')
                await resource_fences.new_upload(conn,member['member_id'],'media',path)
                if await conn.scalar(select(fs.global_media.c.media_id).where(fs.global_media.c.path_locator == locator)) or await conn.scalar(
                    select(fs.upload_sessions.c.upload_id).where(fs.upload_sessions.c.path_locator == locator)):
                    raise Conflict('s3_path_exists')
                location = Location(**json.loads(member['location_json']))
                await conn.execute(insert(s.uploads).values(session_id=session_id,member_id=member['member_id'],master_id=node['node_id'],
                    media_id=media_id,object_id=object_id,path_locator=locator,media_path=path,object_key=location.object_key(member['member_id'],object_id),
                    state='reserved',expected_bytes=size,part_bytes=PART_BYTES,part_count=(size+PART_BYTES-1)//PART_BYTES,
                    credential_revision=member['revision'],expires_at=now+IDLE_SECONDS,created_at=now,updated_at=now))
                await conn.execute(insert(fs.upload_sessions).values(upload_id=session_id,storage_member_id=member['member_id'],media_id=media_id,
                    media_path=path,path_locator=locator,object_kind=kind,expected_bytes=size,content_encrypted=int(encryption is not None),
                    state='reserved',expires_at=now+IDLE_SECONDS,created_at=now,updated_at=now))
                await conn.execute(update(s.members).where(s.members.c.member_id == member['member_id']).values(
                    reserved_bytes=s.members.c.reserved_bytes+size,updated_at=now))
                await media_crypto.put_encryption(conn,media_id,encryption)
                await self.store.log(conn,'s3_upload_reserved',actor,session_id=session_id,member_id=member['member_id'],bytes=size)
        return await self.status(session_id)

    async def initialize(self,session_id):
        with claim(identifier(session_id),root=self.lease_root):
            async with self.store.database.begin() as conn:
                row = await self._row(conn,session_id)
                await self._reservation(conn,row)
                if row['state'] == 'uploading':
                    return await self._known_status(row)
                if row['state'] != 'reserved' or row['expires_at'] <= int(self.clock()):
                    raise Conflict('s3_upload_state_conflict')
                # Local credential failure has no unknown provider side effect.
                # Check before creating the durable outbound-request marker.
                self._signer(await self.members._row(conn,row['member_id']))._time()
                await conn.execute(update(s.uploads).where(s.uploads.c.session_id == session_id).values(state='creating',
                    revision=row['revision']+1,updated_at=int(self.clock())))
                row.update(state='creating',revision=row['revision']+1)
            try:
                async with self._control(row) as control:
                    provider = await control.create(row['object_key'],session_id,row['object_id'])
            except S3Error as error:
                await self._failed(row,'create',error)
                raise
            async with self.store.database.begin() as conn:
                current = await self._row(conn,session_id)
                if current['state'] != 'creating' or current['revision'] != row['revision']:
                    raise Conflict('s3_upload_state_conflict')
                await conn.execute(update(s.uploads).where(s.uploads.c.session_id == session_id).values(
                    state='uploading',provider_upload_id=provider,error_code='',revision=current['revision']+1,updated_at=int(self.clock())))
        return await self.status(session_id)

    async def _known_status(self,row):
        return dict(upload_id=row['session_id'],media_id=row['media_id'],state=row['state'],path=row['media_path'],
            part_bytes=row['part_bytes'],part_count=row['part_count'],expected_bytes=row['expected_bytes'])

    async def _failed(self,row,stage,error):
        code = stage+'_'+str(error)
        if len(code) > 64 or any(char not in 'abcdefghijklmnopqrstuvwxyz0123456789_' for char in code):
            code = stage+'_s3_upstream_error'
        async with self.store.database.begin() as conn:
            current = await self._row(conn,row['session_id'])
            if current['revision'] == row['revision'] and current['state'] == row['state']:
                # A definitive provider refusal created nothing to reconcile.
                # Keep the original reservation/descriptor/deadline so existing
                # retry, cancellation and expiry paths own their settlement.
                # Failure to commit this proof retains the outbound marker.
                rejected = (stage == 'create' and isinstance(error,ControlError) and error.creation_rejected
                    and current['state'] == 'creating' and not any(current[name] for name in
                        ('provider_upload_id','grant_until','checksum','etag','version_id')))
                await conn.execute(update(s.uploads).where(s.uploads.c.session_id == row['session_id']).values(
                    state='reserved' if rejected else 'uncertain',error_code=code,
                    revision=current['revision']+1,updated_at=int(self.clock())))

    async def part_grant(self,session_id,number,digest):
        checksum(digest)
        if type(number) is not int or not 1 <= number <= 1280:
            raise S3Error('s3_part_invalid')
        now = int(self.clock())
        async with self.store.database.begin() as conn:
            row = await self._row(conn,session_id)
            await self._reservation(conn,row)
            if row['state'] != 'uploading' or row['expires_at'] <= now or number > row['part_count']:
                raise Conflict('s3_upload_state_conflict')
            size = min(row['part_bytes'],row['expected_bytes']-(number-1)*row['part_bytes'])
            part = (await conn.execute(select(s.parts).where(s.parts.c.session_id == session_id,s.parts.c.part_number == number)
                                       .with_for_update())).mappings().first()
            if part and (part['checksum_sha256'] != digest or part['size_bytes'] != size):
                raise Conflict('s3_part_content_conflict')
            outstanding = await conn.scalar(select(func.count()).select_from(s.parts).where(s.parts.c.session_id == session_id,
                s.parts.c.part_number != number,s.parts.c.etag == '',s.parts.c.grant_until > now))
            if outstanding >= MAX_PART_GRANTS:
                raise Conflict('s3_part_concurrency_limit')
            member = await self.members._row(conn,row['member_id'])
            expires = min(now+IDLE_SECONDS,row['created_at']+MAX_SESSION_SECONDS)
            signed = self._signer(member).presign('PUT',row['object_key'],query=[('uploadId',row['provider_upload_id']),('partNumber',str(number))],
                seconds=min(300,expires-now),headers={'content-length':str(size),'content-type':'application/octet-stream','x-amz-checksum-sha256':digest})
            values = dict(size_bytes=size,checksum_sha256=digest,grant_until=signed.expires_at,updated_at=now)
            if part is None:
                await conn.execute(insert(s.parts).values(session_id=session_id,part_number=number,**values))
            else:
                await conn.execute(update(s.parts).where(s.parts.c.session_id == session_id,s.parts.c.part_number == number).values(**values))
            await conn.execute(update(s.uploads).where(s.uploads.c.session_id == session_id).values(
                grant_until=max(row['grant_until'],signed.expires_at),expires_at=expires,updated_at=now))
            await conn.execute(update(fs.upload_sessions).where(fs.upload_sessions.c.upload_id == session_id).values(expires_at=expires,updated_at=now))
            return signed

    async def acknowledge(self,session_id,number):
        if type(number) is not int or not 1 <= number <= 1280:
            raise S3Error('s3_part_invalid')
        async with self.store.database.begin() as conn:
            row = await self._row(conn,session_id)
        if row['state'] != 'uploading':
            raise Conflict('s3_upload_state_conflict')
        async with self._control(row) as control:
            result = await control.parts(row['object_key'],row['provider_upload_id'],after=number-1,limit=1)
        physical = result['parts'][0] if result['parts'] else None
        async with self.store.database.begin() as conn:
            row = await self._row(conn,session_id)
            expected = (await conn.execute(select(s.parts).where(s.parts.c.session_id == session_id,s.parts.c.part_number == number)
                                          .with_for_update())).mappings().first()
            if row['state'] != 'uploading' or row['expires_at'] <= int(self.clock()):
                raise Conflict('s3_upload_state_conflict')
            if (not expected or not physical or physical['number'] != number or physical['bytes'] != expected['size_bytes'] or
                physical['checksum'] not in (None,expected['checksum_sha256'])):
                raise Conflict('s3_part_not_verified')
            await conn.execute(update(s.parts).where(s.parts.c.session_id == session_id,s.parts.c.part_number == number).values(
                etag=physical['etag'],updated_at=int(self.clock())))
            await self._touch(conn,row)
        return await self.status(session_id)

    async def _touch(self,conn,row):
        now = int(self.clock())
        if row['state'] != 'uploading' or row['expires_at'] <= now:
            raise Conflict('s3_upload_state_conflict')
        expires = min(now+IDLE_SECONDS,row['created_at']+MAX_SESSION_SECONDS)
        await conn.execute(update(s.uploads).where(s.uploads.c.session_id == row['session_id']).values(expires_at=expires,updated_at=now))
        await conn.execute(update(fs.upload_sessions).where(fs.upload_sessions.c.upload_id == row['session_id']).values(expires_at=expires,updated_at=now))

    async def touch(self,session_id):
        # Authenticated browser activity renews idle time without pretending that
        # client progress is provider-confirmed bytes or extending a part grant.
        async with self.store.database.begin() as conn:
            row = await self._row(conn,session_id)
            await self._reservation(conn,row)
            await self._touch(conn,row)
        return await self.status(session_id)

    async def _manifest(self,conn,row):
        expected = [dict(number=part['part_number'],etag=part['etag'],checksum=part['checksum_sha256']) for part in
            (await conn.execute(select(s.parts).where(s.parts.c.session_id == row['session_id']).order_by(s.parts.c.part_number)
                                .limit(1281).with_for_update())).mappings()]
        if len(expected) != row['part_count'] or any(part['number'] != number or not part['etag'] for number,part in enumerate(expected,1)):
            raise Conflict('s3_parts_incomplete')
        return expected

    async def complete(self,session_id,*,actor):
        with claim(identifier(session_id),root=self.lease_root):
            async with self.store.database.begin() as conn:
                row = await self._row(conn,session_id)
                if row['state'] == 'published':
                    return await self._known_status(row)
                await self._reservation(conn,row)
                if row['state'] != 'uploading' or row['expires_at'] <= int(self.clock()):
                    raise Conflict('s3_upload_state_conflict')
                parts = await self._manifest(conn,row)
                await conn.execute(update(s.uploads).where(s.uploads.c.session_id == session_id).values(state='completing',
                    revision=row['revision']+1,checksum=_composite(parts),updated_at=int(self.clock())))
                row.update(state='completing',revision=row['revision']+1,checksum=_composite(parts))
            try:
                async with self._control(row) as control:
                    result = await control.complete(row['object_key'],row['provider_upload_id'],parts)
                    head = await control.head(row['object_key'],version_id=result['version_id'])
                self._verify_head(row,head)
            except S3Error as error:
                await self._failed(row,'complete',error)
                raise
            await self._publish(row,head,actor)
        return await self.status(session_id)

    def _verify_head(self,row,head):
        if (head['bytes'] != row['expected_bytes'] or head['session_id'] != row['session_id'] or
            head['object_id'] != row['object_id'] or head['checksum'] != row['checksum']):
            raise ControlError('publication_identity',uncertain=True)

    async def _publish(self,row,head,actor):
        async with media_mutation_lock.shared(),resource_pool.storage_write_lock:
            ensure_media_mutations_ready()
            async with self.store.database.begin() as conn:
                current = await self._row(conn,row['session_id'])
                if current['state'] == 'published':
                    return
                if current['state'] not in ('completing','uncertain') or current['revision'] != row['revision']:
                    raise Conflict('s3_upload_state_conflict')
                shared = await self._reservation(conn,current)
                self._verify_head(current,head)
                if shared['content_encrypted']:
                    meta = await media_crypto.encryption_for_id(conn,current['media_id'])
                    if meta is None or meta.ciphertext_size != current['expected_bytes']:
                        raise Conflict('s3_encryption_descriptor_missing')
                member = await self.members._row(conn,current['member_id'])
                if member['reserved_bytes'] < current['expected_bytes']:
                    raise Conflict('s3_reservation_inconsistent')
                now = int(self.clock())
                await conn.execute(insert(s.objects).values(media_id=current['media_id'],member_id=current['member_id'],object_id=current['object_id'],
                    session_id=current['session_id'],object_key=current['object_key'],size_bytes=current['expected_bytes'],checksum=current['checksum'],
                    etag=head['etag'],version_id=head['version_id'],content_encrypted=shared['content_encrypted'],state='active',created_at=now,updated_at=now))
                await conn.execute(insert(fs.global_media).values(media_id=current['media_id'],storage_member_id=current['member_id'],object_id=current['object_id'],
                    media_path=current['media_path'],path_locator=current['path_locator'],object_kind=shared['object_kind'],size_bytes=current['expected_bytes'],
                    etag=head['etag'],state='active',created_at=now,updated_at=now))
                await conn.execute(update(s.members).where(s.members.c.member_id == member['member_id']).values(
                    reserved_bytes=member['reserved_bytes']-current['expected_bytes'],used_bytes=member['used_bytes']+current['expected_bytes'],
                    catalog_revision=s.members.c.catalog_revision+1,updated_at=now))
                await conn.execute(update(fs.upload_sessions).where(fs.upload_sessions.c.upload_id == current['session_id']).values(state='complete',path_locator=None,updated_at=now))
                await conn.execute(update(s.uploads).where(s.uploads.c.session_id == current['session_id']).values(state='published',etag=head['etag'],
                    version_id=head['version_id'],error_code='',revision=current['revision']+1,updated_at=now))
                await self.store.log(conn,'s3_upload_published',actor,session_id=current['session_id'],media_id=current['media_id'])
        await invalidate_media_catalog()

    async def _recover_create(self,row):
        async with self._control(row) as control:
            values = await control.uploads(row['object_key'])
        if len(values) != 1:
            # CreateMultipartUpload has no idempotency key. Never issue a new
            # create or invent absence after a lost acknowledgement.
            raise Conflict('s3_creation_unresolved' if not values else 's3_creation_ambiguous')
        async with self.store.database.begin() as conn:
            current = await self._row(conn,row['session_id'])
            if current['revision'] != row['revision'] or current['provider_upload_id']:
                raise Conflict('s3_upload_state_conflict')
            state = 'aborting' if current['state'] == 'aborting' else 'uploading'
            await conn.execute(update(s.uploads).where(s.uploads.c.session_id == row['session_id']).values(
                provider_upload_id=values[0],state=state,error_code='',revision=current['revision']+1,updated_at=int(self.clock())))
            current.update(provider_upload_id=values[0],state=state,error_code='',revision=current['revision']+1)
            return current

    async def _start_abort(self,row,actor,*,expired=False):
        async with self.store.database.begin() as conn:
            current = await self._row(conn,row['session_id'])
            if current['state'] in ('published','aborted') or current['revision'] != row['revision']:
                raise Conflict('s3_upload_state_conflict')
            if expired and current['expires_at'] > int(self.clock()):
                return None
            if current['state'] != 'aborting':
                await conn.execute(update(s.uploads).where(s.uploads.c.session_id == row['session_id']).values(
                    state='aborting',revision=current['revision']+1,updated_at=int(self.clock())))
                current.update(state='aborting',revision=current['revision']+1)
                await self.store.log(conn,'s3_upload_cancel_requested',actor,session_id=row['session_id'])
            return current

    async def _release(self,row,actor):
        async with self.store.database.begin() as conn:
            current = await self._row(conn,row['session_id'])
            if current['state'] == 'aborted':
                return
            if current['state'] not in ('reserved','aborting') or current['revision'] != row['revision']:
                raise Conflict('s3_upload_state_conflict')
            await self._reservation(conn,current)
            if await conn.scalar(select(fs.global_media.c.media_id).where(fs.global_media.c.media_id == current['media_id'])):
                raise Conflict('s3_published_object_cannot_abort')
            member = await self.members._row(conn,current['member_id'])
            if member['reserved_bytes'] < current['expected_bytes']:
                raise Conflict('s3_reservation_inconsistent')
            now = int(self.clock())
            await conn.execute(update(s.members).where(s.members.c.member_id == member['member_id']).values(
                reserved_bytes=member['reserved_bytes']-current['expected_bytes'],updated_at=now))
            await conn.execute(update(fs.upload_sessions).where(fs.upload_sessions.c.upload_id == current['session_id']).values(
                state='aborted',path_locator=None,updated_at=now))
            await conn.execute(update(s.uploads).where(s.uploads.c.session_id == current['session_id']).values(
                state='aborted',revision=current['revision']+1,error_code='',updated_at=now))
            await media_crypto.forget_encryption(conn,current['media_id'])
            await self.store.log(conn,'s3_upload_aborted',actor,session_id=current['session_id'])

    async def _head_or_missing(self,control,row,*,version_id=''):
        try:
            return await control.head(row['object_key'],version_id=version_id)
        except ControlError as error:
            if error.code not in ('s3_not_found','s3_object_missing'):
                raise
            return None

    async def _abort_remote(self,row,actor):
        try:
            await self._abort_remote_checked(row,actor)
        except S3Error as error:
            code = 'cleanup_'+error.code
            if len(code) > 64:
                code = 'cleanup_s3_recovery_failed'
            async with self.store.database.begin() as conn:
                current = await self._row(conn,row['session_id'])
                if current['state'] == 'aborting':
                    await conn.execute(update(s.uploads).where(s.uploads.c.session_id == row['session_id']).values(
                        error_code=code,updated_at=int(self.clock())))
            raise

    async def _abort_remote_checked(self,row,actor):
        if row['state'] != 'aborting':
            raise Conflict('s3_upload_state_conflict')
        if row['grant_until'] > int(self.clock()):
            return
        if not row['provider_upload_id']:
            row = await self._recover_create(row)
        async with self._control(row) as control:
            head = await self._head_or_missing(control,row,version_id=row['version_id'] if row['etag'] else '')
            observed_output = bool(row['etag'])
            if head is not None:
                # A timed-out completion may already have published bytes at
                # the provider. Only this session's proven unpublished output
                # may be removed; unknown keys are never a cleanup target.
                self._verify_head(row,head)
                if observed_output and (row['etag'] != head['etag'] or row['version_id'] != head['version_id']):
                    raise Conflict('s3_abort_object_changed')
                if not observed_output:
                    # Preserve the independently verified completed object before
                    # DELETE. A lost delete acknowledgement must remain recoverable
                    # without mistaking its absence for an unknown completion.
                    async with self.store.database.begin() as conn:
                        current = await self._row(conn,row['session_id'])
                        if current['state'] != 'aborting' or current['revision'] != row['revision']:
                            raise Conflict('s3_upload_state_conflict')
                        await conn.execute(update(s.uploads).where(s.uploads.c.session_id == row['session_id']).values(
                            etag=head['etag'],version_id=head['version_id'],revision=current['revision']+1,updated_at=int(self.clock())))
                        row.update(etag=head['etag'],version_id=head['version_id'],revision=current['revision']+1)
                    observed_output = True
                await control.delete(row['object_key'],version_id=head['version_id'])
                try:
                    await control.head(row['object_key'],version_id=head['version_id'])
                except ControlError as error:
                    if error.code not in ('s3_not_found','s3_object_missing'):
                        raise
                else:
                    raise Conflict('s3_abort_object_remaining')
            abort_acknowledged = False
            try:
                await control.abort(row['object_key'],row['provider_upload_id'])
                abort_acknowledged = True
            except ControlError as error:
                if error.code != 's3_upload_missing':
                    raise
            if row['checksum'] and not observed_output and not abort_acknowledged:
                # An unacknowledged completion may still be finalizing after
                # consuming the upload ID. Absence alone must not release quota
                # and lose the immutable key before its outcome is observed.
                raise Conflict('s3_completion_unresolved')
            try:
                await control.parts(row['object_key'],row['provider_upload_id'],limit=1)
            except ControlError as error:
                if error.code != 's3_upload_missing':
                    raise
            else:
                # Abort can race a previously accepted part. Keep the journal
                # and repeat reconciliation; an empty ListParts is not absence.
                raise Conflict('s3_abort_pending')
            if await self._head_or_missing(control,row) is not None:
                raise Conflict('s3_abort_object_remaining')
        await self._release(row,actor)

    async def cancel(self,session_id,*,actor):
        with claim(identifier(session_id),root=self.lease_root):
            async with self.store.database.begin() as conn:
                row = await self._row(conn,session_id)
            if row['state'] == 'published':
                raise Conflict('s3_published_object_cannot_abort')
            if row['state'] == 'reserved':
                await self._release(row,actor)
            elif row['state'] != 'aborted':
                row = await self._start_abort(row,actor)
                await self._abort_remote(row,actor)
        return await self.status(session_id)

    async def reconcile(self,session_id,*,actor='system'):
        with claim(identifier(session_id),root=self.lease_root):
            async with self.store.database.begin() as conn:
                row = await self._row(conn,session_id)
            if row['state'] in ('published','aborted'):
                pass
            elif row['state'] == 'reserved':
                if row['expires_at'] <= int(self.clock()):
                    await self._release(row,actor)
            else:
                if not row['provider_upload_id']:
                    row = await self._recover_create(row)
                if row['state'] == 'aborting':
                    await self._abort_remote(row,actor)
                elif row['state'] == 'completing' or (row['state'] == 'uncertain' and row['error_code'].startswith('complete_')):
                    async with self._control(row) as control:
                        head = await self._head_or_missing(control,row)
                        if head is None:
                            await control.parts(row['object_key'],row['provider_upload_id'],limit=1)
                    if head is not None:
                        self._verify_head(row,head)
                        await self._publish(row,head,actor)
                    else:
                        # The known upload still exists and no complete object
                        # was observed. Repeating conditional completion remains
                        # safe; its persisted checksum cannot be changed.
                        async with self.store.database.begin() as conn:
                            current = await self._row(conn,session_id)
                            if current['revision'] != row['revision']:
                                raise Conflict('s3_upload_state_conflict')
                            await conn.execute(update(s.uploads).where(s.uploads.c.session_id == session_id).values(
                                state='uploading',error_code='',revision=current['revision']+1,updated_at=int(self.clock())))
                elif row['expires_at'] <= int(self.clock()):
                    row = await self._start_abort(row,actor,expired=True)
                    if row is not None:
                        await self._abort_remote(row,actor)
        return await self.status(session_id)
