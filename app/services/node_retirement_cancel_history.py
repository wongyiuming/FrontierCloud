"""Offline native cancellation history; no live grants or filesystem authority."""
from types import SimpleNamespace

from sqlalchemy import func,select

from app.services import resource_migration_schema as s
from app.services.federation import protocol as p,schema as fs
from app.services.node_retirement_history import JSON_LIMIT,decoded,exists,fail,ident,integer,native_shape,pages,projection
from app.services.resource_cancel_authority import verify as authorization
from app.services.resource_cancel_source_peers import selected as source_selected,source_proof,verify_receipt as retained
from app.services.resource_cancel_target import TargetCancellation,selected as target_selected,verify_receipt as removed
from app.services.resource_cancel_target_receipts import released,source_record
from app.services.resource_cancel_unit_receipts import recorded,verify as settled
from app.services.resource_commit_peers import CommitPeers
from app.services.resource_observation import digest
from app.services.resource_peer_reservations import INITIAL_UNITS_SHA256,unit_transcript
from app.services.resource_target_verify import live_proof
from app.services.resource_transfer_protocol import target_encryption_digest
from app.services import resource_cancel_private_files as private_files
from app.services.resource_cancel_private_receipts import FORMAT as PRIVATE_RECEIPT


class CancellationHistory:
    def __init__(self,parent):self.parent,self.store,self.now=parent,parent.store,parent.now

    async def _objects(self,conn,node,header,request,control):
        first=None;count=total=0;side=header['side']
        async for binding in pages(conn,s.peer_bindings,s.peer_bindings.c.task_id==header['task_id'],fields={'binding_json':16384},key=s.peer_bindings.c.logical_id):
            value,common=self.parent._binding(node,header,request,control,binding);obj=value['object']
            if first is None:first=common
            elif first!=common:fail()
            count+=1;total+=obj['expected_bytes'];integer(total)
            if count>header['expected_objects'] or total>header['expected_bytes']:fail()
            unit=(await conn.execute(select(*projection(s.units,{'publication':JSON_LIMIT})).where(s.units.c.task_id==header['task_id'],s.units.c.unit_id==obj['unit_id']))).mappings().first()
            if (not unit or unit['state'] not in {'cleaned','committed','cancelled'} or unit['source_version']!=obj['unit_version']
                    or unit['parent_path']!=obj[side+'_path'].rsplit('/',1)[0]):fail()
            entry=(await conn.execute(select(*projection(s.storage_journal,{'publication':JSON_LIMIT})).where(
                s.storage_journal.c.task_id==header['task_id'],s.storage_journal.c.logical_id==obj['logical_id']))).mappings().first()
            if unit['state']=='cancelled' and side=='source':
                if entry is not None:fail()
                continue
            if entry is None:fail()
            if (any(entry[name]!=value[name] for name in ('task_id','master_id','source_id','target_id'))
                    or entry['unit_id']!=obj['unit_id'] or entry['resource_kind']!=obj['resource_kind'] or entry['object_id']!=obj['source_object']
                    or entry['physical_path']!=obj[side+'_path'] or entry['source_version']!=obj['source_version']
                    or entry['expected_bytes']!=obj['expected_bytes']):fail()
            publication=decoded(entry,'publication')
            unit_publication=decoded(unit,'publication')
            progress=unit_publication.get('target_cancel')
            if side=='target' and unit['state']=='cancelled' and not isinstance(progress,dict):fail()
            if side=='target' and unit['state']=='cancelled' and progress.get('version')==2:
                if (entry['state']!='aborted' or type(entry['publication_phase']) is not int or entry['publication_phase']!=0 or
                        set(publication)-{'verified_native','private_cancel'} or entry['sha256'] not in {'',obj['sha256']}):fail()
                marker=publication.get('private_cancel')
                if not isinstance(marker,dict) or marker.keys()!={'intent'} or digest(marker['intent'])!=entry['cleanup_version']:fail()
                root=control['staging_roots'][obj['resource_kind']]
                try:private_files.validate(marker['intent'],header['task_id'],unit,obj,root)
                except (p.ProtocolError,ValueError,TypeError,KeyError):fail()
                intent=marker['intent'];actual=intent['stage']
                if intent['present']:
                    if entry['stage_identity']!=actual['identity']:fail()
                elif entry['stage_identity'] or entry['sha256']:fail()
                if entry['sha256']:
                    original=native_shape(publication.get('verified_native'),obj['expected_bytes'])
                    if (not intent['present'] or actual['bytes']!=obj['expected_bytes'] or actual['version']!=digest(original) or
                            actual['identity']!=digest([original['device'],original['inode']])):fail()
                elif 'verified_native' in publication:fail()
                continue
            if entry['sha256']!=obj['sha256']:fail()
            if side=='target':
                if unit['state']=='cancelled':
                    if entry['state']!='aborted' or type(entry['publication_phase']) is not int or not 0<=entry['publication_phase']<=3:fail()
                    if digest(publication.get('target_cancel',{}).get('intent'))!=entry['cleanup_version']:fail()
                    ident(entry['cleanup_version'],long=True)
                    if set(publication)-{'verified_native','assembled_native','encryption','target_cancel'}:fail()
                elif entry['state']!='published' or entry['publication_phase']!=3 or set(publication)!={'verified_native','assembled_native','encryption'}:fail()
                native_shape(publication['verified_native'],obj['expected_bytes'])
                if entry['publication_phase']>=2:native_shape(publication['assembled_native'],obj['expected_bytes'])
                if entry['publication_phase']==3 and digest(publication.get('encryption'))!=target_encryption_digest(obj):fail()
            else:
                original=decoded(unit,'publication')
                if (entry['state']!='deleted' or entry['stage_identity'] or publication.keys()!={'action','native','commit_sha256'}
                        or publication['action']!='source-cleanup' or publication['commit_sha256']!=original.get('commit_sha256')
                        or entry['cleanup_version']!=digest(native_shape(publication['native'],obj['expected_bytes']))):fail()
        if (first is None or (count,total)!=(header['expected_objects'],header['expected_bytes']) or
                await exists(conn,s.storage_journal,(s.storage_journal.c.task_id==header['task_id'])&~select(s.peer_bindings.c.logical_id).where(
                    s.peer_bindings.c.task_id==s.storage_journal.c.task_id,s.peer_bindings.c.logical_id==s.storage_journal.c.logical_id).exists())):fail()
        return first

    def _settled(self,unit,grant,master_key,roots):
        side=unit['_side'];record,value=recorded(unit,grant,side,self.now)
        historical={**grant,'epoch':value['epoch'],'runner_id':record['authorization']['payload']['runner_id']}
        chosen=(source_selected if side=='source' else target_selected)(unit,historical)
        authorization(record['authorization'],master_key,historical,chosen,value['completed_at'])
        target=removed(record['target_receipt'],grant['target_key'],master_key,grant,chosen,self.now)
        source=retained(record['source_receipt'],master_key,grant,chosen,self.now,target_receipt=record['target_receipt'])
        if unit['committed_at'] or unit['cleanup_after'] or unit['publication'].get('commit'):fail()
        if side=='source':
            prior=unit['publication'].get('cancel_retained')
            if (not isinstance(prior,dict) or prior.keys()!={'version','authorization','target_receipt','receipt'}
                    or type(prior['version']) is not int or prior['version']!=1 or prior['receipt']!=record['source_receipt']
                    or prior['target_receipt']!=record['target_receipt'] or value['root_sha256']!=digest(roots)):fail()
            proof=source_proof(unit['publication'].get('source_verification'),{**grant,'epoch':source['epoch']},chosen,source['completed_at'])
            retained(prior['receipt'],master_key,grant,chosen,self.now,authorization=prior['authorization'],target_receipt=prior['target_receipt'],proof=proof)
            fresh=source_proof(unit['publication'].get('cancel_settlement_source_verification'),historical,chosen,value['completed_at'])
            if (digest(fresh)!=value['fresh_source_verification_sha256'] or source['source_roots_sha256']!=digest(roots)
                    or roots['media' if unit['resource_kind']=='media' else 'recordings'].get('exists') is not True
                    or any(observed['root'][name]!=roots['media' if unit['resource_kind']=='media' else 'recordings'].get(name)
                        for observed in (proof,fresh) for name in ('device','inode','mode'))):fail()
        else:
            progress=unit['publication'].get('target_cancel')
            if (not isinstance(progress,dict) or progress.get('receipt')!=record['target_receipt'] or progress.get('step')!='receipt'
                    or value['root_sha256']!=digest(roots[unit['resource_kind']])
                    or target['root_sha256']!=digest(roots[unit['resource_kind']])
                    or target['initial_proof_sha256']!=digest(progress.get('initial_proof'))
                    or (progress.get('deleted_count'),progress.get('deleted_bytes'))!=(target['deleted_count'],target['deleted_bytes'])
                    or (progress.get('receipt_count'),progress.get('receipt_bytes'))!=(unit['object_count'],unit['total_bytes'])
                    or progress.get('roster_sha256')!=target['roster_sha256'] or progress.get('receipt_roster')!=target['roster_sha256']):fail()
            if progress.get('version')==2:
                if (target['format']!=PRIVATE_RECEIPT or any(progress.get(name)!=target[name] for name in
                        ('processed_count','processed_bytes','absent_count')) or
                        any(progress.get('receipt_'+name)!=target[name] for name in ('deleted_count','deleted_bytes','absent_count'))):fail()
            elif target['format']==PRIVATE_RECEIPT:fail()
            # The progress validator receives a clock only, with no live
            # transfer controller, store, filesystem roots or transport.
            TargetCancellation(SimpleNamespace(store=None,wall=lambda:self.now))._progress(
                {name:item for name,item in progress.items() if name!='receipt'},grant,unit,master_key)
            removed(record['target_receipt'],grant['target_key'],master_key,grant,chosen,self.now,proof=progress['final_proof'])
            source_settled=settled(record['source_settlement'],grant,'source',chosen,self.now,source_receipt=record['source_receipt'],target_receipt=record['target_receipt'])
            if (source_settled['root_sha256']!=source['source_roots_sha256'] or source_settled['completed_at']<source['completed_at']
                    or value['fresh_source_verification_sha256']!=source_settled['fresh_source_verification_sha256']):fail()
        return record['receipt']

    async def _units(self,conn,header,grant,master_key,control,source):
        side=header['side'];request=header['_request'];count=objects=total=committed_units=committed_objects=committed_bytes=0
        transcript=INITIAL_UNITS_SHA256;last='';target_census=digest(['cancel-target-final',1])
        checker=CommitPeers(SimpleNamespace(store=self.store,wall=lambda:self.now))
        roots=control['source_roots'] if side=='source' else control['staging_roots']
        async for raw in pages(conn,s.units,s.units.c.task_id==header['task_id'],fields={'publication':JSON_LIMIT},key=s.units.c.unit_id):
            unit={**raw,'publication':decoded(raw,'publication'),'_side':side};kind=unit['resource_kind'];parent=unit['parent_path']
            description=dict(unit_id=unit['unit_id'],resource_kind=kind,source_parent=parent if side=='source' or kind=='media' else request['source_relationship']+'/'+parent.rsplit('/',1)[-1],
                target_parent=parent if side=='target' or kind=='media' else request['target_relationship']+'/'+parent.rsplit('/',1)[-1],
                source_version=unit['source_version'],object_count=unit['object_count'],total_bytes=unit['total_bytes'])
            self.parent.reservations._unit(header,description)
            if unit['state']=='cancelled':proof=self._settled(unit,grant,master_key,roots)
            else:
                if (unit['state']!=('cleaned' if side=='source' else 'committed') or not 0<unit['committed_at']<unit['cleanup_after']<=self.now
                        or unit['cleanup_after']-unit['committed_at']!=3600):fail()
                commit=unit['publication'].get('commit');checksum=checker._record(commit,grant)
                if (checksum!=unit['publication'].get('commit_sha256') or commit['committed_at']!=unit['committed_at'] or commit['cleanup_after']!=unit['cleanup_after']
                        or any(commit[name]!=description[other] for name,other in (('unit_id','unit_id'),('resource_kind','resource_kind'),('source_parent','source_parent'),
                            ('target_parent','target_parent'),('unit_version','source_version'),('object_count','object_count'),('total_bytes','total_bytes')))):fail()
                if side=='source':
                    if digest(unit['publication'].get('source_verification'))!=commit['source_receipt_sha256']:fail()
                    proof=None
                else:
                    if unit['publication'].get('receipt_sha256')!=commit['target_receipt_sha256']:fail()
                    proof=unit['publication'].get('target_live_verification');value=proof['payload']
                    live_proof(proof,{**grant,'epoch':value['epoch']},commit,value['verified_at'])
                committed_units+=1;committed_objects+=unit['object_count'];committed_bytes+=unit['total_bytes']
            rows=(await conn.execute(select(func.count(),func.coalesce(func.sum(s.storage_journal.c.expected_bytes),0)).where(
                s.storage_journal.c.task_id==header['task_id'],s.storage_journal.c.unit_id==unit['unit_id']))).one()
            expected=(0,0) if side=='source' and unit['state']=='cancelled' else (unit['object_count'],unit['total_bytes'])
            if tuple(rows)!=expected:fail()
            if side=='target' and unit['state']=='cancelled':
                roster=digest([]);physical=dict(deleted_count=0,deleted_bytes=0,absent_count=0)
                progress=unit['publication']['target_cancel'];private=progress.get('version')==2
                async for row in pages(conn,s.storage_journal,(s.storage_journal.c.task_id==header['task_id'])&(s.storage_journal.c.unit_id==unit['unit_id']),
                        fields={'publication':JSON_LIMIT},key=s.storage_journal.c.logical_id):
                    roster=digest([roster,row['logical_id'],row['cleanup_version']])
                    if private:
                        intent=decoded(row,'publication')['private_cancel']['intent'];present=intent['present']
                        physical['deleted_count']+=int(present);physical['absent_count']+=int(not present)
                        physical['deleted_bytes']+=intent['stage']['bytes'] if present else 0
                if roster!=progress['roster_sha256'] or private and any(physical[name]!=progress[name] for name in physical):fail()
            if side=='target':target_census=digest([target_census,unit['unit_id'],unit['state'],digest(proof)])
            transcript=unit_transcript(transcript,description);count+=1;objects+=unit['object_count'];total+=unit['total_bytes'];last=unit['unit_id']
        if (count,objects,total,transcript,last)!=(header['expected_units'],header['expected_objects'],header['expected_bytes'],header['units_sha256'],header['last_unit_id']):fail()
        if (committed_units,committed_objects,committed_bytes)!=(source['committed_units'],source['committed_objects'],source['committed_bytes']):fail()
        return target_census

    async def verify(self,conn,node,header,payload,request,control):
        side=header['side'];key='cancel_release' if side=='source' else 'cancel_target_release'
        if (header['state']!='released' or key not in payload or ('cancel_target_release' if side=='source' else 'cancel_release') in payload
                or payload.get('final_receipt') is not None or payload.get('source_final_receipt') is not None
                or control.get('phase')!='finalize' or control.get('cancel_started') is not True
                or any(header['accepted_'+name]!=header['expected_'+name] for name in ('units','objects','bytes'))):fail()
        common=await self._objects(conn,node,header,request,control)
        grant={**common,'epoch':control['epoch'],'runner_id':control['runner_id'],'purpose':'finalize','expires_at':control['expires_at']}
        master_key=await conn.scalar(select(fs.relationships.c.peer_key).where(fs.relationships.c.relationship_id==header['relationship_id']))
        record=payload[key]
        if side=='source':witness=record
        else:
            if not isinstance(record,dict) or record.keys()!={'version','source_record','preparation','receipt'} or type(record['version']) is not int or record['version']!=1:fail()
            witness=record['source_record']
        source=source_record(witness,grant,(header['expected_units'],header['expected_objects'],header['expected_bytes']),master_key,self.now)
        census=await self._units(conn,header,grant,master_key,control,source)
        if side=='source':
            if source['roots_sha256']!=digest(control['source_roots']) or not header['created_at']<=source['completed_at']<=header['updated_at']:fail()
        else:
            target=released(record['receipt'],grant,source,witness,control['staging_roots'],record['preparation'],self.now)
            if target['units_sha256_current']!=census or not header['created_at']<=target['completed_at']<=header['updated_at']:fail()
