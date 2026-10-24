"""Durable worker fencing and explicit migration controls.

This layer performs short database transactions only. Physical inventory,
transfer, publication and cleanup must use their own verified domain steps;
changing journal state alone is never a resource cutover or proof of cleanup.
"""
from __future__ import annotations

from dataclasses import dataclass
import time

from sqlalchemy import insert, select, update

from app.services.federation import protocol as p
from app.services import resource_migration_schema as s
from app.services.resource_pause_schema import RESUMABLE

ACTIVE = frozenset({'queued', 'copying', 'verifying', 'committing', 'cleanup_pending', 'cancelling'})
PAUSABLE = RESUMABLE
EDGES = {
    'queued': {'copying', 'failed'},
    'copying': {'verifying', 'failed'},
    'verifying': {'committing', 'failed'},
    'committing': {'copying', 'cleanup_pending', 'failed'},
    'cleanup_pending': {'completed', 'failed'},
    'cancelling': {'cancelled', 'cleanup_pending', 'failed'},
}


class Conflict(p.ProtocolError):
    pass


@dataclass(frozen=True)
class Lease:
    task_id: str
    runner_id: str
    epoch: int
    revision: int


def identifier(value):
    if not isinstance(value, str) or not p.IDENTIFIER.fullmatch(value):
        raise p.ProtocolError('迁移任务或执行者身份无效')
    return value


