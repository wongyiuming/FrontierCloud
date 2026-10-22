"""Actual HTTPS assets: logo reuse, long header, wrapping and failed artwork."""
import os
from urllib.parse import quote

from tests import browser_ui_regression as ui
from tests import federation_stack as fleet
from tests.player_logo_restart_browser import exercise as logo_restart


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
        inline=page.evaluate("""() => {
            activeLyricEntries=Array.from({length:7},(_,index)=>({time:index,text:index===3?'天地有情'.repeat(20):'短句'}));
            document.getElementById('inlineLyrics').classList.remove('hidden');renderLyricWindow(3);
            const line=document.querySelector('#inlineLyricsTrack .sync-lyric.current');
            line.style.whiteSpace='nowrap';const before=getComputedStyle(line).fontSize;line.style.removeProperty('white-space');
            const style=getComputedStyle(line),range=document.createRange();range.selectNodeContents(line);
            const box=line.getBoundingClientRect(),panel=document.getElementById('inlineLyrics').getBoundingClientRect();
            const rows=Array.from(range.getClientRects());
            return {before,after:style.fontSize,rows:rows.length,height:box.height,lineHeight:parseFloat(style.lineHeight),
                glyphsInside:rows.every(row=>row.top>=box.top-1&&row.bottom<=box.bottom+1&&row.top>=panel.top-1&&row.bottom<=panel.bottom+1),
                visibleRows:document.getElementById('inlineLyricsTrack').children.length};
        }""")
        report['inline_presentation_geometry']=inline
        check('inline_lyrics_wrap_without_clipping_or_font_reduction',inline['before']==inline['after'] and inline['rows']>1
            and inline['height']>inline['lineHeight'] and inline['glyphsInside'] and inline['visibleRows']==7)
        page.locator('.back-btn').click()
        fleet.wait_page(page,"new URL(location.href).searchParams.get('path')==='music/黄耀明'",timeout=15000)
        check('long_album_return_uses_the_business_parent',True)
        check('presentation_browser_has_no_script_errors',not errors)
        report['presentation']=dict(header=bounds,lyrics=geometry,inline_lyrics=inline,blocked_artwork_inputs=blocked,errors=errors,
            artwork_fault_injected_at_fetch=True,lyric_relation_tested=False,
            logo_cross_page_http_cache=True,browser_restart_cache_tested=True,
            browser_restart_cache_scope='public-music-logo-only',encrypted_media_disk_cache_tested=False)
    finally:context.close()
