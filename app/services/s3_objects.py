"""Published S3 read grants and explicit, recoverable managed deletion.

HTTP callers authorize public visibility or Admin access before invoking reads.
Only catalog-owned immutable keys are signed; no media is fetched by the Master.
"""
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
import time
from urllib.parse import quote

from sqlalchemy import delete, select, text, update

from app.services import media_crypto, resource_fences, resource_pool, s3_schema as s
from app.services.federation import protocol as p, schema as fs
from app.services.media_catalog_cache import invalidate_media_catalog
from app.services.media_manager import ensure_media_mutations_ready, media_mutation_lock
from app.services.s3_control import Control, ControlError
from app.services.s3_members import Members, Conflict
from app.services.s3_signing import S3Error
from app.services.s3_uploads import Uploads
from app.services.upload_claims import claim

MIME_TYPES = {'.mp3':'audio/mpeg','.m4a':'audio/mp4','.flac':'audio/flac','.wav':'audio/wav',
              '.mp4':'video/mp4','.webm':'video/webm','.mkv':'video/x-matroska'}


def identifier(value):
    if not isinstance(value,str) or not p.OBJECT_ID.fullmatch(value):
        raise S3Error('s3_object_identity_invalid')
    return value


class Objects:
    def __init__(self,store,*,clock=time.time,control_factory=Control,lease_root=None):
        self.store,self.clock,self.control_factory,self.lease_root = store,clock,control_factory,lease_root
        self.members = Members(store,clock=clock)
        self.uploads = Uploads(store,clock=clock,control_factory=control_factory,lease_root=lease_root)

    async def _row(self,conn,media_id):
        await self.members._master(conn)
        row = (await conn.execute(select(s.objects).where(s.objects.c.media_id == identifier(media_id))
                                  .with_for_update())).mappings().first()
        if row is None:
            raise S3Error('s3_object_missing')
        source = (await conn.execute(select(fs.global_media).where(fs.global_media.c.media_id == media_id)
                                     .with_for_update())).mappings().first()
        if row['state'] == 'deleted':
            if source is not None:
                raise Conflict('s3_deleted_catalog_conflict')
            return dict(row),None,None
        member = await self.members._row(conn,row['member_id'])
        if (source is None or source['storage_member_id'] != row['member_id'] or source['object_id'] != row['object_id'] or
            source['size_bytes'] != row['size_bytes'] or source['etag'] != row['etag']):
            raise Conflict('s3_object_catalog_conflict')
        return dict(row),dict(source),member

    async def grant(self,media_id,*,method='GET',download=False):
        if method not in ('GET','HEAD') or type(download) is not bool:
            raise S3Error('s3_read_purpose_invalid')
        async with self.store.database.begin() as conn:
            row,source,member = await self._row(conn,media_id)
            if row['state'] != 'active' or source['state'] != 'active':
                raise S3Error('s3_object_missing')
            descriptor = await media_crypto.encryption_for_id(conn,media_id)
            if bool(descriptor) != bool(row['content_encrypted']) or descriptor is not None and descriptor.ciphertext_size != row['size_bytes']:
                raise Conflict('s3_encryption_descriptor_missing')
            name = source['media_path'].rsplit('/',1)[-1]
            suffix = PurePosixPath(name).suffix.lower()
            if suffix not in MIME_TYPES or not name or len(name) > 255 or any(ord(char) < 32 or ord(char) == 127 for char in name):
                raise S3Error('s3_download_name_invalid')
            query = [('response-content-type','application/octet-stream' if descriptor else MIME_TYPES[suffix])]
            if row['version_id']:
                query.append(('versionId',row['version_id']))
            if download:
                query.append(('response-content-disposition','attachment; filename="media'+suffix+'"; filename*=UTF-8\'\''+quote(name,safe='')))
            grant = self.uploads._signer(member).presign(method,row['object_key'],query=query)
            await conn.execute(update(s.objects).where(s.objects.c.media_id == media_id).values(grant_until=max(row['grant_until'],grant.expires_at)))
            return grant

    @asynccontextmanager
    async def _control(self,member):
        control = self.control_factory(self.uploads._signer(member))
        try:
            yield control
        finally:
            await control.close()

    def _verified(self,row,head):
        if (head['bytes'] != row['size_bytes'] or head['checksum'] != row['checksum'] or head['session_id'] != row['session_id'] or
            head['object_id'] != row['object_id'] or head['etag'] != row['etag'] or head['version_id'] != row['version_id']):
            raise Conflict('s3_delete_identity_mismatch')

    async def delete(self,media_id,*,actor='system'):
        identifier(media_id)
        with claim(media_id,root=self.lease_root):
            async with media_mutation_lock.shared(),resource_pool.storage_write_lock:
                ensure_media_mutations_ready()
                async with self.store.database.begin() as conn:
                    row,source,member = await self._row(conn,media_id)
                    if row['state'] == 'deleted':
                        return dict(state='deleted',media_id=media_id)
                    await resource_fences.paths(conn,'media',[(source['media_path'],False)])
                    if source['state'] != 'pending_delete':
                        await conn.execute(update(fs.global_media).where(fs.global_media.c.media_id == media_id).values(
                            state='pending_delete',updated_at=int(self.clock())))
                    if row['state'] != 'deleting':
                        await conn.execute(update(s.objects).where(s.objects.c.media_id == media_id).values(
                            state='deleting',revision=row['revision']+1,updated_at=int(self.clock())))
                        await conn.execute(update(s.members).where(s.members.c.member_id == row['member_id']).values(catalog_revision=s.members.c.catalog_revision+1))
                        await self.store.log(conn,'s3_media_delete_requested',actor,media_id=media_id)
                        row.update(state='deleting',revision=row['revision']+1)
            await invalidate_media_catalog()
            if row['grant_until'] > int(self.clock()):
                return dict(state='deleting',media_id=media_id,not_before=row['grant_until'])
            try:
                async with self._control(member) as control:
                    try:
                        head = await control.head(row['object_key'],version_id=row['version_id'])
                    except ControlError as error:
                        if error.code not in ('s3_not_found','s3_object_missing'):
                            raise
                    else:
                        self._verified(row,head)
                        await control.delete(row['object_key'],version_id=row['version_id'])
                        try:
                            await control.head(row['object_key'],version_id=row['version_id'])
                        except ControlError as error:
                            if error.code not in ('s3_not_found','s3_object_missing'):
                                raise
                        else:
                            raise Conflict('s3_delete_pending')
                await self._finish(row,actor)
            except S3Error as error:
                async with self.store.database.begin() as conn:
                    current,_,_ = await self._row(conn,media_id)
                    if current['state'] == 'deleting':
                        await conn.execute(update(s.objects).where(s.objects.c.media_id == media_id).values(
                            error_code=error.code,updated_at=int(self.clock())))
                raise
        return dict(state='deleted',media_id=media_id)

    async def _finish(self,row,actor):
        async with media_mutation_lock.shared(),resource_pool.storage_write_lock:
            ensure_media_mutations_ready()
            async with self.store.database.begin() as conn:
                current,source,member = await self._row(conn,row['media_id'])
                if current['state'] == 'deleted':
                    return
                if current['state'] != 'deleting' or current['revision'] != row['revision'] or source['state'] != 'pending_delete':
                    raise Conflict('s3_delete_state_conflict')
                if member['used_bytes'] < row['size_bytes']:
                    raise Conflict('s3_used_capacity_inconsistent')
                await media_crypto.forget_encryption(conn,row['media_id'])
                for table in ('media_lyric_links','media_playback_events','media_playback_stats'):
                    await conn.execute(text('DELETE FROM '+table+' WHERE media_id=:id'),{'id':row['media_id']})
                await conn.execute(delete(fs.global_media).where(fs.global_media.c.media_id == row['media_id']))
                await conn.execute(update(s.objects).where(s.objects.c.media_id == row['media_id']).values(
                    state='deleted',revision=current['revision']+1,error_code='',updated_at=int(self.clock())))
                await conn.execute(update(s.members).where(s.members.c.member_id == row['member_id']).values(
                    used_bytes=member['used_bytes']-row['size_bytes'],catalog_revision=s.members.c.catalog_revision+1,updated_at=int(self.clock())))
                await self.store.log(conn,'s3_media_deleted',actor,media_id=row['media_id'])
        await invalidate_media_catalog()
