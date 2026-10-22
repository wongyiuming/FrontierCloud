"""One bounded read-only planning worker backed by persistent observation rows.

A ready preview is not admission. No task, drain, scope or capacity reservation
is created here. Start must repeat observations and install every durable
mutation fence before the transfer runner may use the plan.
"""
import asyncio
import hashlib
import json
import re
import stat
import time
import uuid

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p
from app.services.resource_inventory_client import InventoryClient
from app.services.resource_observation_client import ObservationClient
from app.services.resource_registry import RegistryClient
from app.services.resource_observation import digest
from app.services.resource_encryption_binding import source_digest
from app.services.resource_plan_store import PlanError, PlanStore, BATCH
from app.services.resource_pool import PHYSICAL_RESERVE_BYTES


def extend(checksum, value):
    raw = p.canonical(value)
    checksum.update(len(raw).to_bytes(4,'big'))
    checksum.update(raw)


def blocker(header, stage, code, *, path='', count=1):
    header['blocker_count'] += count
    if len(header['blockers']) < 64:
        safe = code if re.fullmatch(r'migration_[a-z_]{1,96}',code) else 'migration_peer_observation_failed'
        item = dict(stage=stage,code=safe,count=count)
        if path:
            item['path_sha256'] = digest(path)
        header['blockers'].append(item)


