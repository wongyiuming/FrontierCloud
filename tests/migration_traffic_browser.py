"""Actual five-node business traffic, durable manual pause and quiet recovery."""
import hashlib
import io
import json
import ssl
import threading
import time
import uuid
import wave

import httpx

def exercise(nodes,root,report,check):
    master,source,target,*load_nodes=nodes
    inputs=report['inputs'];readers=[]
    def api(path,value=None):
        inputs.append(dict(method='GET' if value is None else 'POST',path=path,body=value))
        return master.api('/api/v1/media/admin/migrations'+path,value)
    def gateway():
        return json.loads(master.web("""import httpx,json
from app.services.business_traffic import SOCKET
with httpx.Client(transport=httpx.HTTPTransport(uds=SOCKET),timeout=2) as client:
 print(json.dumps(client.get('http://gateway/traffic').json()))
"""))
    def wait(predicate,timeout=180):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            value=predicate()
            if value:return value
            time.sleep(.2)
        raise AssertionError('Five-node native condition timed out')
    def allocation(selected):
        for peer in nodes[1:]:
            master.mode('Direct' if peer is selected else 'Relay',relation=peer.relation)
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as output:
        output.setparams((1,2,8000,0,'NONE','not compressed'));output.writeframes(b'\x01\0'*(32*1024**2))
    payload=buffer.getvalue();digest=hashlib.sha256(payload).hexdigest()
    allocation(source)
    originals=[master.upload_media(payload,f'part-{index}.wav',site_type='direct',target_dir='music/traffic-source') for index in range(4)]
    loads=[]
    for index,peer in enumerate(load_nodes):
        allocation(peer)
        loads.append(master.upload_media(payload,f'load-{index}.wav',site_type='direct',target_dir='music/traffic-load-'+str(index)))
        master.mode('Relay',relation=peer.relation)
    for peer in nodes[1:]:
        master.mode('Direct' if peer is source else 'Relay',relation=peer.relation)
    source_id,target_id=source.nodes()['node_id'],target.nodes()['node_id']
    report['readonly_plan_attempts']=[]
    for attempt in range(3):
        preview=api('/plans',dict(request_id=uuid.uuid4().hex,source_id=source_id,target_id=target_id));plan_id=preview['plan_id']
        def plan_ready():
            value=api('/plans/'+plan_id)
            return value if value['state'] in ('ready','blocked','failed') else None
        preview=wait(plan_ready)
        report['readonly_plan_attempts'].append(preview)
        if preview['state']=='ready':break
        if attempt<2:time.sleep(35)
    else:raise AssertionError('Actual readonly planning did not become ready')
    report['traffic_preview']={key:preview.get(key) for key in ('state','object_count','total_bytes','review_sha256')}
    task=api('/plans/'+plan_id+'/start',dict(approved_sha256=preview['review_sha256'],confirm_source_id=source_id,confirm_target_id=target_id))
    task_id=task['task_id']
    def observe():return api('/tasks/'+task_id)
    def control(action):
        for attempt in range(5):
            task=observe();value=dict(action=action,revision=task['revision']);path='/tasks/'+task_id
            inputs.append(dict(method='POST',path=path,body=value,revision_retry=attempt))
            response=master.client.post(master.endpoint+'/api/v1/media/admin/migrations'+path,json=value,
                headers={'X-CSRF-Token':master.csrf})
            if response.status_code==200:return response.json()
            if response.status_code!=409 or attempt==4:raise AssertionError('Actual revision-bound control refused: '+str(response.status_code))
            time.sleep(.2)
    def active():
        value=observe()
        if value['state'] in ('cleanup_pending','completed','failed'):raise AssertionError(value)
        return value if value['state'] in ('queued','copying','verifying','committing') else None
    task=wait(active)
    task=control('pause')
    check('five_node_manual_pause_is_durable',task['state']=='paused' and task['pause_reason']=='manual')
    master.compose('restart','web')
    wait(lambda:master.client.get(master.endpoint+'/health/ready').status_code==200)
    task=observe();check('manual_pause_survives_actual_master_restart',task['state']=='paused' and task['pause_reason']=='manual')
    quiet=None;samples=[];deadline=time.monotonic()+100
    while time.monotonic()<deadline:
        view=gateway();samples.append(view)
        low=max(view['ingress_bytes_per_second'],view['egress_bytes_per_second'],view['recent_bytes_per_second'])<=64*1024 and view['active_business_requests']==0
        quiet=(quiet or time.monotonic()) if low else None
        if quiet and time.monotonic()-quiet>=35:break
        time.sleep(1)
    else:raise AssertionError('Actual gateway never completed its quiet interval')
    task=observe();check('quiet_traffic_does_not_resume_manual_pause',task['state']=='paused' and task['pause_reason']=='manual')
    report['manual_quiet_gateway_samples']=samples
    stop=threading.Event()
    def download(row,view):
        with httpx.Client(verify=ssl.create_default_context(cafile=str(master.ca)),trust_env=False,timeout=3) as client:
            while not stop.is_set():
                try:
                    with client.stream('GET',master.endpoint+row['url']) as response:
                        assert response.status_code==200
                        for chunk in response.iter_bytes(65536):
                            view['bytes']+=len(chunk)
                            if stop.wait(.1):return
                except httpx.HTTPError as error:
                    view['errors'].append(type(error).__name__)
                    if stop.wait(.2):return
    try:
        for row in loads:
            view=dict(bytes=0,errors=[]);thread=threading.Thread(target=download,args=(row,view),daemon=True)
            readers.append((thread,view));inputs.append(dict(method='GET',path=row['url'],transport='Relay',hostname=master.hostname));thread.start()
        def high():
            view=gateway()
            return view if max(view['egress_bytes_per_second'],view['recent_bytes_per_second'])>256*1024 and view['active_business_requests']>=2 else None
        report['actual_high_gateway']=wait(high,60)
        task=control('resume')
        def traffic_pause():
            value=observe()
            return value if value['state']=='paused' and value['pause_reason']=='traffic' else None
        task=wait(traffic_pause,60)
        check('manual_resume_under_real_business_load_enters_traffic_pause',True)
        committed=task['committed_bytes'];began=time.monotonic()
        while time.monotonic()-began<10:
            task=observe();assert task['state']=='paused' and task['pause_reason']=='traffic' and task['committed_bytes']==committed
            time.sleep(1)
        check('sustained_business_transfers_hold_heavy_migration',all(view['bytes']>1024**2 for _thread,view in readers))
    finally:
        stop.set()
        for thread,_view in readers:thread.join(6)
        assert all(not thread.is_alive() for thread,_view in readers)
        report['business_readers']=[view for _thread,view in readers]
    stopped=time.monotonic()
    def committed():
        value=observe()
        if value['state']=='failed':raise AssertionError(value)
        return value if value['state']=='cleanup_pending' else None
    task=wait(committed,240)
    check('observed_quiet_automatically_resumes_traffic_pause',time.monotonic()-stopped>=30 and task['committed_bytes']==4*len(payload))
    rows={row['resource_id']:row for row in master.resources()}
    def target_matches(row):
        with (target.data/'media'/row['path']).open('rb') as stream:
            return row['resource_id'] in rows and hashlib.file_digest(stream,'sha256').hexdigest()==digest
    check('five_node_cutover_keeps_logical_ids_and_plain_bytes',all(target_matches(row) for row in originals))
    check('source_originals_stay_during_real_cleanup_hour',all((source.data/'media'/row['path']).is_file() for row in originals))
    check('migration_payload_never_appears_on_master',all(not (master.data/'media'/row['path']).exists() for row in originals))
    report.update(independent_compose_nodes=5,actual_gateway_bytes=True,actual_master_restart=True,actual_hour=False,
        task_phase=task['state'],source_payload_sha256=digest,source_object_bytes=len(payload),source_object_count=4,
        quiet_resume_elapsed_seconds=round(time.monotonic()-stopped,3),traffic_task=task)
