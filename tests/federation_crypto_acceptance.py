"""Actual encrypted uploads, Service Worker delivery and UI on a disposable fleet.

All media encryption/decryption below executes in Chromium using the deployed
compiled application scripts. Python reads disk bytes only to assert opacity.
"""
from __future__ import annotations

import hashlib
import json
import ssl
import base64
from pathlib import Path

import httpx
from playwright.sync_api import expect


def check(browser, nodes, source, report):
    from tests.federation_stack import admin_page, wait_page
    from tests.admin_focus_browser import verify_live_navigation
    master, direct, relay = nodes
    for site in ("primary", "direct", "relay"):
        master.upload_media(source, "seed.wav", site_type=site, target_dir="music/crypto-"+site)
    context, page = admin_page(browser, master)
    failures, reservations, sessions = [], {}, []
    page.on("pageerror", lambda error: failures.append(str(error)))
    page.on("dialog", lambda dialog: (failures.append(dialog.message), dialog.dismiss()))
    def observed(request):
        if request.method == "POST" and request.url.endswith("/api/v1/media/admin/upload/session"):
            payload=request.post_data_json
            if payload.get("storage_mode")=="encrypted":reservations[payload["filename"]]=payload
        if request.method=="POST" and request.url.endswith("/api/v1/media/admin/crypto/session"):
            sessions.append(request.url)
    page.on("request",observed)
    verify_live_navigation(page)
    master.api('/api/v1/media/admin/security/blocks?sort_order=ip_asc&page_size=2')
    page.locator('[data-admin-module="security"] .module-heading').click()
    page.evaluate('async () => await loadSecurityStatus(true)')
    assert not page.locator('#securitySummary').text_content().startswith('加载失败')
    page.evaluate("() => { window.cspRejected=[]; document.addEventListener('securitypolicyviolation',e=>window.cspRejected.push(e.violatedDirective)); const script=document.createElement('script');script.textContent='window.untrustedInlineExecuted=true';document.body.append(script); }")
    wait_page(page,"window.cspRejected.length>0")
    assert page.evaluate("() => window.untrustedInlineExecuted === undefined")
    assert page.evaluate("() => window.cspRejected.every(name=>name.startsWith('script-src'))")
    if not page.locator('[data-admin-module="media"]').evaluate("el=>el.classList.contains('expanded')"):
        page.locator('[data-admin-module="media"] .module-heading').click()
    results=[]
    for site, owner in (("primary",master),("direct",direct),("relay",relay)):
        page.evaluate("async () => { currentPath=''; await renderTree(); }")
        page.locator('#tree [data-path="music"]').dblclick()
        page.locator('#tree [data-path="music/crypto-'+site+'"]').dblclick()
        page.select_option('#uploadSiteType',site)
        page.locator('#uploadBtn').hover()
        page.locator('#uploadFiles').click()
        expect(page.locator('dialog.upload-storage-mode-dialog')).to_be_visible()
        assert page.locator('dialog').get_by_text('加密落盘',exact=True).count()==1
        assert page.locator('dialog').get_by_text('明文落盘',exact=True).count()==1
        filename="encrypted-"+site+".wav"
        with page.expect_file_chooser() as selected:
            page.locator('dialog').get_by_text('加密落盘',exact=True).click()
        selected.value.set_files({"name":filename,"mimeType":"audio/wav","buffer":source})
        wait_page(page,"() => !uploadRunning && document.querySelector('#uploadSummary').textContent === '完成：成功 1，失败 0' && document.querySelector('#uploadResults').textContent.includes("+json.dumps(filename)+")",timeout=90000)
        assert page.locator('#fileInput').get_attribute('data-storage-mode') is None
        payload=reservations[filename];metadata=payload['encryption']
        path="music/crypto-"+site+"/"+filename
        stored=(owner.data/"media"/path).read_bytes()
        assert len(stored)==metadata['ciphertext_size'] and stored!=source and not stored.startswith(b'RIFF')
        assert metadata['plaintext_size']==len(source)
        # Each new selection must show its own choice; cancel clears any stale
        # mode before a second file picker can emit a change event.
        page.locator('#uploadBtn').hover();page.locator('#uploadFolder').click()
        expect(page.locator('dialog.upload-storage-mode-dialog')).to_be_visible()
        assert page.locator('#folderInput').get_attribute('data-storage-mode') is None
        page.locator('dialog').get_by_text('取消',exact=True).click()
        dto=page.evaluate("""async ({path,fileId}) => {
            await FrontierMediaCrypto.ensureWorker();
            const url=FrontierMediaCrypto.virtualUrl(path,false,fileId);
            const full=await fetch(url,{cache:'no-store'});const body=await full.arrayBuffer();
            const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',body)),x=>x.toString(16).padStart(2,'0')).join('');
            const range=await fetch(url,{headers:{Range:'bytes=1048571-1048604'},cache:'no-store'});
            const part=Array.from(new Uint8Array(await range.arrayBuffer()));
            const head=await fetch(url,{method:'HEAD',cache:'no-store'});
            return {status:full.status,digest,rangeStatus:range.status,part,headStatus:head.status,
                length:Number(head.headers.get('Content-Length')),virtualUrl:url};
        }""",{"path":path,"fileId":metadata['file_id']})
        assert dto['status']==200 and dto['digest']==hashlib.sha256(source).hexdigest(),dto
        assert dto['rangeStatus']==206 and bytes(dto['part'])==source[1048571:1048605],dto
        assert dto['headStatus']==200 and dto['length']==len(source),dto
        plan=master.api('/api/v1/media/admin/download/plan?paths='+json.dumps([path]))
        assert plan['items'][0]['encryption']==metadata
        with page.expect_download() as downloaded:
            page.evaluate("async items => await FrontierMediaCrypto.downloadPlan(items)",plan['items'])
        assert Path(downloaded.value.path()).read_bytes()==source
        results.append({'site_type':site,'ciphertext_size':len(stored),'plaintext_size':len(source),
                        'plaintext_sha256':dto['digest'],'range_status':dto['rangeStatus'],'download':'browser-decrypted'})
    lyric_name='browser-encrypted.lrc'
    lyric=b'[00:01.00]browser-only secret lyric\n[00:05.00]second encrypted line\n'
    page.locator('#uploadBtn').hover();page.locator('#uploadLyrics').click()
    with page.expect_file_chooser() as selected:
        page.locator('dialog').get_by_text('加密落盘',exact=True).click()
    selected.value.set_files({'name':lyric_name,'mimeType':'text/plain','buffer':lyric})
    wait_page(page,"() => !uploadRunning && document.querySelector('#uploadSummary').textContent === '完成：成功 1，失败 0' && document.querySelector('#uploadResults').textContent.includes('browser-encrypted.lrc')",timeout=60000)
    assert (master.data/'media/lyrics'/lyric_name).read_bytes()!=lyric
    for site in ('primary','direct','relay'):
        master.api('/api/v1/media/admin/lyrics/relations',{'origin_kind':'track','origin_path':'music/crypto-'+site+'/encrypted-'+site+'.wav','linked_paths':['lyrics/'+lyric_name]})
    assert len(sessions)==1, {'admin_handshakes':len(sessions)}
    assert not failures,failures
    context.close()
    report['crypto']=results
    report['checks'].append('real per-selection AdminGUI encryption; Local/Direct/Relay opaque disk; compiled SW cross-chunk Range/HEAD/download; one session reused')
    report['checks'].append('current live AdminGUI upward/downward module alignment, real SQLite security listing and CSP unauthorized inline rejection')
    # Real public player must load encrypted catalog entries, use the worker and
    # seek correctly with the production fullscreen handlers.
    context=browser.new_context(viewport={'width':1366,'height':900},has_touch=True)
    page=context.new_page()
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    seek=[]
    for site in ('primary','direct','relay'):
        page.goto(master.endpoint+'/api/v1/media/music/category?path=music/crypto-'+site,wait_until='domcontentloaded')
        wait_page(page,"typeof art !== 'undefined' && art && art.video && currentMediaList.some(item=>item.encryption)",timeout=60000)
        page.evaluate("() => selectMedia(currentMediaList.findIndex(item=>item.encryption))")
        wait_page(page,"art.video.readyState>=2 && art.duration>30 && art.video.currentTime>.1",timeout=45000)
        wait_page(page,"activeLyricEntries.length===2",timeout=15000)
        assert page.evaluate("() => activeLyricEntries[0].text")=="browser-only secret lyric"
        assert page.evaluate("() => art.video.currentSrc.includes('/__fc_media/')")
        duration=page.evaluate('() => art.duration')
        for mode in ('inline','web','native','inline-return'):
            if mode=='web':page.evaluate('() => art.toggleWebFullscreen()')
            elif mode=='native':
                page.evaluate('() => {art.toggleWebFullscreen();art.container.requestFullscreen();}')
                wait_page(page,'document.fullscreenElement===art.container')
            elif mode=='inline-return':
                page.evaluate('() => document.exitFullscreen()');wait_page(page,'!document.fullscreenElement')
            page.wait_for_timeout(120)
            for ratio in (.25,.75):
                page.evaluate('() => {art.pause();art._showControls();}')
                box=page.locator('.art-control-progress-inner').bounding_box()
                page.mouse.click(box['x']+box['width']*ratio,box['y']+box['height']/2)
                page.wait_for_timeout(100)
                actual=page.evaluate('() => art.currentTime')
                assert abs(actual-duration*ratio)<=.15,{'site':site,'mode':mode,'expected':duration*ratio,'actual':actual}
                seek.append({'site_type':site,'mode':mode,'ratio':ratio,'error_seconds':abs(actual-duration*ratio)})
        # The actual end event moves to a second real source rather than leaving
        # the player stuck on the encrypted stream at its final byte.
        old=page.evaluate('() => currentIndex')
        page.evaluate('() => {art.video.currentTime=art.duration-.2;art.video.play();}')
        wait_page(page,'old => currentIndex!==old && art.video.readyState>=2',arg=old,timeout=15000)
    assert not errors,errors
    context.close();report['encrypted_seek']=seek
    report['checks'].append('real public encrypted playback, automatic next-track progression, inline/web/native fullscreen seek on all three placements')
    recording_snapshots(browser,nodes,report)
    # All mutations go through authenticated real APIs; identity/descriptors
    # survive remote directory rename and visibility restoration.
    for site,owner in (("primary",master),("direct",direct),("relay",relay)):
        old='music/crypto-'+site;new='music/crypto-'+site+'-renamed'
        metadata=reservations['encrypted-'+site+'.wav']['encryption']
        master.api('/api/v1/media/admin/directory/rename',{'path':old,'new_name':new.split('/')[-1]})
        path=new+'/encrypted-'+site+'.wav'
        plan=master.api('/api/v1/media/admin/download/plan?paths='+json.dumps([path]))
        assert plan['items'][0]['encryption']==metadata
        master.api('/api/v1/media/admin/hide',{'paths':[new],'hidden':True})
        public=httpx.Client(verify=ssl.create_default_context(cafile=str(master.ca)),trust_env=False)
        try:
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives import serialization
            pair=ec.generate_private_key(ec.SECP256R1())
            raw=pair.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
            session=public.post(master.endpoint+'/api/v1/media/crypto/session',json={'public_key':base64.b64encode(raw).decode()})
            assert session.status_code==200
            authorization={'file_path':path,'session_id':session.json()['session_id']}
            assert public.post(master.endpoint+'/api/v1/media/crypto/key',json=authorization).status_code==404
            master.api('/api/v1/media/admin/hide',{'paths':[new],'hidden':False})
            assert public.post(master.endpoint+'/api/v1/media/crypto/key',json=authorization).status_code==200
            response=public.get(master.endpoint+'/api/v1/media/stream',params={'file_path':path},follow_redirects=True)
            assert response.status_code==200 and response.content==(owner.data/'media'/path).read_bytes()
        finally:public.close()
        result=master.api('/api/v1/media/admin/delete',{'paths':[path]})
        assert result['deleted']==1 and not (owner.data/'media'/path).exists(),result
    report['checks'].append('Local/Direct/Relay rename preserves encryption identity; hide/restore rechecks public access; delete removes opaque bytes')


