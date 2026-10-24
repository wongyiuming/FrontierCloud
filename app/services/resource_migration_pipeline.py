"""One complete parent at a time; future parents need metadata, not copied bytes."""
from sqlalchemy import func,select

from app.services import resource_migration_schema as s
from app.services.resource_transfer_protocol import TransferError

COMMITTED=('committed','cleanup_pending','cleaned')


async def current(conn,task):
    row=(await conn.execute(select(s.units).where(s.units.c.task_id==task['task_id'],
        s.units.c.state.not_in((*COMMITTED,'cancelled'))).order_by(s.units.c.unit_id).limit(1))).mappings().first()
    return dict(row) if row is not None else None


async def verified(conn,task,unit):
    metadata=task['plan'].get('execution',{}).get('metadata')
    if metadata is None:
        # Older candidate journals retain their full-roster publication check.
        # New runners persist the enrollment proof before copying.
        count=await conn.scalar(select(func.count()).select_from(s.objects).where(s.objects.c.task_id==task['task_id']))
        if count!=task['object_count']:raise TransferError('migration_publish_copies_incomplete')
    elif (not isinstance(metadata,dict) or metadata.get('complete') is not True or type(metadata.get('version')) is not int or
            metadata['version']!=1 or type(metadata.get('objects')) is not int or type(metadata.get('bytes')) is not int or
            (metadata['objects'],metadata['bytes'])!=(task['object_count'],task['total_bytes'])):
        raise TransferError('migration_publish_metadata_incomplete')
    if unit is None:raise TransferError('migration_publish_copies_incomplete')
    owned=(s.objects.c.task_id==task['task_id'])&(s.objects.c.unit_id==unit['unit_id'])
    totals=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.objects.c.expected_bytes),0)).where(owned))).one()
    wrong=await conn.scalar(select(s.objects.c.logical_id).where(owned,
        (s.objects.c.state!='verified')|(s.objects.c.verified_bytes!=s.objects.c.expected_bytes)|
        (s.objects.c.source_sha256!=s.objects.c.target_sha256)|(s.objects.c.source_sha256=='')).limit(1))
    if tuple(totals)!=(unit['object_count'],unit['total_bytes']) or wrong:
        raise TransferError('migration_publish_copies_incomplete')
