"""Actual HTTPS assets: logo reuse, long header, wrapping and failed artwork."""
import os
from urllib.parse import quote

from tests import browser_ui_regression as ui
from tests import federation_stack as fleet
from tests.player_logo_restart_browser import exercise as logo_restart


def inline_scroll_state(page):
    return page.evaluate("""() => {
        const viewport=document.getElementById('inlineLyricsLines'),box=viewport.getBoundingClientRect();
        const row=viewport.querySelector('.sync-lyric.current'),text=row.firstChild;
        const visible=offset=>{const range=document.createRange();range.setStart(text,offset);range.setEnd(text,offset+1);
            const glyph=range.getBoundingClientRect();return glyph.top>=box.top-1&&glyph.bottom<=box.bottom+1;};
        return {top:viewport.scrollTop,maximum:viewport.scrollHeight-viewport.clientHeight,
            first:visible(0),last:visible(text.length-1),time:art.currentTime,index:currentIndex,
            sequence:playerSwitchSequence,cue:activeLyricIndex,cancels:window.lyricPointerCancels||0,
            overflow:getComputedStyle(viewport).overflowY,pointerEvents:getComputedStyle(viewport).pointerEvents,
            touchAction:getComputedStyle(document.getElementById('playerSection')).touchAction};
    }""")