def priority(browser,master,source,report):
    from tests.federation_stack import admin_page,wait_page
    scope='music/priority-live'
    resources={}
    for name,value in (('preceding99.wav',99),('a.wav',100),('b.wav',100),('c.wav',101),('maximum.wav',500000),('minimum.wav',-7)):
        item=master.upload_media(source,name,site_type='primary',target_dir=scope);resources[name]=item
        master.api('/api/v1/media/admin/media-priority',{'media_path':item['path'],'resource_id':item['resource_id'],'value':value})
    for value in (-8,500001):
        master.api('/api/v1/media/admin/media-priority',{'media_path':resources['a.wav']['path'],'resource_id':resources['a.wav']['resource_id'],'value':value},expected=422)
    state=master.api('/api/v1/media/admin/priority-order?kind=media&scope='+scope)
    assert state['minimum']==-7 and state['maximum']==500000
    original={item['path']:item['preference'] for item in state['items']}
    context,page=admin_page(browser,master)
    page.locator('[data-admin-module="priority"] .module-heading').click()
    page.evaluate('async scope=>{priorityScope=scope;priorityPage=1;await loadMediaPriority(true);}',scope)
    wait_page(page,"() => !priorityLoading && document.querySelectorAll('#priorityList .priority-drag-handle').length===6")
    page.wait_for_timeout(700)
    def row(name):return page.locator('[data-priority-path="'+scope+'/'+name+'"]')
    row('b.wav').scroll_into_view_if_needed()
    handle=row('c.wav').locator('.priority-drag-handle').bounding_box()
    midpoint=row('b.wav').bounding_box()
    page.mouse.move(handle['x']+handle['width']/2,handle['y']+handle['height']/2)
    page.mouse.down();page.mouse.move(handle['x']+handle['width']/2,midpoint['y']+2,steps=12)
    page.wait_for_timeout(250);page.mouse.up()
    wait_page(page,"document.querySelector('#prioritySaveState').textContent.startsWith('已保存顺序')",timeout=30000)
    saved=master.api('/api/v1/media/admin/priority-order?kind=media&scope='+scope)
    names=[item['path'].rsplit('/',1)[-1] for item in saved['items']]
    assert names.index('a.wav')+1==names.index('c.wav') and names.index('c.wav')+1==names.index('b.wav'),names
    values={item['path']:item['preference'] for item in saved['items']}
    assert values[scope+'/preceding99.wav']==99 and values[scope+'/maximum.wav']==500000 and values[scope+'/minimum.wav']==-7
    # Exercise real server pagination with a small valid page_size. The fixture
    # only changes that request parameter; responses still come from the actual
    # authenticated Python route and SQLite catalog, with no response mocking.
    from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode
    def small_page(route):
        url=urlsplit(route.request.url)
        query=dict(parse_qsl(url.query));query['page_size']='2'
        route.continue_(url=urlunsplit((url.scheme,url.netloc,url.path,urlencode(query),url.fragment)))
    page.route('**/api/v1/media/admin/media-priority?*',small_page)
    page.evaluate('async () => {priorityPage=2;await loadMediaPriority(true);}')
    wait_page(page,"() => !priorityLoading && document.querySelectorAll('#priorityList .priority-drag-handle').length===2")
    assert '2 / 3' in page.locator('#priorityPageInfo').text_content()
    # A concurrent change on another page after the GUI loaded the whole sibling
    # revision must reject its stale drag and restore the displayed order.
    master.api('/api/v1/media/admin/media-priority',{'media_path':resources['preceding99.wav']['path'],'resource_id':resources['preceding99.wav']['resource_id'],'value':98})
    row('c.wav').locator('.priority-drag-handle').focus()
    page.keyboard.press('ArrowDown')
    wait_page(page,"document.querySelector('#prioritySaveState').classList.contains('failed')",timeout=30000)
    assert '恢复' in page.locator('#prioritySaveState').text_content()
    unchanged=master.api('/api/v1/media/admin/priority-order?kind=media&scope='+scope)
    assert [item['path'] for item in unchanged['items']]==[item['path'] for item in saved['items']]
    page.unroute('**/api/v1/media/admin/media-priority?*',small_page)
    context.close()
    # Hidden categories remain part of the complete revision even though the
    # public category listing excludes them.
    master.api('/api/v1/media/admin/hide',{'paths':[scope],'hidden':True})
    dirs=master.api('/api/v1/media/admin/priority-order?kind=directory&scope=music')
    assert any(item['path']==scope for item in dirs['items'])
    master.api('/api/v1/media/admin/hide',{'paths':[scope],'hidden':False})
    report['priority_live']={'before':original,'after':values,'conflict_status':409,'range':[-7,500000],'hidden_siblings':'included','pagination':{'page':2,'pages':3,'rendered':2,'revision_siblings':6,'off_page_change':'rejected stale drag'}}
    report['checks'].append('current real AdminGUI pointer drag resolves 100/100/101 collision without moving adjacent 99 or bounds; real pagination off-page mutation rejects stale drag and restores UI; hidden siblings included')


