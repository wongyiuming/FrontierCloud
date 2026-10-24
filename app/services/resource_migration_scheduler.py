"""One serial, restartable metadata driver; Source and Target carry all bytes."""
import asyncio
from contextlib import suppress
import logging
import re
import time
import uuid

import httpx
from sqlalchemy import and_,or_,select,update

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p
from app.services.resource_migration_admission import Admission
from app.services.resource_migration_journal import ACTIVE,PAUSABLE,Conflict,Journal
from app.services.resource_transfer_master import TransferMaster
from app.services.resource_publish_master import Publication
from app.services.resource_cutover_master import COMMITTED,Cutover
from app.services.resource_cleanup_master import Cleanup
from app.services.resource_finalize_master import Finalization
from app.services.resource_transfer_cancel import Cancellation
from app.services.resource_cancel_execution import CancellationExecution
from app.services.resource_cancel_intent import published as published_cancellation
from app.services.resource_transfer_enrollment import Enrollment
from app.services import resource_migration_pipeline as pipeline

logger=logging.getLogger('frontiercloud.migration')
RETRY_SECONDS=10
TRANSIENT=frozenset({'migration_peer_timeout','migration_peer_connect_failed','migration_peer_unavailable','migration_waiting_low_traffic',
    'migration_waiting_reader','migration_waiting_writer','migration_admission_peer_timeout',
    'migration_admission_peer_unavailable','migration_transfer_direct_timeout','migration_transfer_direct_connect_failed',
    'migration_transfer_direct_unavailable','migration_transfer_source_busy','migration_transfer_target_busy',
    'migration_transfer_window_expired','migration_transfer_peer_offline'})
PREPARING=frozenset({'admitting','prepared','admission_aborting'})


def error_code(error):
    if isinstance(error,httpx.TimeoutException):return 'migration_peer_timeout'
    if isinstance(error,httpx.ConnectError):
        return ('migration_peer_tls_verification_failed' if 'CERTIFICATE_VERIFY_FAILED' in str(error)
            else 'migration_peer_connect_failed')
    if isinstance(error,(httpx.HTTPError,OSError,TimeoutError)):return 'migration_peer_unavailable'
    if isinstance(error,p.ProtocolError) and re.fullmatch(r'migration_[a-z0-9_]{1,54}',str(error)):
        code=str(error)
        if code.endswith(('_waiting_low_traffic','_low_traffic')):return 'migration_waiting_low_traffic'
        if code.endswith(('_busy','_waiting_writer')):return 'migration_waiting_writer'
        if code.endswith('_waiting_reader'):return 'migration_waiting_reader'
        return code
    return 'migration_execution_internal_error'