def exercise_inline_scrolling(page,context,report,check):
    viewport=page.locator('#inlineLyricsLines');box=viewport.bounding_box()
    x,y=box['x']+box['width']/2,box['y']+box['height']/2
    before=inline_scroll_state(page)
    check('tall_inline_lyrics_are_focusable_native_scroll_content',before['maximum']>0
        and before['overflow']=='auto' and before['pointerEvents']=='auto' and before['touchAction']=='pan-y'
        and viewport.get_attribute('tabindex')=='0')
    page.mouse.move(x,y);report['inputs'].append(dict(action='native lyric wheel',x=x,y=y,delta_y=-10000));page.mouse.wheel(0,-10000)
    fleet.wait_page(page,'document.getElementById("inlineLyricsLines").scrollTop<=1')
    first=inline_scroll_state(page)
    report['inputs'].append(dict(action='native lyric wheel',x=x,y=y,delta_y=10000));page.mouse.wheel(0,10000)
    fleet.wait_page(page,'(()=>{const v=document.getElementById("inlineLyricsLines");return v.scrollTop>=v.scrollHeight-v.clientHeight-1;})()')
    last=inline_scroll_state(page)
    check('native_wheel_reaches_first_and_last_glyphs_of_tall_inline_lyrics',first['first'] and last['last'] and last['top']>first['top'])
    page.evaluate("""() => {window.lyricPointerCancels=0;
        document.getElementById('playerSection').addEventListener('pointercancel',()=>window.lyricPointerCancels++);} """)
    session=context.new_cdp_session(page);touch_inputs=[];touch_ends={}
    report['inputs'].append(dict(action='CDP native lyric touch',events=touch_inputs))
    try:
        for direction,label in ((-1,'first'),(1,'last')):
            for _ in range(8):
                start=box['y']+box['height']*(.8 if direction==1 else .2)
                for index in range(7):
                    point={'x':x,'y':start-direction*box['height']*.6*index/6}
                    value={'type':'touchStart' if index==0 else 'touchMove','touchPoints':[point]}
                    session.send('Input.dispatchTouchEvent',value);touch_inputs.append(value)
                    page.wait_for_timeout(35)
                end={'type':'touchEnd','touchPoints':[]};session.send('Input.dispatchTouchEvent',end);touch_inputs.append(end)
                page.wait_for_timeout(100)
                touch_ends[label]=inline_scroll_state(page)
                if touch_ends[label][label]:break
            check('native_touch_reaches_'+label+'_glyph_of_tall_inline_lyrics',touch_ends[label][label])
    finally:session.detach()
    fleet.wait_page(page,'document.getElementById("inlineLyricsLines").scrollTop>5')
    page.wait_for_function("""() => {const top=document.getElementById('inlineLyricsLines').scrollTop;
        const old=window.lyricScrollSettle;window.lyricScrollSettle={top,count:old&&Math.abs(old.top-top)<.5?old.count+1:0};
        return window.lyricScrollSettle.count>=4;}""",timeout=5000)
    touched=inline_scroll_state(page)
    check('native_vertical_touch_scroll_does_not_seek_or_change_track',touched['cancels']>0 and touched['top']>5
        and touched['index']==before['index'] and touched['sequence']==before['sequence'] and abs(touched['time']-before['time'])<.05)
    # Real media events and the normal RAF clock remain installed. Seeking to
    # zero is an explicit test input, after the no-seek gesture assertion.
    report['inputs'].append(dict(action='same-cue real media clock',setup_seek_seconds=0,play_until_seconds=.1,then_pause=True))
    page.evaluate('()=>{art.currentTime=0;}')
    fleet.wait_page(page,'!art.video.seeking&&art.currentTime<.05&&activeLyricIndex===3')
    clock_before=inline_scroll_state(page)
    page.evaluate('()=>art.play()')
    fleet.wait_page(page,'art.currentTime>.1&&!art.video.paused',timeout=10000)
    page.evaluate('()=>art.pause()')
    fleet.wait_page(page,'art.video.paused&&!art.playing')
    clock_after=inline_scroll_state(page)
    check('same_cue_real_playback_clock_preserves_manual_lyric_scroll',clock_after['cue']==clock_before['cue']==3
        and clock_after['sequence']==clock_before['sequence'] and abs(clock_after['top']-clock_before['top'])<=1)
    # Retain the original surface semantics on the same served audio player.
    report['inputs'].append(dict(action='native horizontal mouse drag',start=[x+25,y],end=[x-25,y],steps=6))
    page.mouse.move(x+25,y);page.mouse.down();page.mouse.move(x-25,y,steps=6);page.mouse.up()
    fleet.wait_page(page,'!art.video.seeking&&art.currentTime<.05')
    dragged=inline_scroll_state(page)
    check('inline_lyric_horizontal_drag_keeps_original_audio_seek',dragged['sequence']==clock_after['sequence'] and dragged['time']<.05)
    # The existing drag suppresses follow-up clicks for 500ms.
    page.wait_for_timeout(550)
    sequence=dragged['sequence'];report['inputs'].append(dict(action='native touchscreen single tap',x=x,y=y));page.touchscreen.tap(x,y)
    fleet.wait_page(page,f'playerSwitchSequence==={sequence+1}&&art.video.readyState>=2')
    page.evaluate('()=>art.pause()');sequence=page.evaluate('playerSwitchSequence')
    report['inputs'].append(dict(action='native touchscreen double tap',x=x,y=y,interval_ms=80))
    page.touchscreen.tap(x,y);page.wait_for_timeout(80);page.touchscreen.tap(x,y)
    fleet.wait_page(page,f'playerSwitchSequence==={sequence+1}&&art.video.readyState>=2')
    page.evaluate('()=>art.pause()')
    check('inline_lyric_surface_retains_single_next_and_double_previous',page.evaluate('clickTimer===null'))
    report['inline_scroll']=dict(before=before,wheel_first=first,wheel_last=last,touch=touched,touch_ends=touch_ends,
        clock_before=clock_before,clock_after=clock_after,horizontal_drag=dragged,
        actual_media_clock=True,clock_replaced=False)


