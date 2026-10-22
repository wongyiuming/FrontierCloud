"""Actual throttled browser disconnect/refresh and unchanged idle recovery."""
import hashlib
import io
import json
import time
import wave

from tests import federation_stack as fleet


def state(master):
    return json.loads(master.web("""import asyncio,json
from sqlalchemy import select
from app.core.db import engine
from app.core.config import settings
from app.services.federation import schema as s
async def read():
 async with engine.connect() as conn:
  sessions=[dict(row) for row in (await conn.execute(select(s.upload_sessions.c.upload_id,s.upload_sessions.c.media_path,
   s.upload_sessions.c.expected_bytes,s.upload_sessions.c.state,s.upload_sessions.c.updated_at,s.upload_sessions.c.expires_at).where(
    s.upload_sessions.c.media_path.like('music/fault-interrupted-%')))).mappings()]
  members=[dict(row) for row in (await conn.execute(select(s.storage_members.c.member_id,s.storage_members.c.used_bytes,s.storage_members.c.reserved_bytes))).mappings()]
  return dict(sessions=sessions,members=members,inactivity_seconds=settings.ADMIN_UPLOAD_INACTIVITY_TIMEOUT)
print(json.dumps(asyncio.run(read())))
"""))


def exercise(browser,nodes,root,report,check):
    master,direct,relay=nodes
    master.mode('Relay',relation=relay.relation)
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as output:
        output.setparams((1,2,8000,0,'NONE','not compressed'));output.writeframes(b'\x02\0'*(8*1024**2))
    payload=buffer.getvalue();digest=hashlib.sha256(payload).hexdigest()
    for site in ('primary','direct','relay'):
        row=master.upload_media(payload,site+'.wav',site_type=site,target_dir='music/upload-baseline-'+site)
        with master.client.stream('GET',master.endpoint+row['url']) as response:
            assert response.status_code==200
            first=next(response.iter_bytes(1024));assert len(first)==1024
        response=master.client.get(master.endpoint+row['url'],headers={'Range':'bytes=3-9'})
        check(site+'_cancelled_download_keeps_original_range',response.status_code==206 and response.content==payload[3:10])
        report['inputs'].append(dict(action='close actual HTTP download after first chunk then Range',site_type=site,
            path=row['url'],first_bytes=1024,range='bytes=3-9',status=response.status_code))
    before_catalog=master.resources();before=state(master)
    check('original_five_minute_inactivity_boundary',before['inactivity_seconds']==300)
    report['upload_faults']=[]
    for site,receiver in [('primary',master),('direct',direct),('relay',relay)]:
        for mode in ('明文落盘','加密落盘'):
            parent='fault-interrupted-'+site+('-plain' if mode=='明文落盘' else '-encrypted')
            folder=root/parent;folder.mkdir();(folder/'sample.wav').write_bytes(payload)
            context,page=fleet.admin_page(browser,master);reservations=[]
            context.add_init_script("""(() => {
 const send=XMLHttpRequest.prototype.send;window.faultUpload={loaded:0,total:0,events:0};
 XMLHttpRequest.prototype.send=function(body){
  if(body instanceof Blob)this.upload.addEventListener('progress',event=>{window.faultUpload={loaded:event.loaded,total:event.total,events:window.faultUpload.events+1};});
  return send.call(this,body);
 };
})();""")
            def observed(response):
                if response.request.method=='POST' and response.url.endswith('/admin/upload/session') and response.status==200:
                    value=response.json();reservations.append(dict(upload_id=value['upload_id'],media_id=value['media_id'],transport=value['transport']))
            page.on('response',observed)
            page.on('dialog',lambda dialog:dialog.accept())
            try:
                page.reload(wait_until='domcontentloaded')
                cdp=context.new_cdp_session(page);cdp.send('Network.enable')
                cdp.send('Network.emulateNetworkConditions',dict(offline=False,latency=10,downloadThroughput=-1,uploadThroughput=128*1024))
                heading=page.locator('[data-admin-module="media"] .module-heading')
                if heading.get_attribute('aria-expanded')!='true':heading.click()
                page.locator('.tree-row[data-path="music"]').dblclick();fleet.wait_page(page,"currentPath==='music'")
                page.select_option('#uploadSiteType',site);page.locator('#uploadBtn').click();page.locator('#uploadFolder').click()
                with page.expect_file_chooser() as selected:page.get_by_role('button',name=mode,exact=True).click()
                selected.value.set_files(str(folder))
                fleet.wait_page(page,'window.faultUpload.loaded>=256*1024 && window.faultUpload.loaded<window.faultUpload.total',timeout=90000)
                assert len(reservations)==1
                transfer=page.evaluate('window.faultUpload');upload_id=reservations[0]['upload_id']
                pending=next(row for row in state(master)['sessions'] if row['upload_id']==upload_id)
                part_id=upload_id if site=='primary' else reservations[0]['media_id']
                part=receiver.data/'media'/('.cluster-upload-'+part_id+'.part')
                deadline=time.monotonic()+10
                while not part.exists() and time.monotonic()<deadline:page.wait_for_timeout(100)
                part_bytes=part.stat().st_size if part.exists() else 0
                report['current_fault_observation']=dict(site_type=site,storage_mode=mode,upload_id=upload_id,part_id=part_id,
                    receiver_partial_bytes=part_bytes,expected_bytes=pending['expected_bytes'],actual_xhr_progress=transfer)
                check(parent+'_receiver_has_actual_incomplete_bytes',0<part_bytes<pending['expected_bytes'])
                conflict=dict(site_type=site,target_dir='music',relative_path=parent+'/sample.wav',filename='sample.wav',
                    size_bytes=len(payload),storage_mode='plain')
                master.api('/api/v1/media/admin/upload/session',conflict,expected=409)
                check(parent+'_active_path_conflict_refuses_second_reservation',True)
                record=dict(site_type=site,storage_mode=mode,parent=parent,upload_id=upload_id,part_id=part_id,actual_xhr_progress=transfer,
                    receiver_partial_bytes=part_bytes,expected_bytes=pending['expected_bytes'],observed_at=time.time(),
                    actual_network_throttle_bytes_per_second=128*1024,source_sha256=digest)
                report['upload_faults'].append(record)
                report['inputs'].append(dict(action='GUI folder upload then '+('network offline' if mode=='明文落盘' else 'actual page refresh'),
                    site_type=site,storage_mode=mode,parent=parent,source_bytes=len(payload),upload_id=upload_id))
                if mode=='明文落盘':
                    context.set_offline(True)
                    fleet.wait_page(page,"document.querySelector('#uploadSummary').textContent.startsWith('完成：')",timeout=90000)
                    text=page.locator('#uploadSummary').inner_text();record['failure_summary']=text
                    check(parent+'_network_failure_is_visible','成功 0，失败 1' in text)
                    context.set_offline(False)
                    page.reload(wait_until='domcontentloaded')
                else:
                    page.reload(wait_until='domcontentloaded')
                    check(parent+'_actual_refresh_interrupts_upload',True)
            finally:context.close()
    retained=state(master);report['after_browser_interruptions']=retained
    check('uncompleted_uploads_do_not_publish_catalog_objects',master.resources()==before_catalog)
    master.compose('restart','web')
    fleet.wait_for(lambda:master.client.get(master.endpoint+'/health/ready').status_code==200)
    report['inputs'].append(dict(action='actual Compose Web restart with unchanged mounts',project=master.project))
    started=time.monotonic();deadline=started+540;observations=[]
    while time.monotonic()<deadline:
        current=state(master);observations.append(dict(clock=time.time(),states=[row['state'] for row in current['sessions']],
            reserved_bytes=sum(row['reserved_bytes'] for row in current['members'])))
        if not any(row['state']=='reserved' for row in current['sessions']) and all(row['reserved_bytes']==0 for row in current['members']):break
        time.sleep(5)
    else:raise AssertionError('Normal inactivity worker did not settle interrupted filesystem uploads')
    final=state(master);report['idle_recovery']=dict(observations=observations,seconds=round(time.monotonic()-started,3),final=final,
        product_deadlines_changed=False,manual_cleanup_called=False)
    check('normal_worker_refunds_all_interrupted_reservations_after_restart',not final['sessions']
        and all(row['reserved_bytes']==0 for row in final['members']))
    before_used={row['member_id']:row['used_bytes'] for row in before['members']}
    check('worker_does_not_reclaim_original_owned_media',master.resources()==before_catalog and
        {row['member_id']:row['used_bytes'] for row in final['members']}==before_used)
    check('all_known_partial_payload_files_are_gone',all(not (receiver.data/'media'/('.cluster-upload-'+identifier+'.part')).exists()
        for row in report['upload_faults'] for receiver in nodes for identifier in {row['upload_id'],row['part_id']}))
    for site,node in [('primary',master),('direct',direct),('relay',relay)]:
        with (node.data/('media/music/upload-baseline-'+site)/(site+'.wav')).open('rb') as stream:
            check(site+'_original_file_sha256_unchanged',hashlib.file_digest(stream,'sha256').hexdigest()==digest)
    report.update(independent_compose_nodes=3,filesystem_sites=['primary','direct','relay'],browser_interruption_cases=6,
        actual_master_restart=True,actual_inactivity_seconds=300,baseline_sha256=digest)
