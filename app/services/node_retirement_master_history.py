"""Paged Master migration-history validation, without transfer or deletion rights."""
import hashlib
import stat
from types import SimpleNamespace

from sqlalchemy import func,select

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.node_retirement_history import JSON_LIMIT,decoded,exists,fail,ident,integer,orphans,pages,projection,terminal_clear
from app.services.node_retirement_fences import RetirementError
from app.services.resource_commit_peers import CommitPeers
from app.services.resource_cancel_terminal_receipts import COMPLETION,cleaned_unit,master_task,master_unit
from app.services.resource_final_receipts import CANCELLED_COMPLETION_FORMAT,committed_cancellation,verify as final_receipt
from app.services.resource_migration_planning import extend
from app.services.resource_observation import digest
from app.services.resource_encryption_binding import bind_source,source_digest
from app.services.resource_observation_client import ObservationClient
from app.services.resource_peer_reservations import INITIAL_UNITS_SHA256,unit_transcript
from app.services.resource_plan_store import relation_binding
from app.services.resource_transfer_protocol import object_shape


class MasterHistory:
    def __init__(self,store,now):self.store,self.now=store,now

    async def _header(self,conn,task):
        for name in ('task_id','master_id','source_id','target_id','source_relationship','target_relationship'):ident(task[name])
        ident(task['plan_sha256'],long=True)
        if (task['state'] not in {'completed','cancelled'} or len({task['master_id'],task['source_id'],task['target_id']})!=3 or
                task['source_relationship']==task['target_relationship'] or task['error_code'] or task['pause_reason'] or task['resume_state']):fail()
        for name in ('revision','created_at','updated_at','object_count','total_bytes'):integer(task[name],positive=True)
        if not task['created_at']<=task['updated_at']<=self.now+5:fail()
        plan=decoded(task,'plan');task={**task,'plan':plan}
        if (plan.get('format')!='frontiercloud-python-admission' or type(plan.get('version')) is not int or plan['version']!=1
                or type(task['committed_bytes']) is not int or not 0<=task['committed_bytes']<=task['total_bytes']
                or COMPLETION not in plan and task['committed_bytes']!=(task['total_bytes'] if task['state']=='completed' or committed_cancellation(task) else 0)):fail()
        integer(plan.get('unit_count'),positive=True);ident(plan.get('units_sha256'),long=True)
        if plan['unit_count']>task['object_count']:fail()
        row=(await conn.execute(select(*projection(s.plans,{'header_json':JSON_LIMIT})).where(s.plans.c.plan_id==task['task_id']))).mappings().first()
        if not row or row['state']!='admitted':fail()
        header=decoded(row,'header_json',canonical=True)
        facts=plan.get('facts')
        if not isinstance(facts,dict) or facts.keys()!={'master_id','source_id','target_id','relationships','storage'}:fail()
        if any(facts[name]!=task[name] or row[name]!=task[name] for name in ('master_id','source_id','target_id')):fail()
        if (any(header.get(name)!=item for name,item in facts.items()) or row['plan_sha256']!=task['plan_sha256'] or
                (row['object_count'],row['unit_count'],row['total_bytes'])!=(task['object_count'],plan['unit_count'],task['total_bytes']) or
                header.get('blocker_count')!=0 or header.get('blockers_complete') is not True or header.get('inventory_complete') is not True):fail()
        for name in ('catalog_sha256','proofs_sha256'):ident(row[name],long=True)
        if digest(dict(facts=facts,catalog_sha256=row['catalog_sha256'],proofs_sha256=row['proofs_sha256'],registries=header.get('registries'),
                object_count=row['object_count'],unit_count=row['unit_count'],total_bytes=row['total_bytes']))!=task['plan_sha256']:fail()
        members={task['source_id'],task['target_id']}
        if not isinstance(facts['relationships'],dict) or set(facts['relationships'])!=members or not isinstance(facts['storage'],dict) or set(facts['storage'])!=members:fail()
        grant={name:task[name] for name in ('task_id','master_id','source_id','target_id','source_relationship','target_relationship','plan_sha256')}
        grant['units_sha256']=plan['units_sha256']
        for side in ('source','target'):
            member=task[side+'_id'];saved=facts['storage'][member]
            if not isinstance(saved,dict) or saved.get('member_id')!=member or saved.get('member_kind')!='Follower' or saved.get('transport') not in {'Direct','Relay'}:fail()
            relation=(await conn.execute(select(*projection(fs.relationships,{},names={'relationship_id','peer_id','peer_endpoint','peer_key',
                'credential','direction','mode','state','protocol'})).where(fs.relationships.c.relationship_id==task[side+'_relationship']))).mappings().first()
            if not relation or relation['peer_id']!=member or relation['direction']!='downstream':fail()
            # Retirement and subsequent mode changes legitimately change these
            # two fields. The persisted key, credential and endpoint do not.
            original={**dict(relation),'state':'active','mode':saved['transport']}
            if relation_binding(original)!=facts['relationships'][member]:fail('retirement_migration_owner_changed')
            grant[side+'_key']=relation['peer_key'];grant[side+'_endpoint']=relation['peer_endpoint']
            request={name:grant[name] for name in ('task_id','master_id','source_id','target_id','source_relationship','target_relationship','plan_sha256','units_sha256')}
            grant[side+'_binding']=digest(dict(**request,side=side,unit_count=plan['unit_count'],object_count=task['object_count'],total_bytes=task['total_bytes']))
        return task,dict(row),grant

    def _snapshot(self,task,row):
        snapshot=decoded(row,'object_json',canonical=True)
        fields={'master','encryption','physical_path','target_path','parent_path','target_parent','source_object','source_proof','native_entry_sha256'}
        if snapshot.keys() not in (fields,fields|{'source_encryption_sha256'}):fail()
        original={name:value for name,value in snapshot.items() if name not in {'source_proof','native_entry_sha256','source_encryption_sha256'}}
        if digest(original)!=row['object_sha256'] or snapshot['source_object']!=row['source_object']:fail()
        proof=snapshot['source_proof'];master=snapshot['master'];kind=row['resource_kind']
        if not isinstance(proof,dict) or not isinstance(master,dict) or not isinstance(proof.get('result'),dict):fail()
        result=proof['result'];observed=integer(result.get('observed_at'),positive=True)
        binding=dict(node_id=task['source_id'],master_id=task['master_id'],relationship_id=task['source_relationship'],
            request_id=ident(proof.get('request_id')),resource_kind=kind,object_id=row['source_object'],user_id=master.get('user_id',''))
        try:
            # This is structural validation at the recorded observation time.
            # No fresh grant, network request or authentication is produced.
            ObservationClient(None,None,None,wall=lambda:observed)._validate(proof,binding,proof)
        except (p.ProtocolError,ValueError,TypeError,KeyError):fail()
        if proof['state']!='completed' or observed>task['created_at']+5:fail()
        if (master.get('media_id' if kind=='media' else 'recording_id')!=row['logical_id'] or master.get('storage_member_id')!=task['source_id'] or
                master.get('size_bytes')!=row['expected_bytes'] or result['bytes']!=row['expected_bytes'] or
                result['physical_path']!=snapshot['physical_path'] or result['sha256']!=row['source_sha256'] or row['native_seen']!=1 or
                snapshot['parent_path']!=snapshot['physical_path'].rsplit('/',1)[0] or snapshot['target_parent']!=snapshot['target_path'].rsplit('/',1)[0]):fail()
        native={**result['native'],'mode':stat.S_IMODE(result['native']['mode'])}
        if digest(native)!=snapshot['native_entry_sha256']:fail()
        try:
            if source_digest(snapshot)!=result['encryption_sha256']:fail()
            bind_source(snapshot,result['encryption_sha256'])
        except (ValueError,TypeError,KeyError):fail()
        if kind=='media' and result['metadata_sha256']!=row['native_metadata_sha256']:fail()
        obj=dict(logical_id=row['logical_id'],unit_id=row['unit_id'],resource_kind=kind,source_object=row['source_object'],
            source_path=snapshot['physical_path'],target_path=snapshot['target_path'],source_version=result['source_version'],
            unit_version='0'*64,expected_bytes=row['expected_bytes'],sha256=row['source_sha256'],
            metadata_sha256=result['metadata_sha256'],encryption_sha256=result['encryption_sha256'],user_id=master.get('user_id',''))
        if 'source_encryption_sha256' in snapshot:obj['master_encryption_sha256']=digest(snapshot['encryption'])
        try:object_shape({**task,'object':obj})
        except (p.ProtocolError,ValueError,TypeError,KeyError):fail()
        return snapshot,original,obj

    async def _catalog(self,conn,task,header):
        catalog=hashlib.sha256();proofs=hashlib.sha256();count=total=0
        for kind in ('media','recording'):
            async for row in pages(conn,s.plan_bindings,(s.plan_bindings.c.plan_id==task['task_id'])&(s.plan_bindings.c.resource_kind==kind),fields={'object_json':JSON_LIMIT},key=s.plan_bindings.c.logical_id):
                snapshot,original,_=self._snapshot(task,row)
                extend(catalog,dict(logical_id=row['logical_id'],resource_kind=kind,snapshot=original,unit_id=row['unit_id'],expected_bytes=row['expected_bytes']))
                extend(proofs,dict(logical_id=row['logical_id'],proof={name:value for name,value in snapshot['source_proof']['result'].items() if name!='observed_at'}))
                count+=1;total+=row['expected_bytes'];integer(total)
        if (count,total,catalog.hexdigest(),proofs.hexdigest())!=(task['object_count'],task['total_bytes'],header['catalog_sha256'],header['proofs_sha256']):fail()

    async def _objects(self,conn,task,unit_id,unit_version,*,state=None):
        expected_state=state or ('cleaned' if task['state']=='completed' else 'cancelled')
        checksum=hashlib.sha256();count=objects=total=0
        async for binding in pages(conn,s.plan_bindings,(s.plan_bindings.c.plan_id==task['task_id'])&(s.plan_bindings.c.unit_id==unit_id),fields={'object_json':JSON_LIMIT},key=s.plan_bindings.c.logical_id):
            snapshot,_,obj=self._snapshot(task,binding)
            extend(checksum,dict(logical_id=obj['logical_id'],source_object=obj['source_object'],source_sha256=obj['sha256'],source_version=obj['source_version']))
            row=(await conn.execute(select(*projection(s.objects,{'catalog_encryption_json':16384})).where(s.objects.c.task_id==task['task_id'],s.objects.c.logical_id==obj['logical_id']))).mappings().first()
            if row is not None:
                expected=dict(unit_id=unit_id,resource_kind=obj['resource_kind'],source_object=obj['source_object'],target_object=obj['source_object'],
                    source_version=obj['source_version'],physical_path=obj['source_path'],expected_bytes=obj['expected_bytes'],source_sha256=obj['sha256'],
                    catalog_etag=snapshot['master'].get('etag',''),catalog_path_locator=snapshot['master'].get('path_locator',''),
                    catalog_media_kind=snapshot['master'].get('object_kind',''),catalog_user_id=obj['user_id'])
                if any(row[name]!=value for name,value in expected.items()) or row['state']!=expected_state:fail()
                raw=row['catalog_encryption_json']
                if raw:
                    encryption=decoded(row,'catalog_encryption_json',16384)
                    if encryption!=snapshot['encryption'] or row['catalog_encryption_file_id']!=encryption.get('file_id'):fail()
                elif snapshot['encryption'] is not None or row['catalog_encryption_file_id']:fail()
                if expected_state=='cleaned':
                    if row['target_sha256']!=obj['sha256'] or row['verified_bytes']!=obj['expected_bytes']:fail()
                elif row['target_sha256'] not in {'',obj['sha256']} or row['verified_bytes'] not in {0,obj['expected_bytes']}:fail()
                objects+=1
            elif expected_state=='cleaned' or COMPLETION in task['plan']:fail()
            count+=1;total+=obj['expected_bytes']
        if count==0 or checksum.hexdigest()!=unit_version:fail()
        return count,objects,total

    async def _units(self,conn,task,grant):
        terminal=COMPLETION in task['plan'];committed=0;complete=task['state']=='completed' or committed_cancellation(task)
        transcript=INITIAL_UNITS_SHA256;census=digest(['master-cancel-final-census' if terminal else 'master-final-census',1]);units=objects=total=0
        checker=CommitPeers(SimpleNamespace(store=self.store,wall=lambda:self.now))
        async for unit in pages(conn,s.units,s.units.c.task_id==task['task_id'],fields={'publication':JSON_LIMIT},key=s.units.c.unit_id):
            publication=decoded(unit,'publication');parent=unit['parent_path'];kind=unit['resource_kind']
            allowed={'cleaned','cancelled'} if terminal else {'cleaned' if complete else 'cancelled'}
            if task['plan']['phase']=='aborted' or unit['state'] not in allowed:fail()
            description=dict(unit_id=unit['unit_id'],resource_kind=kind,source_parent=parent,
                target_parent=parent if kind=='media' else task['target_relationship']+'/'+parent.rsplit('/',1)[-1],source_version=unit['source_version'],
                object_count=unit['object_count'],total_bytes=unit['total_bytes'])
            count,_,amount=await self._objects(conn,task,unit['unit_id'],unit['source_version'],state=unit['state'])
            if (count,amount)!=(unit['object_count'],unit['total_bytes']):fail()
            if unit['state']=='cleaned':
                commit=publication.get('commit')
                try:checksum=checker._record(commit,grant)
                except (p.ProtocolError,TypeError,KeyError):fail()
                if (checksum!=publication.get('commit_sha256') or unit['native_acknowledged']!=1 or
                        unit['cleanup_after']>self.now or unit['cleanup_after']!=commit['cleanup_after'] or unit['committed_at']!=commit['committed_at'] or
                        any(commit[name]!=description[other] for name,other in (('unit_id','unit_id'),('resource_kind','resource_kind'),('source_parent','source_parent'),
                            ('target_parent','target_parent'),('unit_version','source_version'),('object_count','object_count'),('total_bytes','total_bytes')))):fail()
                if terminal:
                    try:proof=cleaned_unit({**unit,'publication':publication},self.now)
                    except p.ProtocolError:fail('retirement_migration_receipt_invalid')
                    census=digest([census,unit['unit_id'],proof]);committed+=unit['total_bytes']
                else:census=digest([census,unit['unit_id'],checksum])
            elif terminal:
                if unit['committed_at'] or unit['cleanup_after'] or publication.get('commit'):fail()
                proof=publication.get('cancel_unit_receipt')
                try:master_unit(proof,p.public_key(self.store.unseal(self.store.node['private_key'])),task,unit,self.now)
                except p.ProtocolError:fail('retirement_migration_receipt_invalid')
                census=digest([census,unit['unit_id'],proof])
            elif unit['native_acknowledged'] or unit['committed_at'] or unit['cleanup_after'] or publication.get('commit'):fail()
            transcript=unit_transcript(transcript,description);units+=1;objects+=count;total+=amount
        if task['plan']['phase']=='aborted':
            if units or await exists(conn,s.objects,s.objects.c.task_id==task['task_id']):fail()
        elif (units,objects,total,transcript)!=(task['plan']['unit_count'],task['object_count'],task['total_bytes'],task['plan']['units_sha256']):fail()
        if terminal and committed!=task['committed_bytes']:fail()
        # A journal row whose logical ID is absent from its preview is foreign.
        if await exists(conn,s.objects,(s.objects.c.task_id==task['task_id'])&~select(s.plan_bindings.c.logical_id).where(
                s.plan_bindings.c.plan_id==s.objects.c.task_id,s.plan_bindings.c.logical_id==s.objects.c.logical_id).exists()):fail('retirement_migration_orphan')
        return census

    async def _planned_units(self,conn,task):
        current=None;checksum=None;transcript=INITIAL_UNITS_SHA256;count=objects=total=0
        def finish():
            return dict(**current,source_version=checksum.hexdigest())
        async for row in pages(conn,s.plan_bindings,s.plan_bindings.c.plan_id==task['task_id'],fields={'object_json':JSON_LIMIT},
                key=(s.plan_bindings.c.unit_id,s.plan_bindings.c.logical_id)):
            snapshot,_,obj=self._snapshot(task,row)
            if current is None or current['unit_id']!=row['unit_id']:
                if current:transcript=unit_transcript(transcript,finish());count+=1
                current=dict(unit_id=row['unit_id'],resource_kind=row['resource_kind'],source_parent=snapshot['parent_path'],
                    target_parent=snapshot['target_parent'],object_count=0,total_bytes=0);checksum=hashlib.sha256()
            if (current['resource_kind'],current['source_parent'],current['target_parent'])!=(row['resource_kind'],snapshot['parent_path'],snapshot['target_parent']):fail()
            current['object_count']+=1;current['total_bytes']+=obj['expected_bytes'];objects+=1;total+=obj['expected_bytes']
            extend(checksum,dict(logical_id=obj['logical_id'],source_object=obj['source_object'],source_sha256=obj['sha256'],source_version=obj['source_version']))
        if current:transcript=unit_transcript(transcript,finish());count+=1
        if (count,objects,total,transcript)!=(task['plan']['unit_count'],task['object_count'],task['total_bytes'],task['plan']['units_sha256']):fail()

    async def _task(self,conn,raw):
        task,header,grant=await self._header(conn,raw);plan=task['plan']
        if task['state']=='cancelled' and plan.get('phase') not in {'aborted','cancelled'}:fail()
        await terminal_clear(conn,task['task_id'],self.now)
        await self._catalog(conn,task,header)
        await self._planned_units(conn,task)
        completion=plan.get('completion')
        cancelled=None
        if COMPLETION in plan:
            try:cancelled=master_task(plan[COMPLETION],p.public_key(self.store.unseal(self.store.node['private_key'])),task,self.now)
            except p.ProtocolError:fail('retirement_migration_receipt_invalid')
            signed=cancelled['grant']['payload']
            if any(signed.get(name)!=value for name,value in grant.items()):fail('retirement_migration_owner_changed')
            grant={**grant,'epoch':cancelled['epoch']}
        if task['state']=='completed' or committed_cancellation(task):
            if not isinstance(completion,dict) or completion.keys()!={'format','version','task_id','plan_sha256','epoch','completed_at','unit_count','object_count','total_bytes','census_sha256','receipts'}:fail()
            expected=dict(format=CANCELLED_COMPLETION_FORMAT if committed_cancellation(task) else 'frontiercloud-python-migration-completion',
                version=1,task_id=task['task_id'],plan_sha256=task['plan_sha256'],
                unit_count=plan['unit_count'],object_count=task['object_count'],total_bytes=task['total_bytes'])
            if any(completion[name]!=value for name,value in expected.items()) or digest(completion)!=plan.get('completion_sha256'):fail()
            grant['epoch']=integer(completion['epoch'],positive=True);finished=integer(completion['completed_at'],positive=True)
            if not task['created_at']<=finished<=task['updated_at']:fail()
            receipts=completion['receipts']
            if not isinstance(receipts,dict) or receipts.keys()!={'source','target'}:fail()
            for side in ('source','target'):
                try:value=final_receipt(receipts[side],grant,side,(plan['unit_count'],task['object_count'],task['total_bytes']),self.now,source_receipt=receipts['source'])
                except (p.ProtocolError,TypeError,KeyError):fail('retirement_migration_receipt_invalid')
                if not task['created_at']<=value['finished_at']<=finished:fail()
        elif completion is not None or plan.get('completion_sha256'):fail()
        census=await self._units(conn,task,grant)
        if (task['state']=='completed' or committed_cancellation(task)) and census!=completion['census_sha256']:fail()
        if cancelled is not None and census!=cancelled['census_sha256']:fail('retirement_migration_receipt_invalid')

    async def _verify(self,conn,node,member_id):
        ident(member_id);await orphans(conn);checked=0
        async for row in pages(conn,s.tasks,(s.tasks.c.source_id==member_id)|(s.tasks.c.target_id==member_id),fields={'plan':JSON_LIMIT}):
            await self._task(conn,row);checked+=1
        return dict(master_tasks=checked)

    async def verify(self,conn,node,member_id):
        try:return await self._verify(conn,node,member_id)
        except RetirementError:raise
        except (ValueError,TypeError,KeyError,OverflowError,RecursionError):fail()