def exercise(browser,master,root,report,check):
    previous=ui.BASE_URL
    try:
        ui.BASE_URL=master.endpoint
        ui.logo_cross_page_cache_check(browser)
        check('actual_logo_reuses_immutable_content_across_pages',True)
    finally:ui.BASE_URL=previous
    logo_restart(browser,master,root,report,check)
    name='MULTIVERSE_OF_GO_EAST_30TH_ANNIV'
    artist=root/'presentation'/'黄耀明';album=artist/name;album.mkdir(parents=True)
    os.link(root/'album/02-next.mp3',album/'short.mp3')
    context,page=fleet.admin_page(browser,master)
    try:
        heading=page.locator('[data-admin-module="media"] .module-heading')
        if heading.get_attribute('aria-expanded')!='true':heading.click()
        page.locator('.tree-row[data-path="music"]').dblclick();fleet.wait_page(page,"currentPath==='music'")
        page.select_option('#uploadSiteType','primary');page.locator('#uploadBtn').click();page.locator('#uploadFolder').click()
        with page.expect_file_chooser() as chosen:page.get_by_role('button',name='明文落盘',exact=True).click()
        chosen.value.set_files(str(artist))
        fleet.wait_page(page,"document.querySelector('#uploadSummary').textContent.startsWith('完成：')",timeout=60000)
        check('long_album_fixture_uses_normal_gui_upload','成功 1，失败 0' in page.locator('#uploadSummary').inner_text())
        report['inputs'].append(dict(action='GUI upload long album',path='music/黄耀明/'+name+'/short.mp3',site_type='primary'))
    finally:context.close()
    context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
    page=context.new_page();errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.add_init_script("""
        window.artworkFaultInputs=[];
        const originalFetch=window.fetch.bind(window);
        window.fetch=(input,options)=>{
            const url=new URL(typeof input==='string'?input:input.url,location.href);
            if(url.pathname==='/api/v1/media/cover'){
                artworkFaultInputs.push({method:options?.method||'GET',path:url.pathname,
                    cache:options?.cache,fault:'injected-fetch-rejection'});
                return Promise.reject(new TypeError('Injected optional artwork cache failure'));
            }
            return originalFetch(input,options);
        };
    """)
    try:
        page.goto(master.endpoint+'/api/v1/media/music/category?path='+quote('music/黄耀明/'+name),wait_until='domcontentloaded')
        fleet.wait_page(page,"typeof art!=='undefined'&&art&&currentMediaList.length===1")
        bounds=page.locator('.back-btn').evaluate('element=>{const r=element.getBoundingClientRect();return {left:r.left,right:r.right,width:r.width,height:r.height,viewport:innerWidth,href:element.href};}')
        check('long_album_keeps_business_return_button_visible',bounds['width']>30 and bounds['height']>15 and bounds['left']>=0 and bounds['right']<=bounds['viewport']+1)
        # The mobile queue is collapsed. Make a real queue row visible before
        # injecting a failure into its optional cache-only artwork read.
        page.set_viewport_size({'width':1280,'height':800})
        page.evaluate('()=>selectMedia(0)')
        fleet.wait_page(page,'art.currentTime>.2&&!art.video.paused&&!art.video.error',timeout=30000)
        fleet.wait_page(page,'artworkFaultInputs.length>0',timeout=15000)
        blocked=page.evaluate('artworkFaultInputs')
        check('failed_artwork_does_not_hold_actual_media_playback',
            bool(blocked) and all(row['cache']=='only-if-cached' for row in blocked)
            and page.evaluate('art.currentTime>.2&&!art.video.paused&&!art.video.error'))
        page.evaluate('()=>art.pause()')
        fleet.wait_page(page,'art.video.paused&&!art.playing&&!art.video.seeking&&inlineLyricsRequest===null')
        page.set_viewport_size({'width':390,'height':844})
        geometry=page.evaluate("""() => {
            const overlay=document.getElementById('fullscreenLyrics');overlay.classList.remove('hidden');overlay.setAttribute('aria-hidden','false');
            activeLyricEntries=[{time:0,text:'天地有情'.repeat(20)}];renderFullscreenLyrics();sizeFullscreenLyrics();
            const line=overlay.querySelector('.fullscreen-lyric-line');line.style.whiteSpace='nowrap';
            const before=getComputedStyle(line).fontSize;line.style.removeProperty('white-space');
            const style=getComputedStyle(line),range=document.createRange();range.selectNodeContents(line);
            const box=line.getBoundingClientRect(),rows=Array.from(range.getClientRects());
            return {before,after:style.fontSize,whiteSpace:style.whiteSpace,rows:rows.length,lineHeight:parseFloat(style.lineHeight),height:box.height,
                glyphsInside:rows.every(row=>row.top>=box.top-1&&row.bottom<=box.bottom+1),presentation_fixture:true,white_space_comparison:true};
        }""")
        report['presentation_geometry']=geometry
        print({'presentation_geometry':geometry},flush=True)
        check('long_lyrics_wrap_without_reducing_the_font',geometry['before']==geometry['after'] and geometry['rows']>1
            and geometry['whiteSpace']!='nowrap' and geometry['height']>geometry['lineHeight'] and geometry['glyphsInside'])
        page.evaluate('()=>closeFullscreenLyrics()')
        normal=page.evaluate("""() => {
            const clock=art.currentTime;
            activeLyricEntries=Array.from({length:7},(_,index)=>({time:index<=3?0:clock+(index-3)*60,text:index===3?'天地有情'.repeat(4):'短句'}));
            document.getElementById('inlineLyrics').classList.remove('hidden');renderLyricWindow(3);
            const line=document.querySelector('#inlineLyricsTrack .sync-lyric.current');
            const panel=document.getElementById('inlineLyricsLines').getBoundingClientRect();
            const range=document.createRange();range.selectNodeContents(line);
            return {height:line.getBoundingClientRect().height,panelHeight:panel.height,
                allVisible:[...range.getClientRects()].every(row=>row.top>=panel.top-1&&row.bottom<=panel.bottom+1)};
        }""")
        check('ordinary_wrapped_inline_lyric_is_fully_visible',normal['height']<=normal['panelHeight'] and normal['allVisible'])
        inline=page.evaluate("""() => {
            const clock=art.currentTime;
            activeLyricEntries=Array.from({length:7},(_,index)=>({time:index<=3?0:clock+(index-3)*60,text:index===3?'天地有情'.repeat(20):'短句'}));
            document.getElementById('inlineLyrics').classList.remove('hidden');renderLyricWindow(3);
            const line=document.querySelector('#inlineLyricsTrack .sync-lyric.current');
            line.style.whiteSpace='nowrap';const before=getComputedStyle(line).fontSize;line.style.removeProperty('white-space');
            const style=getComputedStyle(line),range=document.createRange();range.selectNodeContents(line);
            const box=line.getBoundingClientRect(),panel=document.getElementById('inlineLyrics').getBoundingClientRect();
            const rows=Array.from(range.getClientRects());
            return {before,after:style.fontSize,rows:rows.length,height:box.height,lineHeight:parseFloat(style.lineHeight),
                glyphsInside:rows.every(row=>row.top>=box.top-1&&row.bottom<=box.bottom+1),
                glyphsInsidePanel:rows.every(row=>row.top>=panel.top-1&&row.bottom<=panel.bottom+1),panelHeight:panel.height,
                visibleRows:document.getElementById('inlineLyricsTrack').children.length};
        }""")
        report['inline_presentation_geometry']=inline
        check('inline_lyrics_wrap_without_clipping_or_font_reduction',inline['before']==inline['after'] and inline['rows']>1
            and inline['height']>inline['lineHeight'] and inline['glyphsInside'] and inline['visibleRows']==7)
        check('tall_inline_fixture_really_exceeds_the_viewport',inline['height']>inline['panelHeight'])
        exercise_inline_scrolling(page,context,report,check)
        page.locator('.back-btn').click()
        fleet.wait_page(page,"new URL(location.href).searchParams.get('path')==='music/黄耀明'",timeout=15000)
        check('long_album_return_uses_the_business_parent',True)
        check('presentation_browser_has_no_script_errors',not errors)
        report['presentation']=dict(header=bounds,lyrics=geometry,ordinary_inline_lyrics=normal,inline_lyrics=inline,blocked_artwork_inputs=blocked,errors=errors,
            artwork_fault_injected_at_fetch=True,lyric_relation_tested=False,
            logo_cross_page_http_cache=True,browser_restart_cache_tested=True,
            browser_restart_cache_scope='public-music-logo-only',encrypted_media_disk_cache_tested=False)
    finally:context.close()