class Scheduler:
    def __init__(self,store,transport,*,traffic=None,wall=time.time,fenced=None):
        from app.services.business_traffic import monitor
        from app.services.site_control import hard_fence_active
        self.store,self.transport,self.wall=store,transport,wall
        self.traffic=monitor if traffic is None else traffic
        self.fenced=hard_fence_active if fenced is None else fenced
        options=dict(traffic=self.traffic,wall=wall)
        self.journal=Journal(store,clock=wall,traffic=self.traffic)
        self.admission=Admission(store,transport,**options)
        self.transfer=TransferMaster(store,transport,**options)
        self.enrollment=Enrollment(self.transfer)
        self.publication=Publication(store,transport,**options)
        self.cutover=Cutover(store,transport,**options)
        self.cleanup=Cleanup(store,transport,**options)
        self.finalization=Finalization(store,transport,**options)
        self.cancellation=Cancellation(store,transport,**options)
        self.cancel_execution=CancellationExecution(store,transport,**options)
        self.runner_id=uuid.uuid4().hex
        self.lease=None
        self.task=None
        self.wakeup=asyncio.Event()
        self.lock=asyncio.Lock()

    def start(self):
        if self.task is None or self.task.done():
            self.task=asyncio.create_task(self.run(),name='resource-migration-scheduler')
        self.wakeup.set()

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):await self.task
            self.task=None
        await self._release()

    async def _release(self):
        lease,self.lease=self.lease,None
        if lease is not None:await self.journal.release(lease)

    async def _candidate(self):
        now=int(self.wall())
        unfinished=select(s.units.c.unit_id).where(s.units.c.task_id==s.tasks.c.task_id,s.units.c.state!='cleaned')
        due=unfinished.where(s.units.c.cleanup_after<=now)
        cancel_unsettled=select(s.units.c.unit_id).where(s.units.c.task_id==s.tasks.c.task_id,
            s.units.c.state.not_in((*COMMITTED,'cancelled'))|(s.units.c.native_acknowledged==0))
        cancel_cleanup=select(s.units.c.unit_id).where(s.units.c.task_id==s.tasks.c.task_id,s.units.c.state.in_(['committed','cleanup_pending']))
        cancel_due=cancel_cleanup.where(s.units.c.cleanup_after<=now)
        query=select(s.tasks).outerjoin(s.runners,s.tasks.c.task_id==s.runners.c.task_id).where(
            s.tasks.c.master_id==self.store.node['node_id'],
            or_(s.tasks.c.state.in_(ACTIVE|PREPARING),and_(s.tasks.c.state=='paused',s.tasks.c.pause_reason=='traffic',
                self.traffic.snapshot()['can_run'])),
            or_(s.runners.c.task_id.is_(None),s.runners.c.expires_at<=now),
            or_(s.tasks.c.state.not_in(PREPARING),self.traffic.snapshot()['can_run']),
            or_(s.tasks.c.error_code=='',and_(s.tasks.c.error_code.in_(TRANSIENT),s.tasks.c.updated_at<=now-RETRY_SECONDS)),
            or_(s.tasks.c.state!='cleanup_pending',~unfinished.exists(),due.exists()),
            or_(s.tasks.c.state!='cancelling',cancel_unsettled.exists(),~cancel_cleanup.exists(),cancel_due.exists())
        ).order_by(s.tasks.c.updated_at,s.tasks.c.task_id).limit(1)
        async with self.store.database.connect() as conn:
            row=(await conn.execute(query)).mappings().first()
        return dict(row) if row else None

    async def _task(self):
        async with self.store.database.begin() as conn:return await self.journal._fenced(conn,self.lease)

    async def _delay(self,code):
        await self.journal.note(self.lease,code)
        await self._release()

    async def _preparation_error(self,task,error):
        code=error_code(error)
        async with self.store.database.begin() as conn:
            current=await self.journal._task(conn,task['task_id'])
            if current['state'] not in PREPARING|{'cancelling'}:return
            if current['error_code']!=code:
                await conn.execute(update(s.tasks).where(s.tasks.c.task_id==task['task_id']).values(
                    error_code=code,updated_at=int(self.wall()),revision=current['revision']+1))
                await self.store.log(conn,'migration_preparation_deferred','scheduler',task_id=task['task_id'],error_code=code)

    async def tick(self):
        async with self.lock:
            if self.store.node.get('role')!='Master' or self.fenced():
                await self._release();return False
            task=None
            try:
                if self.lease is not None:task=await self._task()
                else:
                    task=await self._candidate()
                    if task is None:return False
                    task_id=task['task_id']
                    if task['state']=='paused':
                        if not self.traffic.snapshot()['can_run']:return False
                        await self.journal.control(task_id,task['revision'],'traffic_resume','scheduler')
                        return True
                    if task['state'] in PREPARING:
                        if not self.traffic.snapshot()['can_run']:return False
                        if task['state']=='admitting':await self.admission.prepare(task_id,task['plan_sha256'],'scheduler')
                        elif task['state']=='admission_aborting':await self.admission.cancel_unstarted(task_id,'scheduler')
                        else:await self.transfer.preflight(task_id,'scheduler')
                        return True
                    if task['state']=='cancelling' and not published_cancellation(task):
                        result=await self.cancellation.advance(task_id,'scheduler')
                        waits=set(result.get('peers',{}).values())&{'waiting_reader','waiting_writer','waiting_low_traffic'}
                        if waits:
                            code='migration_'+sorted(waits)[0]
                            await self._preparation_error(task,p.ProtocolError(code));return False
                        return True
                    self.lease=await self.journal.claim(task_id,self.runner_id,ttl=60)
                    task=await self._task()
                if task['state'] in PAUSABLE and not self.traffic.snapshot()['can_run']:
                    if task['state']=='committing':
                        async with self.store.database.connect() as conn:
                            unack=await conn.scalar(select(s.units.c.unit_id).where(s.units.c.task_id==task['task_id'],
                                s.units.c.state.in_(COMMITTED),s.units.c.native_acknowledged==0).order_by(s.units.c.unit_id).limit(1))
                        if unack:
                            await self.cutover.acknowledge_one(self.lease,unack);return True
                    await self.journal.control(task['task_id'],self.lease.revision,'traffic_pause','scheduler')
                    self.lease=None;return False
                if task['state']=='queued':
                    self.lease=await self.journal.checkpoint(self.lease,'copying',committed_bytes=task['committed_bytes'])
                elif task['state']=='copying':await self._copy(task)
                elif task['state']=='verifying':self.lease=await self.publication.enter(self.lease)
                elif task['state']=='committing':await self._commit(task)
                elif task['state']=='cleanup_pending':await self._cleanup(task)
                elif task['state']=='cancelling':await self._cancel(task)
                if self.lease is not None and task['error_code']:await self.journal.note(self.lease,'')
                return True
            except Conflict:
                await self._release();return False
            except asyncio.CancelledError:raise
            except Exception as error:
                code=error_code(error)
                if self.lease is not None:
                    try:
                        if code in TRANSIENT:await self.journal.note(self.lease,code)
                        else:await self.journal.fail(self.lease,code)
                    except Conflict:pass
                    finally:await self._release()
                elif task is not None and task['state'] in PREPARING|{'cancelling'}:
                    await self._preparation_error(task,error)
                else:raise
                return False

    async def _copy(self,task):
        if not task['plan'].get('execution',{}).get('metadata',{}).get('complete'):
            await self.enrollment.advance(self.lease);return
        async with self.store.database.connect() as conn:unit=await pipeline.current(conn,task)
        if unit is None:raise p.ProtocolError('migration_copy_unit_missing')
        query=select(s.plan_bindings.c.logical_id).outerjoin(s.objects,and_(
            s.objects.c.task_id==s.plan_bindings.c.plan_id,s.objects.c.logical_id==s.plan_bindings.c.logical_id)).where(
                s.plan_bindings.c.plan_id==task['task_id'],s.plan_bindings.c.unit_id==unit['unit_id'],
                or_(s.objects.c.logical_id.is_(None),s.objects.c.state!='verified'))
        async with self.store.database.connect() as conn:
            logical=await conn.scalar(query.order_by(s.plan_bindings.c.logical_id).limit(1))
        if logical is None:self.lease=await self.publication.enter(self.lease)
        else:self.lease,_=await self.transfer.copy_one(self.lease,logical)

    @staticmethod
    def _fresh(proof,lease,now):
        return (proof.get('epoch')==lease.epoch and type(proof.get('expires_at')) is int
            and proof['expires_at']>now+5)

    async def _commit(self,task):
        async with self.store.database.connect() as conn:
            unack=(await conn.execute(select(s.units).where(s.units.c.task_id==task['task_id'],
                s.units.c.state.in_(COMMITTED),s.units.c.native_acknowledged==0).order_by(s.units.c.unit_id).limit(1))).mappings().first()
            unit=(await conn.execute(select(s.units).where(s.units.c.task_id==task['task_id'],
                s.units.c.state.not_in(COMMITTED)).order_by(s.units.c.unit_id).limit(1))).mappings().first()
        if unack is not None:
            await self.cutover.acknowledge_one(self.lease,unack['unit_id']);return
        if unit is None:
            self.lease=await self.journal.checkpoint(self.lease,'cleanup_pending',committed_bytes=task['committed_bytes'])
            await self._release();return
        if unit['state'] in {'pending','copying','verified'}:
            ready=True
            async with self.store.database.connect() as conn:
                try:await pipeline.verified(conn,task,dict(unit))
                except p.ProtocolError:ready=False
            if not ready:
                self.lease=await self.journal.checkpoint(self.lease,'copying',committed_bytes=task['committed_bytes'])
                return
        if not self.traffic.snapshot()['can_run']:
            await self._delay('migration_waiting_low_traffic');return
        target=unit['publication'].get('target_reply',{});source=unit['publication'].get('source_reply',{})
        if target.get('state')!='published' or not self._fresh(target,self.lease,self.wall()):
            await self.publication.target_step(self.lease,unit['unit_id'])
        elif source.get('phase')!='verified' or not self._fresh(source,self.lease,self.wall()):
            await self.publication.source_step(self.lease,unit['unit_id'])
        else:self.lease,_=await self.cutover.commit(self.lease,unit['unit_id'])

    async def _cleanup(self,task):
        if not self.traffic.snapshot()['can_run']:
            await self._delay('migration_waiting_low_traffic');return
        async with self.store.database.connect() as conn:
            unit=(await conn.execute(select(s.units).where(s.units.c.task_id==task['task_id'],s.units.c.state!='cleaned')
                .order_by(s.units.c.cleanup_after,s.units.c.unit_id).limit(1))).mappings().first()
        if unit is None:
            result=await self.finalization.advance(self.lease)
            if result['task_completed']:self.lease=None
            return
        self.lease,result=await self.cleanup.advance(self.lease,unit['unit_id'])
        if result['phase'] in ('waiting_grace','waiting_reader','waiting_low_traffic'):
            if result['phase']=='waiting_grace':await self._release()
            else:await self._delay('migration_'+result['phase'])

    async def _cancel(self,task):
        self.lease,result=await self.cancel_execution.advance(self.lease)
        if result.get('task_completed'):
            self.lease=None;return
        phase=result.get('phase','')
        if phase=='waiting_grace':await self._release()
        elif phase.endswith(('waiting_reader','waiting_writer','waiting_low_traffic')):
            suffix=phase.rsplit('waiting_',1)[-1]
            await self._delay('migration_waiting_'+suffix)

    async def run(self):
        while True:
            self.wakeup.clear()
            try:progress=await self.tick()
            except asyncio.CancelledError:raise
            except Exception as error:
                logger.warning('Migration scheduling deferred: %s',type(error).__name__)
                progress=False
            try:await asyncio.wait_for(self.wakeup.wait(),timeout=.05 if progress else 1)
            except TimeoutError:pass
