"""Prove admitted metadata without inventing copy completion during cancellation."""
from sqlalchemy import and_,func,or_,select

from app.services import resource_migration_schema as s
from app.services.resource_transfer_protocol import TransferError


async def validate(conn,task):
    owner=s.objects.c.task_id==task['task_id']
    counts=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.objects.c.expected_bytes),0)).where(owner))).one()
    if tuple(counts)!=(task['object_count'],task['total_bytes']):raise TransferError('migration_cancel_metadata_roster_required')
    metadata=task['plan'].get('execution',{}).get('metadata')
    complete=and_(s.objects.c.verified_bytes==s.objects.c.expected_bytes,s.objects.c.source_sha256!='',
        s.objects.c.source_sha256==s.objects.c.target_sha256)
    if metadata is None:
        valid=and_(complete,s.objects.c.state.in_(['verified','published','cleanup_pending','cleaned','cancelled']))
    else:
        if (not isinstance(metadata,dict) or metadata.keys()!={'version','after','objects','bytes','complete'} or
                type(metadata['version']) is not int or metadata['version']!=1 or metadata['complete'] is not True or
                not isinstance(metadata['after'],str) or any(type(metadata[name]) is not int for name in ('objects','bytes')) or
                (metadata['objects'],metadata['bytes'])!=(task['object_count'],task['total_bytes'])):
            raise TransferError('migration_cancel_metadata_roster_required')
        valid=or_(and_(s.objects.c.state.in_(['pending','copying','cancelled']),s.objects.c.verified_bytes==0,
            s.objects.c.source_sha256!='',s.objects.c.target_sha256==''),
            and_(complete,s.objects.c.state.in_(['verified','published','cleanup_pending','cleaned','cancelled'])))
        # Each materialized object must still be one admitted metadata record.
        planned=select(s.plan_bindings.c.logical_id).where(s.plan_bindings.c.plan_id==s.objects.c.task_id,
            s.plan_bindings.c.logical_id==s.objects.c.logical_id,s.plan_bindings.c.unit_id==s.objects.c.unit_id,
            s.plan_bindings.c.resource_kind==s.objects.c.resource_kind,s.plan_bindings.c.source_object==s.objects.c.source_object,
            s.plan_bindings.c.expected_bytes==s.objects.c.expected_bytes,s.plan_bindings.c.source_sha256==s.objects.c.source_sha256)
        if await conn.scalar(select(s.objects.c.logical_id).where(owner,~planned.exists()).limit(1)):
            raise TransferError('migration_cancel_metadata_roster_changed')
    if await conn.scalar(select(s.objects.c.logical_id).where(owner,~valid).limit(1)):
        raise TransferError('migration_cancel_copy_journal_changed')
    advanced=select(s.units.c.unit_id).where(s.units.c.task_id==s.objects.c.task_id,s.units.c.unit_id==s.objects.c.unit_id,
        s.units.c.state.in_(['publishing','published','committed','cleanup_pending','cleaned']))
    if await conn.scalar(select(s.objects.c.logical_id).where(owner,advanced.exists(),~complete).limit(1)):
        raise TransferError('migration_cancel_publication_journal_changed')
    if task['state']=='committing' and not task['committed_bytes']:
        from app.services import resource_migration_pipeline as pipeline
        await pipeline.verified(conn,task,await pipeline.current(conn,task))