def certificates(browser,nodes,report):
    import re
    from datetime import datetime,timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509.verification import PolicyBuilder,Store
    from tests.federation_stack import admin_page,wait_for,wait_page
    master=nodes[0]
    for node in nodes:
        identifier=node.nodes()['node_id'] if node is master else node.relation
        wait_for(lambda: master.api('/api/v1/media/admin/nodes/'+identifier+'/certificate').get('serving_matches'),seconds=90,description='actual managed TLS serving observation')
    context,page=admin_page(browser,master);observations=[]
    roots=Store([x509.load_pem_x509_certificate(master.ca.read_bytes())]);results=[]
    for node in nodes:
        panel=page.locator('.node-resource-panel').filter(has_text=node.hostname).filter(has=page.locator('button').filter(has_text='下载证书及私钥'))
        button=panel.get_by_role('button',name='下载证书及私钥',exact=True)
        expect(button).to_be_enabled(timeout=20000)
        identifier=node.nodes()['node_id'] if node is master else node.relation
        request_url=master.endpoint+'/api/v1/media/admin/nodes/'+identifier+'/certificate'
        with page.expect_response(lambda response: response.request.method=='POST' and response.url==request_url) as replied:
            with page.expect_download() as result:button.click()
        value=replied.value.json()
        assert 'PRIVATE KEY' not in json.dumps(value) and 'private_key' not in value and 'account_key' not in value
        assert value['download']['algorithm']=='P256-HKDF-SHA256-AES256GCM'
        observations.append(value)
        data=Path(result.value.path()).read_bytes()
        leaves=x509.load_pem_x509_certificates(data)
        key_blob=re.search(rb'-----BEGIN (?:EC |RSA )?PRIVATE KEY-----.*?-----END (?:EC |RSA )?PRIVATE KEY-----',data,re.S)
        assert key_blob
        private=serialization.load_pem_private_key(key_blob.group(),password=None)
        assert private.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)==leaves[0].public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
        PolicyBuilder().store(roots).time(datetime.now(timezone.utc)).build_server_verifier(x509.DNSName(node.hostname)).verify(leaves[0],leaves[1:])
        fingerprint=hashlib.sha256(leaves[0].public_bytes(serialization.Encoding.DER)).hexdigest()
        assert value['hostname']==node.hostname and value['fingerprint']==fingerprint,{'expected_hostname':node.hostname,'reply_hostname':value['hostname'],'download_fingerprint':fingerprint,'reply_fingerprint':value['fingerprint']}
        assert node.hostname in leaves[0].extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        wait_page(page,"document.querySelector('#nodeOperationStatus').textContent.includes('已下载')")
        results.append({'hostname':node.hostname,'fingerprint':fingerprint,'browser_unwrap':'P256/HKDF/AES-GCM','matching_private_key':True,'verified_test_chain':True})
    assert len(observations)==3
    context.close();report['certificate_downloads']=results
    report['checks'].append('each node certificate download button unwraps only in browser; SAN/trusted chain/private pair/fingerprint verified; Master receives remote ciphertext envelopes')


