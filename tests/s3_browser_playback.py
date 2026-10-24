"""Actual large MP3 and H.264/AAC browser playback on the private S3 fixture."""
import datetime
import hashlib
import os
from pathlib import Path
import ssl
import time
from urllib.parse import parse_qs, urlsplit

import httpx

from tests import federation_stack as fleet


FIXTURES=Path(__file__).resolve().parent/'fixtures'


def read_expiry(page,node,directory,long_track,long_index,next_index,signed,transfers,report,check):
    page.evaluate('(index)=>selectMedia(index)',long_index)
    fleet.wait_page(page,f'currentIndex==={long_index} && art.currentTime>0.1 && art.duration>8000',timeout=30000)
    page.evaluate('() => art.pause()')
    stale=signed[0]
    delay=stale['expires_at']+2-time.time()
    check('s3_read_grant_has_real_bounded_deadline',0<delay<310)
    started=time.monotonic()
    while time.time()<stale['expires_at']+2:
        page.wait_for_timeout(min(1000,max(1,int((stale['expires_at']+2-time.time())*1000))))
    with httpx.Client(verify=ssl.create_default_context(cafile=str(directory/'roots.pem')),trust_env=False,timeout=10) as client:
        expired=client.get(stale['url'],headers={'Range':'bytes=0-0'})
    check('s3_actual_expired_original_read_url_is_refused',expired.status_code==403)
    before=len(signed);before_bytes=sum(row['bytes'] for row in transfers)
    offset=70*1024**2
    with long_track.open('rb') as source:
        source.seek(offset);expected=hashlib.sha256(source.read(256)).hexdigest()
    result=page.evaluate("""async offset => {
        const row=currentMediaList.find(item=>item.media_path.endsWith('/01-long.mp3'));
        const url=FrontierMediaCrypto.virtualUrl(row.media_path,false,row.encryption.file_id);
        const response=await fetch(url,{headers:{Range:`bytes=${offset}-${offset+255}`},cache:'no-store'});
        const value=await response.arrayBuffer();return {status:response.status,bytes:value.byteLength,
            sha256:Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',value)),x=>x.toString(16).padStart(2,'0')).join('')};
    }""",offset)
    check('s3_expired_grant_renews_for_uncached_ciphertext_and_browser_decrypt',result==dict(status=206,bytes=256,sha256=expected))
    target=page.evaluate('async () => {await art.play(); const target=art.duration*0.53; art.currentTime=target; return target;}')
    try:
        fleet.wait_page(page,f'currentIndex==={long_index} && art.currentTime>{target+0.1} && !art.video.paused && !art.video.error',timeout=45000)
    except Exception:
        report['read_expiry_seek_failure']=page.evaluate('() => ({current_index:currentIndex,current_time:art.currentTime,duration:art.duration,paused:art.video.paused,media_error:art.video.error?.code||0,pipeline:frontierCloudContinuousAudio.status()})')
        raise
    check('s3_long_mse_seek_continues_after_real_read_grant_expiry',True)
    page.evaluate('() => {art.currentTime=art.duration-2; return art.play();}')
    fleet.wait_page(page,f'currentIndex==={next_index} && art.currentTime>0.1 && !art.video.paused && !art.video.error',timeout=45000)
    fresh=signed[before:]
    check('s3_long_album_automatic_next_survives_real_read_grant_expiry',bool(fresh)
        and all(row['expires_at']>stale['expires_at'] and row['url']!=stale['url'] for row in fresh))
    added=sum(row['bytes'] for row in transfers)-before_bytes
    check('s3_read_grant_renewal_stays_bounded',0<added<24*1024**2)
    report['read_grant_expiry']=dict(actual_clock_wait=True,wait_seconds=round(time.monotonic()-started,3),
        original_expires_at=stale['expires_at'],original_url_sha256=hashlib.sha256(stale['url'].encode()).hexdigest(),
        expired_url_status=expired.status_code,uncached_plaintext_range=result,new_grant_count=len(fresh),
        new_url_sha256=[hashlib.sha256(row['url'].encode()).hexdigest() for row in fresh],
        additional_ciphertext_bytes=added,resumed_before_seek=True,seek_target=target,manual_seek=True,real_tail_auto_next=True)


def sha256(path):
    result=hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):
            result.update(chunk)
    return result.hexdigest()


