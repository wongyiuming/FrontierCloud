"""One bounded metadata-only S3 recovery worker, independent of node heartbeats."""
import asyncio
import logging
import time

from sqlalchemy import and_, literal, or_, select, union_all, update

from app.services import s3_schema as s
from app.services.federation import schema as fs
from app.services.s3_signing import S3Error
from app.services.s3_uploads import Uploads
from app.services.s3_objects import Objects
from app.services.upload_claims import UploadBusy

logger = logging.getLogger('frontiercloud.s3.recovery')


class Recovery:
    def __init__(self,store,*,service=None,clock=time.time):
        self.store,self.clock = store,clock
        self.service = Uploads(store,clock=clock) if service is None else service
        self.objects = Objects(store,clock=clock,control_factory=self.service.control_factory,lease_root=self.service.lease_root)

    async def once(self,*,limit=4):
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError('invalid_s3_recovery_batch')
        from app.services.site_control import hard_fence_active
        if hard_fence_active():
            return dict(processed=0,closed=0,pending=0)
        now = int(self.clock())
        async with self.store.database.connect() as conn:
            role = await conn.scalar(select(fs.identity.c.role).where(fs.identity.c.singleton == 1))
            if role != 'Master':
                return dict(processed=0,closed=0,pending=0)
            uploads = select(literal('upload').label('kind'),s.uploads.c.session_id.label('identity'),s.uploads.c.state,
                s.uploads.c.revision,s.uploads.c.updated_at).where(or_(and_(s.uploads.c.state.in_(('reserved','uploading')),s.uploads.c.expires_at <= now),
                    s.uploads.c.state.in_(('creating','completing','uncertain')),
                    and_(s.uploads.c.state == 'aborting',s.uploads.c.grant_until <= now)))
            objects = select(literal('object').label('kind'),s.objects.c.media_id.label('identity'),s.objects.c.state,
                s.objects.c.revision,s.objects.c.updated_at).where(s.objects.c.state == 'deleting',s.objects.c.grant_until <= now)
            candidates = union_all(uploads,objects).subquery('s3_recoverable')
            rows = [dict(row) for row in (await conn.execute(select(candidates)
                .order_by(candidates.c.updated_at,candidates.c.kind,candidates.c.identity).limit(limit))).mappings()]
        closed = 0
        for row in rows:
            error = ''
            try:
                async with asyncio.timeout(45):
                    status = (await self.service.reconcile(row['identity']) if row['kind'] == 'upload'
                              else await self.objects.delete(row['identity']))
                closed += int(status['state'] in ('published','aborted','deleted'))
            except UploadBusy:
                pass
            except (S3Error,TimeoutError) as failure:
                error = getattr(failure,'code','s3_recovery_timeout')
                if not isinstance(error,str) or len(error) > 50 or any(char not in 'abcdefghijklmnopqrstuvwxyz0123456789_' for char in error):
                    error = 's3_recovery_failed'
            # Failed/ambiguous keys cannot starve every later session. No state,
            # grant, expiry, reservation, checksum or worker revision is reset.
            async with self.store.database.begin() as conn:
                await self.service.members._master(conn)
                table = s.uploads if row['kind'] == 'upload' else s.objects
                identity = table.c.session_id if row['kind'] == 'upload' else table.c.media_id
                current = (await conn.execute(select(table).where(identity == row['identity'])
                                              .with_for_update())).mappings().first()
                if current and current['revision'] == row['revision'] and current['state'] == row['state']:
                    values = dict(updated_at=int(self.clock()))
                    if error:
                        if row['kind'] == 'object':
                            values['error_code'] = error
                        else:
                            stage = ('complete' if current['state'] == 'completing' or current['error_code'].startswith('complete_')
                                     else 'create' if not current['provider_upload_id'] else 'cleanup')
                            values['error_code'] = stage+'_'+error
                    await conn.execute(update(table).where(identity == row['identity']).values(**values))
        return dict(processed=len(rows),closed=closed,pending=len(rows)-closed)

    async def run(self):
        while True:
            try:
                await self.once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Status retains the transaction. Do not reflect arbitrary SDK,
                # SQL or transport exception strings into logs or the Admin UI.
                logger.warning('S3 recovery remains pending; error_type=%s',type(error).__name__)
            await asyncio.sleep(60)