def recording_snapshots(browser,nodes,report):
    from tests.federation_stack import wait_page
    master,direct,_relay=nodes
    user=master.register_karaoke_user(username='crypto_karaoke')
    context=browser.new_context()
    context.grant_permissions(['microphone'],origin=master.endpoint)
    context.add_cookies([{'name':item.name,'value':item.value,'domain':item.domain,'path':item.path,'secure':item.secure,'httpOnly':'session' in item.name}
                        for item in master.client.cookies.jar if 'karaoke' in item.name])
    page=context.new_page();errors=[];tickets=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    def observed(response):
        if response.request.method=='POST' and response.url.endswith('/recordings/ticket') and response.status==200:tickets.append(response.json())
    page.on('response',observed)
    page.goto(master.endpoint+'/api/v1/media/music/category?path=music/crypto-direct',wait_until='domcontentloaded')
    wait_page(page,"typeof currentMediaList!=='undefined' && currentMediaList.some(item=>item.encryption)",timeout=45000)
    media=page.evaluate('() => currentMediaList.find(item=>item.encryption).karaoke_id')
    captured=[]
    for mode in ('Relay','Direct'):
        master.mode(mode,relation=direct.relation)
        pending=[]
        def delayed_lyric(route):
            pending.append(route)
        page.route('**/api/v1/karaoke/lyrics?*',delayed_lyric)
        page.goto(master.endpoint+'/karaoke/?media='+media,wait_until='domcontentloaded')
        wait_page(page,"document.querySelector('#title').textContent!=='正在载入当前媒体…'",timeout=45000)
        expect(page.locator('#record')).to_be_disabled()
        page.locator('#record').dispatch_event('click')
        assert page.evaluate('() => state.phase === "idle" && state.recordingSnapshot===null')
        # Drive browser events while the actual encrypted lyric response is held.
        for _ in range(100):
            if pending:break
            page.wait_for_timeout(50)
        assert pending,'Karaoke did not request encrypted lyric initialization'
        for route in pending:route.continue_()
        page.unroute('**/api/v1/karaoke/lyrics?*',delayed_lyric)
        wait_page(page,"state.sourceLyricsEncrypted && state.sourceLyrics.length===2 && state.account && document.querySelector('#status').textContent.includes('浏览器能力检查通过') && !document.querySelector('#record').disabled",timeout=45000)
        page.locator('#record').click()
        expect(page.locator('#status')).to_contain_text('正在录制纯人声支路',timeout=20000)
        page.wait_for_timeout(1200);page.locator('#stop').click()
        expect(page.locator('#previewCard')).to_be_visible(timeout=15000)
        expect(page.locator('#upload')).to_be_enabled()
        page.locator('#upload').click()
        expect(page.locator('#status')).to_contain_text('录音已上传到个人空间',timeout=60000)
        ticket=tickets[-1]
        assert ticket['direct']==(mode=='Direct'),{'mode':mode,'direct':ticket['direct']}
        listing=master.client.get(master.endpoint+'/api/v1/karaoke/account/recordings').json()['items']
        item=next(item for item in listing if item['recording_id']==ticket['recording_id'])
        assert item['lyrics']==[] and item['encrypted_lyrics']['encryption']['plaintext_size']>0
        files=list((direct.data/'recordings').rglob(ticket['recording_id']+'.bin'));assert len(files)==1
        opaque=files[0].read_bytes()
        assert b'browser-only secret lyric' not in opaque and b'encrypted_lyrics' in opaque
        result=page.evaluate("""async id => {
            const value=await FrontierMediaCrypto.decryptRecordingLyrics(fields=>accountApi('/recordings/'+id+'/lyrics-key',{
                method:'POST',headers:karaokeHeaders(),body:JSON.stringify(fields)}));
            return {lines:value.length,text:value[0].text};
        }""",ticket['recording_id'])
        assert result=={'lines':2,'text':'browser-only secret lyric'},result
        captured.append({'transport':mode,'recording_id':ticket['recording_id'],'bytes':len(opaque),'encrypted_snapshot':True,'browser_decrypted_lines':2})
    assert not errors,errors
    # New preparation must reject a now-hidden source, while the owner may still
    # read the recording's immutable historical browser-encrypted lyric snapshot.
    master.api('/api/v1/media/admin/hide',{'paths':['music/crypto-direct'],'hidden':True})
    result=page.evaluate("""async id => await FrontierMediaCrypto.decryptRecordingLyrics(fields=>accountApi('/recordings/'+id+'/lyrics-key',{
        method:'POST',headers:karaokeHeaders(),body:JSON.stringify(fields)}))""",tickets[-1]['recording_id'])
    assert result[0]['text']=='browser-only secret lyric'
    master.api('/api/v1/media/admin/hide',{'paths':['music/crypto-direct'],'hidden':False})
    context.close()
    for ticket in tickets:master.karaoke_api('/recordings/'+ticket['recording_id'],method='DELETE')
    assert not any((direct.data/'recordings').rglob('*.bin'))
    master.api('/api/v1/media/admin/users/'+user['user_id'],{'action':'delete','quota_mib':None})
    report['recording_snapshots']=captured
    report['checks'].append('Karaoke delayed encrypted initialization blocks early recording; real microphone encrypts lyric footer locally; Direct/Relay reservation proof/finalize/owner browser decryption and hidden-source historical snapshot survive; deletion frees bytes')
