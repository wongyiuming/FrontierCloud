"""Persist cancellation routing before any native abort or publication request."""
from sqlalchemy import func,select

from app.services import resource_migration_schema as s
from app.services.resource_migration_journal import Conflict
from app.services.resource_transfer_protocol import TransferError

KEY='cancel_request'
PUBLICATION=frozenset({'committing','cleanup_pending'})


async def intent(conn,task,now):
    state=task['resume_state'] if task['state']=='paused' else task['plan'].get('execution_failure',{}).get('state') if task['state']=='failed' else task['state']
    if state not in {'queued','copying','verifying','committing','cleanup_pending'}:raise Conflict('迁移取消来源阶段无效')
    advanced=state in PUBLICATION or task['committed_bytes']>0 or await conn.scalar(select(s.units.c.unit_id).where(
        s.units.c.task_id==task['task_id'],s.units.c.state.not_in(['pending','copying','verified','cancelled'])).limit(1)) is not None
    if advanced:
        from app.services.resource_cancel_roster import validate
        try:await validate(conn,task)
        except TransferError:raise Conflict('迁移发布清单不完整，保留资源等待对账') from None
    return dict(version=1,path='publication' if advanced else 'prepublication',from_state=state,requested_at=int(now))


def published(task):
    value=task['plan'].get(KEY)
    if value is None and KEY not in task['plan']:return False
    if (not isinstance(value,dict) or value.keys()!={'version','path','from_state','requested_at'} or type(value['version']) is not int
            or value['version']!=1 or not isinstance(value['path'],str) or value['path'] not in {'publication','prepublication'}
            or not isinstance(value['from_state'],str) or value['from_state'] not in {'prepared','queued','copying','verifying','committing','cleanup_pending'}
            or type(value['requested_at']) is not int or value['requested_at']<=0):
        raise TransferError('migration_cancel_intent_invalid')
    return value['path']=='publication'
