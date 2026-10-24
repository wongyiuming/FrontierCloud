"""Persistent migration exclusions for ordinary resource mutations.

Call within the mutation transaction or its existing process-local media lock.
Admission must hold that same media lock exclusively and the node identity row
while installing scopes. Read APIs do not consult these write exclusions.
"""
import time

from sqlalchemy import and_, literal, or_, select

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p
from app.services.federation import schema as fs


class FenceConflict(p.ProtocolError):
    def __init__(self,code):
        self.code = code
        messages = {'migration_scope_conflict':'此目录正在资源迁移，暂时不能修改',
                    'migration_member_draining':'此节点正在排空，暂时不接收新上传',
                    'migration_member_busy':'此节点仍有资源迁移或待恢复事务，暂时不能修改节点配置',
                    'migration_node_busy':'节点仍有资源迁移或待恢复事务，不能重新初始化'}
        super().__init__(messages[code])


async def _present(conn,table,condition):
    return (await conn.execute(select(literal(1)).select_from(table).where(condition)
                               .limit(1).with_for_update())).first() is not None


async def lock_node(conn):
    row = (await conn.execute(select(fs.identity.c.node_id).where(fs.identity.c.singleton == 1)
                              .with_for_update())).first()
    if row is None:
        raise p.ProtocolError('节点身份尚未初始化')


async def new_upload(conn,member_id,kind,path):
    await lock_node(conn)
    from app.services.node_retirement_fences import clear
    await clear(conn,member_id=member_id)
    if await _present(conn,s.drains,s.drains.c.member_id == member_id):
        raise FenceConflict('migration_member_draining')
    await paths(conn,kind,[(path,False)])


async def preparing(conn, *, owner_task_id=None):
    # Until all scopes are installed, a partial list is insufficient authority
    # for ordinary mutations. This conservative short-lived gate survives crash.
    for table, states in ((s.tasks,['admitting','admission_aborting']),
                          (s.peer_reservations,['preparing'])):
        if await _present(conn,table,and_(table.c.state.in_(states),
                table.c.task_id != owner_task_id if owner_task_id else True)):
            raise FenceConflict('migration_scope_conflict')


async def cancellation_census(conn,*,member_id=None,owner_task_id=None):
    rows=(await conn.execute(select(s.peer_reservations.c.payload).where(
        s.peer_reservations.c.side=='source',s.peer_reservations.c.state=='active',
        s.peer_reservations.c.member_id==member_id if member_id is not None else True,
        s.peer_reservations.c.task_id!=owner_task_id if owner_task_id else True))).scalars()
    for payload in rows:
        # Presence, including malformed/null state, keeps the durable fence.
        # Only the authenticated task finalizer may eventually release it.
        if 'cancel_census' in payload.get('transfer',{}):raise FenceConflict('migration_scope_conflict')


async def master_cancellation_census(conn,*,member_id=None,kind=None,selections=(),owner_task_id=None):
    condition=and_(s.tasks.c.state=='cancelling',
        s.tasks.c.task_id!=owner_task_id if owner_task_id else True)
    if member_id is not None:condition=and_(condition,s.tasks.c.source_id==member_id)
    elif kind=='media':
        matches=[]
        for path,directory in selections:
            matches.append(fs.global_media.c.media_path.startswith(path+'/',autoescape=True) if directory else fs.global_media.c.media_path==path)
        condition=and_(condition,select(literal(1)).select_from(fs.global_media).where(
            fs.global_media.c.storage_member_id==s.tasks.c.source_id,or_(*matches)).exists())
    else:
        # Recording callers identify their storage member explicitly.
        return
    after=''
    while True:
        rows=(await conn.execute(select(s.tasks.c.task_id,s.tasks.c.plan).where(condition,s.tasks.c.task_id>after)
            .order_by(s.tasks.c.task_id).limit(16))).mappings().all()
        for row in rows:
            if 'cancel_current_census' in row['plan']:raise FenceConflict('migration_scope_conflict')
        if len(rows)<16:return
        after=rows[-1]['task_id']