class Journal:
    def __init__(self, store, *, clock=time.time, traffic=None):
        self.store, self.clock = store, clock
        from app.services.business_traffic import monitor
        self.traffic = monitor if traffic is None else traffic

    async def _task(self, conn, task_id):
        identifier(task_id)
        node = await self.store.lock(conn)
        if node['role'] != 'Master':
            raise p.ProtocolError('只有 Master 可以控制资源迁移')
        row = (await conn.execute(select(s.tasks).where(
            s.tasks.c.task_id == task_id).with_for_update())).mappings().first()
        if row is None or row['master_id'] != node['node_id']:
            raise p.ProtocolError('迁移任务不存在或不属于当前节点')
        return dict(row)

    async def _runner(self, conn, task_id):
        row = (await conn.execute(select(s.runners).where(
            s.runners.c.task_id == task_id).with_for_update())).mappings().first()
        return dict(row) if row is not None else None

    def _ttl(self, ttl):
        if type(ttl) is not int or not 5 <= ttl <= 60:
            raise p.ProtocolError('迁移执行租约必须为 5..60 秒')
        return ttl

    async def claim(self, task_id: str, runner_id: str, *, ttl=15) -> Lease:
        identifier(runner_id)
        self._ttl(ttl)
        async with self.store.database.begin() as conn:
            task = await self._task(conn, task_id)
            if task['state'] not in ACTIVE:
                raise Conflict('迁移任务当前不能执行')
            current = await self._runner(conn, task_id)
            now = int(self.clock())
            if current is not None and current['expires_at'] > now:
                raise Conflict('迁移任务已有有效执行者')
            epoch = current['epoch'] + 1 if current else 1
            values = dict(runner_id=runner_id, epoch=epoch, expires_at=now + ttl)
            if current is None:
                await conn.execute(insert(s.runners).values(task_id=task_id, **values))
            else:
                await conn.execute(update(s.runners).where(s.runners.c.task_id == task_id).values(**values))
            revision = task['revision'] + 1
            await conn.execute(update(s.tasks).where(s.tasks.c.task_id == task_id).values(
                revision=revision, updated_at=now))
            await self.store.log(conn, 'migration_runner_claim', runner_id, task_id=task_id, epoch=epoch)
            return Lease(task_id, runner_id, epoch, revision)

    async def _fenced(self, conn, lease):
        task = await self._task(conn, lease.task_id)
        runner = await self._runner(conn, lease.task_id)
        if (runner is None or runner['runner_id'] != lease.runner_id or
                runner['epoch'] != lease.epoch or runner['expires_at'] <= int(self.clock()) or
                task['revision'] != lease.revision or task['state'] not in ACTIVE):
            raise Conflict('迁移执行租约已失效或任务状态已改变')
        return task

    async def renew(self, lease: Lease, *, ttl=15):
        self._ttl(ttl)
        async with self.store.database.begin() as conn:
            await self._fenced(conn, lease)
            await conn.execute(update(s.runners).where(s.runners.c.task_id == lease.task_id).values(
                expires_at=int(self.clock()) + ttl))

    async def release(self, lease: Lease):
        """Expire only this runner incarnation; never release task resources."""
        async with self.store.database.begin() as conn:
            await conn.execute(update(s.runners).where(s.runners.c.task_id==lease.task_id,
                s.runners.c.runner_id==lease.runner_id,s.runners.c.epoch==lease.epoch).values(expires_at=0))

    @staticmethod
    def _error_code(code):
        if (not isinstance(code,str) or not 1<=len(code)<=64
                or any(char not in 'abcdefghijklmnopqrstuvwxyz0123456789_' for char in code)):
            raise p.ProtocolError('迁移错误代码无效')
        return code

    async def note(self,lease,code):
        if code:self._error_code(code)
        async with self.store.database.begin() as conn:
            task=await self._fenced(conn,lease)
            if task['error_code']==code:return
            await conn.execute(update(s.tasks).where(s.tasks.c.task_id==lease.task_id).values(
                error_code=code,updated_at=int(self.clock())))
            await self.store.log(conn,'migration_execution_wait' if code else 'migration_execution_recovered',
                lease.runner_id,task_id=lease.task_id,error_code=code)

    async def fail(self,lease,code):
        self._error_code(code)
        async with self.store.database.begin() as conn:
            task=await self._fenced(conn,lease)
            failure=dict(state=task['state'],error_code=code,failed_at=int(self.clock()))
            await conn.execute(update(s.tasks).where(s.tasks.c.task_id==lease.task_id).values(
                state='failed',error_code=code,revision=task['revision']+1,updated_at=int(self.clock()),
                plan={**task['plan'],'execution_failure':failure}))
            await conn.execute(update(s.runners).where(s.runners.c.task_id==lease.task_id).values(expires_at=0))
            await self.store.log(conn,'migration_execution_failed',lease.runner_id,task_id=lease.task_id,**failure)

    async def checkpoint(self, lease: Lease, state: str, *, committed_bytes: int, error_code='') -> Lease:
        if (type(committed_bytes) is not int or committed_bytes < 0 or
                not isinstance(error_code, str) or len(error_code) > 64 or
                any(char not in 'abcdefghijklmnopqrstuvwxyz0123456789_' for char in error_code)):
            raise p.ProtocolError('迁移进度或错误代码无效')
        async with self.store.database.begin() as conn:
            task = await self._fenced(conn, lease)
            if state != task['state'] and state not in EDGES.get(task['state'], set()):
                raise Conflict('不允许跳过迁移阶段')
            if not task['committed_bytes'] <= committed_bytes <= task['total_bytes']:
                raise Conflict('迁移已提交进度不能回退或超过计划')
            # Committed bytes are backed by complete committed parent units,
            # never by copied bytes or client-reported progress.
            from sqlalchemy import func
            proven = int((await conn.scalar(select(func.coalesce(func.sum(s.units.c.total_bytes), 0)).where(
                s.units.c.task_id == task['task_id'],
                s.units.c.state.in_(['committed', 'cleanup_pending', 'cleaned'])))) or 0)
            if committed_bytes != proven:
                raise Conflict('迁移进度与完整目录提交日志不一致')
            if state == 'committing':
                from app.services import resource_migration_pipeline as pipeline
                from app.services.resource_transfer_protocol import TransferError
                try:await pipeline.verified(conn,task,await pipeline.current(conn,task))
                except TransferError:raise Conflict('当前完整目录尚未全部校验，禁止提交') from None
            if state=='copying' and task['state']=='committing':
                from app.services import resource_migration_pipeline as pipeline
                unit=await pipeline.current(conn,task)
                unack=await conn.scalar(select(s.units.c.unit_id).where(s.units.c.task_id==task['task_id'],
                    s.units.c.state.in_(pipeline.COMMITTED),s.units.c.native_acknowledged==0).limit(1))
                if unit is None or unit['state'] not in {'pending','copying','verified'} or unack:
                    raise Conflict('前一目录尚未完成切换确认')
            if state == 'cleanup_pending' and committed_bytes != task['total_bytes']:
                raise Conflict('完整目录尚未全部提交')
            if state == 'completed':
                # Only Finalization may complete after both signed native
                # censuses and atomic release of this task's own exclusions.
                raise Conflict('迁移完成必须通过源端和目标端的最终盘点')
            if state == 'cancelled':
                remaining = await conn.scalar(select(func.count()).select_from(s.units).where(
                    s.units.c.task_id == task['task_id'],
                    s.units.c.state.not_in(['cancelled', 'cleaned'])))
                if remaining:
                    raise Conflict('取消尚未完成存储端对账')
            revision = task['revision'] + 1
            await conn.execute(update(s.tasks).where(s.tasks.c.task_id == task['task_id']).values(
                state=state, committed_bytes=committed_bytes, error_code=error_code,
                revision=revision, updated_at=int(self.clock())))
            await self.store.log(conn, 'migration_checkpoint', lease.runner_id,
                task_id=task['task_id'], state=state, revision=revision)
            return Lease(lease.task_id, lease.runner_id, lease.epoch, revision)

    async def control(self, task_id: str, expected_revision: int, action: str, actor: str):
        if type(expected_revision) is not int or expected_revision < 1:
            raise p.ProtocolError('迁移任务版本无效')
        if action not in {'pause', 'resume', 'traffic_pause', 'traffic_resume', 'cancel','retry'}:
            raise p.ProtocolError('未知迁移操作')
        async with self.store.database.begin() as conn:
            task = await self._task(conn, task_id)
            if task['revision'] != expected_revision:
                raise Conflict('迁移任务已改变，请刷新')
            state, resume, reason = task['state'], task['resume_state'], task['pause_reason']
            if action in {'pause', 'traffic_pause'}:
                if state == 'paused' and action == 'pause':
                    reason = 'manual'
                elif state in PAUSABLE:
                    resume, state = state, 'paused'
                    reason = 'manual' if action == 'pause' else 'traffic'
                else:
                    raise Conflict('当前阶段不能暂停')
            elif action in {'resume', 'traffic_resume'}:
                if state != 'paused' or (action == 'traffic_resume' and
                        (reason != 'traffic' or not self.traffic.snapshot()['can_run'])):
                    raise Conflict('手动暂停必须手动恢复；自动恢复需要低流量证明')
                state, resume, reason = resume, '', ''
            elif action=='retry':
                old=task['plan'].get('execution_failure',{}).get('state')
                if state=='failed' and old in ACTIVE:
                    state=old
                elif state in {'admitting','prepared','admission_aborting','cancelling'} and task['error_code']:
                    runner=await self._runner(conn,task_id)
                    if runner and runner['expires_at']>int(self.clock()):
                        raise Conflict('迁移准备仍有有效执行者')
                else:
                    raise Conflict('当前任务没有可重试的执行失败')
            else:
                if state not in PAUSABLE | {'paused', 'failed'}:
                    raise Conflict('当前阶段不能取消；已发布资源须完成对账')
                from app.services.resource_cancel_intent import KEY,intent
                cancellation=await intent(conn,task,self.clock())
                # Fences and reservations survive cancellation requests. Only
                # verified storage abort/cleanup may release them later.
                state, resume, reason = 'cancelling', '', ''
            revision = task['revision'] + 1
            await conn.execute(update(s.tasks).where(s.tasks.c.task_id == task_id).values(
                state=state, resume_state=resume, pause_reason=reason,
                revision=revision,error_code='' if action in {'retry','cancel'} else task['error_code'],updated_at=int(self.clock()),
                **({'plan':{**task['plan'],KEY:cancellation}} if action=='cancel' else {})))
            # The old worker cannot write after any control change. Expire its
            # row, retaining the epoch so a future worker cannot reuse it.
            await conn.execute(update(s.runners).where(s.runners.c.task_id == task_id).values(expires_at=0))
            await self.store.log(conn, 'migration_' + action, actor,
                task_id=task_id, state=state, revision=revision)
            return revision
