"""Bounded Admin projections and durable controls over the migration domain."""
from sqlalchemy import and_,or_,select

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.resource_plan_store import PlanError

PAGE=8
LIMIT=48*1024
TASK_FIELDS=('task_id','source_id','target_id','state','revision','object_count','total_bytes','committed_bytes',
    'error_code','resume_state','pause_reason','created_at','updated_at')
UNIT_FIELDS=('unit_id','resource_kind','parent_path','state','object_count','total_bytes','committed_at','cleanup_after','native_acknowledged')


def cursor(value,*,long=False):
    if not isinstance(value,str) or value and not (p.OBJECT_ID if long else p.IDENTIFIER).fullmatch(value):
        raise PlanError('migration_admin_cursor_invalid')
    return value


class Admin:
    def __init__(self,runtime):self.runtime,self.store=runtime,runtime.store

    async def master(self,*,mutable=False):
        async with self.store.database.connect() as conn:
            node=(await conn.execute(select(fs.identity.c.node_id,fs.identity.c.role).where(fs.identity.c.singleton==1))).mappings().one()
        if node['role']!='Master' or node['node_id']!=self.store.node['node_id']:raise PlanError('migration_admin_master_required')
        if mutable and self.runtime.scheduler.fenced():raise PlanError('migration_admin_maintenance_fenced')

    async def members(self,after=''):
        await self.master();cursor(after)
        async with self.store.database.connect() as conn:
            rows=(await conn.execute(select(fs.storage_members.c.member_id,fs.storage_members.c.transport,
                fs.storage_members.c.storage_enabled,fs.storage_members.c.allocated_bytes,fs.storage_members.c.used_bytes,
                fs.storage_members.c.reserved_bytes,fs.relationships.c.peer_endpoint,fs.relationships.c.status).join(fs.relationships,
                fs.storage_members.c.relationship_id==fs.relationships.c.relationship_id).where(
                fs.storage_members.c.member_kind=='Follower',fs.relationships.c.state=='active',fs.relationships.c.direction=='downstream',
                fs.storage_members.c.member_id>after)
                .order_by(fs.storage_members.c.member_id).limit(65))).mappings().all()
        return dict(items=[dict(row) for row in rows[:64]],complete=len(rows)<=64,next_cursor=rows[63]['member_id'] if len(rows)>64 else '')

    async def preview(self,request_id,source_id,target_id,actor):
        await self.master(mutable=True)
        return await self.runtime.planner.start(request_id,source_id,target_id,actor)

    async def plan(self,plan_id,after=''):
        await self.master();return await self.runtime.planner.status(plan_id,after=after)

    async def start(self,plan_id,approved_sha256,confirm_source_id,confirm_target_id,actor):
        await self.master(mutable=True)
        if not isinstance(plan_id,str) or not p.IDENTIFIER.fullmatch(plan_id):raise PlanError('migration_admission_request_invalid')
        async with self.store.database.connect() as conn:
            row=(await conn.execute(select(s.plans.c.source_id,s.plans.c.target_id,s.plans.c.master_id).where(s.plans.c.plan_id==plan_id))).mappings().first()
        if not row or row['master_id']!=self.store.node['node_id']:raise PlanError('migration_plan_unavailable')
        if (confirm_source_id,confirm_target_id)!=(row['source_id'],row['target_id']):raise PlanError('migration_admin_confirmation_mismatch')
        await self.runtime.scheduler.admission.queue(plan_id,approved_sha256,actor)
        self.runtime.scheduler.wakeup.set()
        return await self.task(plan_id)

    async def history(self,after=''):
        await self.master();cursor(after)
        async with self.store.database.connect() as conn:
            query=select(*(s.tasks.c[name] for name in TASK_FIELDS)).where(s.tasks.c.master_id==self.store.node['node_id'])
            if after:
                anchor=(await conn.execute(select(s.tasks.c.created_at).where(s.tasks.c.task_id==after,
                    s.tasks.c.master_id==self.store.node['node_id']))).mappings().first()
                if anchor is None:raise PlanError('migration_admin_cursor_invalid')
                query=query.where(or_(s.tasks.c.created_at<anchor['created_at'],and_(s.tasks.c.created_at==anchor['created_at'],s.tasks.c.task_id<after)))
            rows=(await conn.execute(query.order_by(s.tasks.c.created_at.desc(),s.tasks.c.task_id.desc()).limit(21))).mappings().all()
        return dict(items=[dict(row) for row in rows[:20]],next_cursor=rows[19]['task_id'] if len(rows)>20 else '')

    async def task(self,task_id,after=''):
        await self.master();cursor(task_id);cursor(after,long=True)
        async with self.store.database.connect() as conn:
            row=(await conn.execute(select(s.tasks).where(s.tasks.c.task_id==task_id,s.tasks.c.master_id==self.store.node['node_id']))).mappings().first()
            if not row:raise PlanError('migration_admin_task_unavailable')
            result={name:row[name] for name in TASK_FIELDS};result.update(units=[],next_cursor='')
            current=row['plan'].get('current_object',{})
            if current:
                obj=(await conn.execute(select(s.objects.c.logical_id,s.objects.c.physical_path,s.objects.c.expected_bytes,s.objects.c.state).where(
                    s.objects.c.task_id==task_id,s.objects.c.logical_id==current.get('logical_id')))).mappings().first()
                if obj is not None:
                    received,hashed=current.get('received_bytes'),current.get('hashed_bytes')
                    if type(received) is not int or type(hashed) is not int or not 0<=hashed<=received<=obj['expected_bytes']:
                        raise PlanError('migration_admin_progress_invalid')
                    result['current_object']={**dict(obj),'received_bytes':received,'hashed_bytes':hashed}
            units=(await conn.execute(select(s.units).where(s.units.c.task_id==task_id,s.units.c.unit_id>after)
                .order_by(s.units.c.unit_id).limit(PAGE+1))).mappings().all()
            for unit in units[:PAGE]:
                item={name:unit[name] for name in UNIT_FIELDS}
                item['publication_phase']=unit['publication'].get('target_reply',{}).get('phase','')
                result['units'].append(item)
                if len(p.canonical(result))>LIMIT-256:
                    result['units'].pop();break
            if units and not result['units']:raise PlanError('migration_admin_response_too_large')
            if len(units)>len(result['units']):result['next_cursor']=result['units'][-1]['unit_id']
        if len(p.canonical(result))>LIMIT:raise PlanError('migration_admin_response_too_large')
        return result

    async def control(self,task_id,revision,action,actor):
        await self.master(mutable=True)
        if action not in {'pause','resume','cancel','retry'}:raise PlanError('migration_admin_action_invalid')
        task=await self.task(task_id);scheduler=self.runtime.scheduler
        if action=='cancel':
            if task['state'] in {'admitting','prepared'}:await scheduler.admission.request_cancel(task_id,revision,actor)
            else:await scheduler.cancellation.request(task_id,revision,actor)
        else:await scheduler.journal.control(task_id,revision,action,actor)
        scheduler.wakeup.set()
        return await self.task(task_id)