async def paths(conn,kind,selections,*,member_id=None,owner_task_id=None):
    """Files fence their parent; directories also fence descendant units."""
    if kind not in {'media','recording'} or not selections or len(selections) > 1000:
        raise p.ProtocolError('资源变更范围无效')
    await lock_node(conn)
    from app.services.node_retirement_fences import clear
    await clear(conn,member_id=member_id)
    await preparing(conn,owner_task_id=owner_task_id)
    await cancellation_census(conn,member_id=member_id,owner_task_id=owner_task_id)
    conditions = []
    for path,directory in selections:
        if (not isinstance(path,str) or not 0 < len(path) <= 1024 or '\\' in path or
                path.startswith('/') or path.endswith('/') or type(directory) is not bool or
                any(part in {'','.','..'} for part in path.split('/'))):
            raise p.ProtocolError('资源变更路径无效')
        if directory:
            conditions.append(or_(s.scopes.c.parent_path == path,
                s.scopes.c.parent_path.startswith(path+'/',autoescape=True)))
        else:
            if '/' not in path:
                raise p.ProtocolError('资源文件缺少父目录')
            conditions.append(s.scopes.c.parent_path == path.rsplit('/',1)[0])
    for offset in range(0,len(conditions),64):
        await master_cancellation_census(conn,member_id=member_id,kind=kind,
            selections=selections[offset:offset+64],owner_task_id=owner_task_id)
        condition = and_(s.scopes.c.resource_kind == kind,or_(*conditions[offset:offset+64]))
        if member_id is not None:
            condition = and_(condition,s.scopes.c.member_id == member_id)
        if await _present(conn,s.scopes,condition):
            raise FenceConflict('migration_scope_conflict')


async def recording(conn,member_id,user_id,*,upload=False):
    if not isinstance(user_id,str) or not p.IDENTIFIER.fullmatch(user_id):
        raise p.ProtocolError('录音用户身份无效')
    await lock_node(conn)
    from app.services.node_retirement_fences import clear
    await clear(conn,member_id=member_id)
    await preparing(conn)
    await cancellation_census(conn,member_id=member_id)
    await master_cancellation_census(conn,member_id=member_id)
    if upload and await _present(conn,s.drains,s.drains.c.member_id == member_id):
        raise FenceConflict('migration_member_draining')
    condition = and_(s.scopes.c.member_id == member_id,s.scopes.c.resource_kind == 'recording',
        s.scopes.c.parent_path.endswith('/'+user_id,autoescape=True))
    if await _present(conn,s.scopes,condition):
        raise FenceConflict('migration_scope_conflict')


async def member_clear(conn,member_id,*,readers=False):
    await lock_node(conn)
    from app.services.node_retirement_fences import clear
    await clear(conn,member_id=member_id)
    await _migration_member_clear(conn,member_id,readers=readers)


async def _migration_member_clear(conn,member_id,*,readers=False):
    # The terminal retirement controller already owns and verifies its exact
    # durable fence. Ordinary callers must use member_clear above.
    for table,condition in (
        (s.drains,s.drains.c.member_id == member_id),(s.scopes,s.scopes.c.member_id == member_id),
        (s.capacity,s.capacity.c.member_id == member_id),
        (s.peer_reservations,and_(s.peer_reservations.c.member_id == member_id,
            s.peer_reservations.c.state.not_in(['aborted','released']))),
        (s.tasks,and_(or_(s.tasks.c.source_id == member_id,s.tasks.c.target_id == member_id),
                      s.tasks.c.state.not_in(['completed','cancelled']))),
        (s.storage_journal,and_(or_(s.storage_journal.c.source_id == member_id,s.storage_journal.c.target_id == member_id),
            s.storage_journal.c.state.in_(['receiving','verified','publishing','deleting','aborting']))),
    ):
        if await _present(conn,table,condition):
            raise FenceConflict('migration_member_busy')
    if readers and await _present(conn,s.read_leases,and_(s.read_leases.c.member_id == member_id,
                                                        s.read_leases.c.expires_at > int(time.time()))):
        raise FenceConflict('migration_member_busy')


async def node_clear(conn):
    await lock_node(conn)
    from app.services.node_retirement_fences import clear
    await clear(conn,reset=True)
    for table,condition in (
        (s.drains,True),(s.scopes,True),(s.capacity,True),
        (s.peer_reservations,s.peer_reservations.c.state.not_in(['aborted','released'])),
        (s.tasks,s.tasks.c.state.not_in(['completed','cancelled'])),
        (s.storage_journal,s.storage_journal.c.state.in_(['receiving','verified','publishing','deleting','aborting'])),
        (s.read_leases,s.read_leases.c.expires_at > int(time.time())),
    ):
        if await _present(conn,table,condition):
            raise FenceConflict('migration_node_busy')