def run(browser,node,source,directory,report,check):
    context,page=fleet.admin_page(browser,node)
    context.add_init_script("""(() => {
      const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send,header=XMLHttpRequest.prototype.setRequestHeader;
      const seen=new WeakMap(),uploads=new Map();window.s3PlaybackInputs=[];
      XMLHttpRequest.prototype.open=function(method,url,...rest){
        const parsed=new URL(url,location.href),upload=parsed.pathname+':'+parsed.searchParams.get('uploadId');
        if(!uploads.has(upload))uploads.set(upload,uploads.size+1);
        seen.set(this,{method,origin:parsed.origin,upload_ordinal:uploads.get(upload),part_number:Number(parsed.searchParams.get('partNumber'))});
        return open.call(this,method,url,...rest);
      };
      XMLHttpRequest.prototype.setRequestHeader=function(name,value){
        if(name.toLowerCase()==='x-amz-checksum-sha256'&&seen.has(this))seen.get(this).checksum_sha256=value;
        return header.call(this,name,value);
      };
      XMLHttpRequest.prototype.send=function(body){const row=seen.get(this);if(row&&body instanceof Blob)window.s3PlaybackInputs.push({...row,bytes:body.size,with_credentials:this.withCredentials});return send.call(this,body);};
    })();""")
    files=[]
    try:
        album=source/'long-album';album.mkdir()
        tone=(FIXTURES/'tone.mp3').read_bytes()
        long_track=album/'01-long.mp3'
        with long_track.open('wb') as output:
            for _ in range((129*1024**2)//len(tone)+1):
                output.write(tone)
        (album/'02-next.mp3').write_bytes(tone*3)
        files.append(dict(kind='music',parent='long-album',mode='加密落盘',
            names=['01-long.mp3','02-next.mp3'],folder=album))
        for name,mode in (('plain-video','明文落盘'),('encrypted-video','加密落盘')):
            folder=source/name;folder.mkdir()
            (folder/'clip.mp4').write_bytes((FIXTURES/'tone.mp4').read_bytes())
            files.append(dict(kind='vido',parent=name,mode=mode,names=['clip.mp4'],folder=folder))
        for item in files:
            page.reload(wait_until='domcontentloaded')
            heading=page.locator('[data-admin-module="media"] .module-heading')
            if heading.count() and heading.get_attribute('aria-expanded')!='true':heading.click()
            page.locator(f'.tree-row[data-path="{item["kind"]}"]').dblclick()
            fleet.wait_page(page,'currentPath === '+repr(item['kind']))
            page.select_option('#uploadSiteType','s3')
            page.locator('#uploadBtn').click();page.locator('#uploadFolder').click()
            with page.expect_file_chooser() as choose:
                page.get_by_role('button',name=item['mode'],exact=True).click()
            choose.value.set_files(str(item['folder']))
            report['actions'].append(dict(action='GUI playback-fixture upload',kind=item['kind'],
                parent=item['parent'],mode=item['mode'],files=[dict(name=name,bytes=(item['folder']/name).stat().st_size,
                    sha256=sha256(item['folder']/name)) for name in item['names']]))
            fleet.wait_page(page,"document.querySelector('#uploadSummary').textContent.startsWith('完成：')",timeout=180000)
            summary=page.locator('#uploadSummary').inner_text()
            check('gui_'+item['parent']+'_upload_confirmed',f'成功 {len(item["names"])}，失败 0' in summary)
            report.setdefault('playback_upload_inputs',[]).extend(dict(row,album=item['parent']) for row in page.evaluate('window.s3PlaybackInputs'))
        report['playback_media']=[{key:value for key,value in item.items() if key!='folder'} for item in files]
        parents={item['kind']+'/'+item['parent'] for item in files}
        rows=[row for row in node.resources() if row['path'].rsplit('/',1)[0] in parents]
        actual=report['playback_upload_inputs']
        report['playback_uploaded_objects']=rows
        report['playback_upload_observation']=dict(actual_blob_bytes=sum(row['bytes'] for row in actual),
            catalog_bytes=sum(row['size'] for row in rows),blob_count=len(actual),object_count=len(rows))
        unique={}
        for row in actual:
            identity=(row['album'],row['upload_ordinal'],row['part_number'])
            value=(row['bytes'],row.get('checksum_sha256'))
            if identity in unique and unique[identity]!=value:raise AssertionError('A retried part changed its size or checksum')
            unique[identity]=value
        check('s3_upload_retries_keep_immutable_part_checksums',all(value[1] and len(value[1])==44 for value in unique.values()))
        report['playback_upload_observation'].update(unique_part_bytes=sum(row[0] for row in unique.values()),
            unique_parts=len(unique),duplicate_attempts=len(actual)-len(unique))
        check('large_audio_and_video_payloads_go_directly_to_s3',len(rows)==4
            and sum(row[0] for row in unique.values())==sum(row['size'] for row in rows)
            and sum(row['bytes'] for row in actual)>=sum(row['size'] for row in rows)
            and all(row['method']=='PUT' and row['origin']=='https://s3.4399.com:8333'
                and row['bytes']<=8*1024**2 and not row['with_credentials'] for row in actual))
    finally:
        context.close()

    context=browser.new_context()
    page=context.new_page()
    transfers=[];keys=[];signed=[]
    def response_seen(response):
        url=urlsplit(response.url)
        if url.hostname=='s3.4399.com' and response.request.method=='GET':
            headers=response.headers
            transfers.append(dict(status=response.status,bytes=int(headers.get('content-length','0')),
                content_range=headers.get('content-range',''),range=response.request.headers.get('range','')))
            query=parse_qs(url.query)
            if 'X-Amz-Date' in query and 'X-Amz-Expires' in query:
                issued=datetime.datetime.strptime(query['X-Amz-Date'][0],'%Y%m%dT%H%M%SZ').replace(tzinfo=datetime.timezone.utc).timestamp()
                signed.append(dict(url=response.url,expires_at=issued+int(query['X-Amz-Expires'][0])))
        elif url.path=='/api/v1/media/crypto/key':
            keys.append(response.status)
    context.on('response',response_seen)
    page.on('pageerror',lambda error:report['page_errors'].append(str(error).split('\n')[0][:120]))
    try:
        page.goto(node.endpoint+'/api/v1/media/music/category?path=music%2Flong-album',wait_until='domcontentloaded')
        fleet.wait_page(page,"typeof art !== 'undefined' && art && currentMediaList.length === 2",timeout=30000)
        long_index=page.evaluate("currentMediaList.findIndex(row=>row.media_path.endsWith('/01-long.mp3'))")
        next_index=1-long_index
        page.evaluate('(index)=>selectMedia(index)',long_index)
        fleet.wait_page(page,"frontierCloudContinuousAudio.status().active && art.currentTime>0.2 && art.duration>8000 && !art.video.error",timeout=30000)
        check('encrypted_s3_above_128mib_starts_bounded_mse',long_track.stat().st_size>128*1024**2)
        start=page.evaluate('frontierCloudContinuousAudio.status()')
        page.evaluate('(index)=>selectMedia(index)',next_index)
        fleet.wait_page(page,f'currentIndex==={next_index} && art.currentTime>0.1 && !art.video.paused && !art.video.error',timeout=30000)
        check('s3_long_album_manual_next',True)
        page.evaluate('(index)=>selectMedia(index)',long_index)
        fleet.wait_page(page,f'currentIndex==={long_index} && art.currentTime>0.1 && art.duration>8000',timeout=30000)
        page.evaluate('() => {art.currentTime=art.duration-2; return art.play();}')
        fleet.wait_page(page,f'currentIndex==={next_index} && art.currentTime>0.1 && !art.video.paused && !art.video.error',timeout=45000)
        final=page.evaluate('frontierCloudContinuousAudio.status()')
        check('s3_long_mp3_real_end_automatically_starts_next',not final['pipeline_error'])
        music_transfers=list(transfers)
        check('s3_long_playback_uses_ranges_without_full_download',any(row['status']==206 and row['content_range'] for row in music_transfers)
            and sum(row['bytes'] for row in music_transfers)<32*1024**2)
        report['large_audio']=dict(source_bytes=long_track.stat().st_size,source_sha256=sha256(long_track),
            manual_next=True,real_end_auto_next=True,start_status=start,final_status=final,s3_responses=music_transfers,
            elapsed_whole_track=False,seek_to_real_tail=True)
        if os.environ.get('S3_BROWSER_READ_EXPIRY')=='1':
            read_expiry(page,node,directory,long_track,long_index,next_index,signed,transfers,report,check)
        for name in ('plain-video','encrypted-video'):
            page.goto(node.endpoint+'/api/v1/media/video/category?path=vido%2F'+name,wait_until='domcontentloaded')
            fleet.wait_page(page,"typeof art !== 'undefined' && art && currentMediaList.length === 1",timeout=30000)
            page.evaluate('() => selectMedia(0)')
            fleet.wait_page(page,'art.video.currentTime>0.2 && !art.video.paused && !art.video.error && art.video.webkitDecodedFrameCount>0 && art.video.webkitAudioDecodedByteCount>0',timeout=30000)
            page.evaluate('() => {art.currentTime=2; return art.play();}')
            fleet.wait_page(page,'art.video.currentTime>2.1 && !art.video.error',timeout=30000)
            state=page.evaluate('() => ({width:art.video.videoWidth,height:art.video.videoHeight,muted:art.video.muted,audio_bytes:art.video.webkitAudioDecodedByteCount,video_frames:art.video.webkitDecodedFrameCount})')
            check('s3_'+name+'_actual_audio_video_decode_and_seek',state['width']==160 and state['height']==90 and not state['muted'])
            report.setdefault('video_decode',{})[name]=state
        # A worker's warm ciphertext cache may survive a page reload. It must
        # still ask the current page for a fresh server authorization decision.
        before_gets=len(transfers);before_keys=len(keys)
        page.reload(wait_until='domcontentloaded')
        fleet.wait_page(page,"typeof art !== 'undefined' && art && currentMediaList.length === 1",timeout=30000)
        page.evaluate('() => selectMedia(0)')
        fleet.wait_page(page,'art.video.currentTime>0.2 && !art.video.error',timeout=30000)
        check('s3_warm_ciphertext_reuse_keeps_fresh_authorization',len(keys)>before_keys and all(status==200 for status in keys[before_keys:]) and len(transfers)==before_gets)
        report['ciphertext_reuse']=dict(page_reload=True,worker_kept_alive=True,new_key_requests=len(keys)-before_keys,
            additional_s3_gets=len(transfers)-before_gets,persistent_browser_storage=False)
        check('s3_extended_playback_no_script_errors',not report['page_errors'])
    finally:
        context.close()