class Planner:
    def __init__(self, store, transport, *, traffic=None, wall=time.time):
        from app.services.business_traffic import monitor
        self.store, self.transport, self.wall = store, transport, wall
        self.traffic = monitor if traffic is None else traffic
        self.spool = PlanStore(store, wall=wall)
        self.epoch = uuid.uuid4().hex
        self.lock = asyncio.Lock()
        self.task = None
        self.plan_id = None

    async def start(self, request_id, source_id, target_id, actor):
        if not isinstance(request_id,str) or not p.IDENTIFIER.fullmatch(request_id):
            raise PlanError('migration_request_invalid')
        async with self.lock:
            async with self.store.database.connect() as conn:
                old = (await conn.execute(select(s.plans).where(s.plans.c.plan_id == request_id))).mappings().first()
            if old is not None:
                if old['source_id'] != source_id or old['target_id'] != target_id:
                    raise PlanError('migration_request_changed')
                return await self.status(request_id)
            if self.task is not None and not self.task.done():
                raise PlanError('migration_planner_busy')
            await self.spool.reap_previews()
            facts, members, relations = await self.spool.facts(source_id,target_id)
            header = await self.spool.create(request_id,facts,self.epoch,actor)
            self.plan_id = request_id
            self.task = asyncio.create_task(self._run(request_id,header,facts,members,relations), name='resource-migration-planner')
            return await self.status(request_id)

    async def _progress(self, plan_id, header, stage, **values):
        header['stage'] = stage
        await self.spool.save_header(plan_id,header,**values)

    async def _scan(self, plan_id, header, facts, relations, registries, observation_round):
        source, target = facts['source_id'], facts['target_id']
        header['inventory_complete'] = False
        inventories = {}
        for member in (source,target):
            inventories[member] = {}
            for scope in ('data','media','recordings'):
                stage = ('source_' if member == source else 'target_')+scope+'_'+observation_round
                await self._progress(plan_id,header,stage)
                await self.traffic.wait()
                async def observe(value):
                    try:
                        residual = await self.spool.native_entry(plan_id,member,source,
                            relations[source]['relationship_id'],scope,value,observation_round=observation_round)
                        if residual and observation_round == 'initial':
                            header['residual_count'] += 1
                    except PlanError as exc:
                        if str(exc) == 'migration_native_duplicate':
                            raise
                        blocker(header,stage,str(exc),path=value['path'])
                proof = await InventoryClient(self.store,relations[member],self.transport).read(scope,observe)
                if proof['header']['incarnation'] != registries[member]['inventory_incarnation']:
                    raise PlanError('migration_peer_restarted')
                inventories[member][scope] = proof
        header['inventory_complete'] = True
        return inventories

    async def _run(self, plan_id, header, facts, members, relations):
        source, target = facts['source_id'], facts['target_id']
        try:
            async with asyncio.timeout(1800):
                await self.traffic.wait()
                catalog = hashlib.sha256()
                count = total = 0
                async for entry in self.spool.catalog(source,relations[source]['relationship_id'],relations[target]['relationship_id']):
                    await self.traffic.wait()
                    extend(catalog,entry)
                    await self.spool.add(plan_id,entry,source,target)
                    count += 1
                    total += entry['expected_bytes']
                if not count:
                    raise PlanError('migration_source_empty')
                if total != members[source]['used_bytes']:
                    raise PlanError('migration_source_usage_mismatch')
                async with self.store.database.connect() as conn:
                    units = await conn.scalar(select(func.count(func.distinct(s.plan_bindings.c.unit_id))).where(
                        s.plan_bindings.c.plan_id == plan_id))
                await self.spool.save_header(plan_id,header,object_count=count,unit_count=units,total_bytes=total,
                                             catalog_sha256=catalog.hexdigest())
                registries = {}
                for member in (source,target):
                    stage = 'source_registry' if member == source else 'target_registry'
                    await self._progress(plan_id,header,stage)
                    await self.traffic.wait()
                    identity = await self.transport.identity(relations[member]['peer_endpoint'], expected_id=member,
                        expected_key=relations[member]['peer_key'], role='Follower')
                    if identity['node_id'] != member:
                        raise PlanError('migration_peer_identity_changed')
                    async def registration(value):
                        try:
                            await self.spool.registration(plan_id,member,source,value)
                        except PlanError as exc:
                            blocker(header,stage,str(exc),path=value['path'])
                    registries[member] = await RegistryClient(self.store,relations[member],self.transport).read(registration)
                async with self.store.database.connect() as conn:
                    expected_media = await conn.scalar(select(func.count()).select_from(s.plan_bindings).where(
                        s.plan_bindings.c.plan_id == plan_id,s.plan_bindings.c.resource_kind == 'media'))
                if registries[source]['count'] != expected_media:
                    blocker(header,'source_registry','migration_source_registration_incomplete',
                            count=abs(registries[source]['count']-expected_media))
                inventories = await self._scan(plan_id,header,facts,relations,registries,'initial')
                async with self.store.database.connect() as conn:
                    missing = await conn.scalar(select(func.count()).select_from(s.plan_bindings).where(
                        s.plan_bindings.c.plan_id == plan_id,s.plan_bindings.c.native_seen != 1))
                if missing:
                    blocker(header,header['stage'],'migration_source_file_missing',count=missing)
                if header['blocker_count']:
                    await self.spool.save_header(plan_id,header,state='blocked',expires_at=int(self.wall())+300)
                    return
                await self._progress(plan_id,header,'content')
                content = hashlib.sha256()
                async for row in self.spool.bindings(plan_id):
                    await self.traffic.wait()
                    snapshot = json.loads(row['object_json'])
                    proof = await ObservationClient(self.store,relations[source],self.transport).read(
                        row['resource_kind'],row['source_object'],snapshot['master'].get('user_id',''))
                    result = proof['result']
                    if proof['incarnation'] != registries[source]['incarnation']:
                        raise PlanError('migration_peer_restarted')
                    native = dict(result['native'])
                    native['mode'] = stat.S_IMODE(native['mode'])
                    if (result['bytes'] != row['expected_bytes'] or result['physical_path'] != snapshot['physical_path'] or
                            digest(native) != snapshot['native_entry_sha256']):
                        raise PlanError('migration_source_content_changed')
                    if row['resource_kind'] == 'media' and (
                            result['metadata_sha256'] != row['native_metadata_sha256'] or
                            result['encryption_sha256'] != source_digest(snapshot)):
                        raise PlanError('migration_source_metadata_changed')
                    expected_hash = snapshot['master']['etag'].strip('"') if row['resource_kind'] == 'media' else snapshot['master']['sha256']
                    if p.OBJECT_ID.fullmatch(expected_hash) and result['sha256'] != expected_hash:
                        raise PlanError('migration_source_hash_mismatch')
                    semantic = {key:value for key,value in result.items() if key != 'observed_at'}
                    extend(content,dict(logical_id=row['logical_id'],proof=semantic))
                    snapshot['source_proof'] = proof
                    async with self.store.database.begin() as conn:
                        await conn.execute(update(s.plan_bindings).where(s.plan_bindings.c.plan_id == plan_id,
                            s.plan_bindings.c.logical_id == row['logical_id']).values(source_sha256=result['sha256'],
                            object_json=p.canonical(snapshot).decode()))
                    header['observed_objects'] += 1
                    await self._progress(plan_id,header,'content')
                # Content observation can take minutes. Reset the seen flags and
                # rescan both endpoints, so deleted/rewritten sources and target
                # files introduced during hashing cannot pass an old preview.
                async with self.store.database.begin() as conn:
                    await conn.execute(update(s.plan_bindings).where(s.plan_bindings.c.plan_id == plan_id).values(native_seen=0))
                inventories = await self._scan(plan_id,header,facts,relations,registries,'final')
                async with self.store.database.connect() as conn:
                    missing = await conn.scalar(select(func.count()).select_from(s.plan_bindings).where(
                        s.plan_bindings.c.plan_id == plan_id,s.plan_bindings.c.native_seen != 1))
                if missing:
                    blocker(header,header['stage'],'migration_source_file_missing',count=missing)
                await self._progress(plan_id,header,'recheck')
                for member in (source,target):
                    async def unchanged(_value):
                        pass
                    fresh = await RegistryClient(self.store,relations[member],self.transport).read(unchanged)
                    if fresh != registries[member]:
                        raise PlanError('migration_registration_changed')
                again = hashlib.sha256()
                async for entry in self.spool.catalog(source,relations[source]['relationship_id'],relations[target]['relationship_id']):
                    extend(again,entry)
                latest, _, _ = await self.spool.facts(source,target)
                if again.hexdigest() != catalog.hexdigest() or latest != facts:
                    raise PlanError('migration_catalog_or_members_changed')
                header['blockers_complete'] = True
                native_free = min(proof['header']['physical_free_bytes'] for proof in inventories[target].values())
                available = min(members[target]['allocated_bytes']-members[target]['used_bytes']-members[target]['reserved_bytes'],
                                native_free-PHYSICAL_RESERVE_BYTES-members[target]['reserved_bytes'])
                header['capacity'] = dict(required_bytes=total,available_bytes=max(0,available),
                    native_free_bytes=native_free,physical_reserve_bytes=PHYSICAL_RESERVE_BYTES,writable_verified=False)
                if available < total:
                    blocker(header,header['stage'],'migration_target_capacity_insufficient')
                header['inventories'], header['registries'] = inventories, registries
                if header['blocker_count']:
                    await self.spool.save_header(plan_id,header,state='blocked',expires_at=int(self.wall())+300)
                    return
                review = digest(dict(facts=facts,catalog_sha256=catalog.hexdigest(),proofs_sha256=content.hexdigest(),
                                     registries=registries,object_count=count,unit_count=units,total_bytes=total))
                header['stage'] = 'ready'
                await self.spool.save_header(plan_id,header,state='ready',expires_at=int(self.wall())+300,
                    proofs_sha256=content.hexdigest(),plan_sha256=review)
        except asyncio.CancelledError:
            blocker(header,header['stage'],'migration_observation_interrupted')
            await self.spool.save_header(plan_id,header,state='blocked',expires_at=int(self.wall())+300)
            raise
        except (p.ProtocolError,httpx.HTTPError,ValueError,TypeError,KeyError,OSError,TimeoutError,SQLAlchemyError) as exc:
            code = str(exc) if isinstance(exc,PlanError) else 'migration_peer_observation_failed'
            blocker(header,header['stage'],code)
            await self.spool.save_header(plan_id,header,state='blocked',expires_at=int(self.wall())+300)

    async def status(self, plan_id, *, after=''):
        if not isinstance(plan_id,str) or not p.IDENTIFIER.fullmatch(plan_id) or not isinstance(after,str) or (after and not (p.OBJECT_ID.fullmatch(after) or p.IDENTIFIER.fullmatch(after))):
            raise PlanError('migration_plan_cursor_invalid')
        async with self.store.database.connect() as conn:
            row = (await conn.execute(select(s.plans).where(s.plans.c.plan_id == plan_id))).mappings().first()
            if row is None or row['master_id'] != self.store.node['node_id']:
                raise PlanError('migration_plan_unavailable')
            header = json.loads(row['header_json'])
            if header['epoch'] != self.epoch or row['expires_at'] <= self.wall():
                raise PlanError('migration_plan_expired_or_restarted')
            rows = list((await conn.execute(select(s.plan_bindings.c.logical_id,s.plan_bindings.c.resource_kind,
                s.plan_bindings.c.unit_id,s.plan_bindings.c.expected_bytes,s.plan_bindings.c.source_sha256,s.plan_bindings.c.object_json)
                .where(s.plan_bindings.c.plan_id == plan_id,s.plan_bindings.c.logical_id > after)
                .order_by(s.plan_bindings.c.logical_id).limit(BATCH+1))).mappings())
        result = dict(plan_id=plan_id,state=row['state'],review_sha256=row['plan_sha256'],object_count=row['object_count'],
                    unit_count=row['unit_count'],total_bytes=row['total_bytes'],expires_at=row['expires_at'],
                    report=header,objects=[],next=after,complete=False)
        for raw in rows[:BATCH]:
            value = dict(raw)
            snapshot = json.loads(value.pop('object_json'))
            value.update({name:snapshot[name] for name in ('physical_path','target_path','parent_path')})
            result['objects'].append(value)
            if len(p.canonical(result)) > 48*1024-256:
                result['objects'].pop()
                break
            result['next'] = value['logical_id']
        if rows and not result['objects']:
            raise PlanError('migration_plan_response_too_large')
        result['complete'] = len(rows) == len(result['objects'])
        if len(p.canonical(result)) > 48*1024:
            raise PlanError('migration_plan_response_too_large')
        return result

    async def stop(self):
        async with self.lock:
            if self.task is not None and not self.task.done():
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
