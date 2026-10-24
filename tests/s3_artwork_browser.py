"""Plain S3 selected/visible-track metadata through ordinary browser Range reads."""
import hashlib

from tests import federation_stack as fleet
from tests.player_storage_browser import image,tag


def run(browser,node,source,report,check):
    folder=source/'plain-artwork-album';folder.mkdir()
    tone=(fleet.ROOT/'tests/fixtures/tone.mp3').read_bytes()
    names=['01-plain.mp3','02-plain.mp3'];titles=['Plain S3 first','Plain S3 second']
    pictures=[image(210,30),image(30,210)]
    for name,title,picture in zip(names,titles,pictures):
        (folder/name).write_bytes(tag(title,picture)+tone*20)
    context,page=fleet.admin_page(browser,node)
    try:
        heading=page.locator('[data-admin-module="media"] .module-heading')
        if heading.get_attribute('aria-expanded')!='true':heading.click()
        page.locator('.tree-row[data-path="music"]').dblclick();fleet.wait_page(page,"currentPath==='music'")
        page.select_option('#uploadSiteType','s3');page.locator('#uploadBtn').click();page.locator('#uploadFolder').click()
        with page.expect_file_chooser() as chosen:page.get_by_role('button',name='明文落盘',exact=True).click()
        chosen.value.set_files(str(folder))
        report['actions'].append(dict(action='GUI plain S3 tagged album upload',parent=folder.name,files=names))
        fleet.wait_page(page,"document.querySelector('#uploadSummary').textContent.startsWith('完成：')",timeout=90000)
        check('plain_s3_artwork_upload_confirmed','成功 2，失败 0' in page.locator('#uploadSummary').inner_text())
        report.setdefault('playback_media',[]).append(dict(kind='music',parent=folder.name,names=names,mode='明文落盘'))
    finally:context.close()
    context=browser.new_context();page=context.new_page();gets=[];range_inputs=[]
    def request_seen(request):
        if request.method=='GET' and request.url.startswith(node.endpoint+'/api/v1/media/stream?'):
            headers=request.all_headers()
            if headers.get('range'):range_inputs.append(dict(method='GET',range=headers['range'],resource_type=request.resource_type))
    context.on('request',request_seen)
    def response_seen(response):
        if response.url.startswith('https://s3.4399.com:8333/') and response.request.method=='GET':
            gets.append(dict(status=response.status,resource_type=response.request.resource_type,range=response.request.all_headers().get('range',''),
                content_range=response.headers.get('content-range',''),bytes=int(response.headers.get('content-length','0'))))
    context.on('response',response_seen)
    try:
        page.goto(node.endpoint+'/api/v1/media/music/category?path=music%2F'+folder.name,wait_until='domcontentloaded')
        fleet.wait_page(page,"typeof art!=='undefined' && art && currentMediaList.length===2",timeout=30000)
        check('plain_s3_metadata_does_not_require_encryption',page.evaluate('currentMediaList.every(row=>row.browser_metadata===true&&!row.encryption)'))
        page.evaluate('() => selectMedia(0)')
        fleet.wait_page(page,"art.currentTime>0.2 && !art.video.error && currentMediaList.every(row=>row.title.startsWith('Plain S3 '))",timeout=45000)
        check('plain_s3_selected_and_visible_rows_load_their_own_tags',
            all(title in page.locator('.media-title').all_inner_texts() for title in titles))
        actual=page.evaluate("""async () => Promise.all(currentMediaList.map(async row=>{
            const response=await fetch(row.url,{headers:{Range:'bytes=0-4095'},cache:'no-store'});
            const tags=FrontierMusicMetadata.parse(new Uint8Array(await response.arrayBuffer()));
            const size=await new Promise((resolve,reject)=>{const image=new Image();image.onload=()=>resolve([image.naturalWidth,image.naturalHeight]);image.onerror=()=>reject(new Error('Missing original S3 picture'));image.src=row.cover;});
            return {title:row.title,size,picture_sha256:Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',tags.picture.bytes)),x=>x.toString(16).padStart(2,'0')).join('')};
        }))""")
        expected={title:hashlib.sha256(picture).hexdigest() for title,picture in zip(titles,pictures)}
        check('plain_s3_each_original_picture_keeps_its_bytes_and_dimensions',all(
            row['size']==[64,64] and row['picture_sha256']==expected[row['title']] for row in actual))
        report['plain_s3_artwork']=dict(originals=actual,master_range_inputs=range_inputs,s3_gets=gets,selected_and_visible_loaded=True,
            observed_payload_path='browser-to-provider',master_payload_reads_independently_measured=False)
        ranges=[row for row in gets if row['content_range'] and row['resource_type']=='fetch']
        check('plain_s3_tag_reads_use_direct_bounded_ranges',sum(row['range']=='bytes=0-9' for row in range_inputs)>=2
            and sum(row['content_range'].startswith('bytes 0-9/') for row in ranges)>=2
            and all(row['status']==206 and row['content_range'] and row['bytes']<=1024*1024 for row in ranges))
    finally:context.close()
