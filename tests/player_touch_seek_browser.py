"""Native Chromium touch regression on shipped audio-player markup and assets.

Catalog/API startup is isolated; media decoding, player controls, lyrics, layout,
pointer negotiation and CDP touch input are real. No server or TLS bypass is used.
Run identical --assets-root snapshots before/after; --expect-broken accepts only
observed horizontal-touch failures, never fixture or unrelated-control failures.
"""
import argparse
import array
import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import wave

from playwright.sync_api import sync_playwright


ASSETS = ['static/media/audio-player.html', 'static/css/player.css',
          'static/css/lyrics-window.css', 'static/js/player.js',
          'static/js/audio-continuous-stream.js', 'static/js/lyrics-window.js']
VIEWPORTS = [(390, 844), (768, 1024), (1360, 768)]


def shipped_page(assets):
    template = assets[ASSETS[0]]
    inline = re.findall(r'<script\s*>(.*?)</script>', template, re.S)
    assert len(inline) == 2 and 'FrontierPlayerLayout' in inline[-1]
    markup = re.sub(r'<script\b[^>]*>.*?</script>', '', template, flags=re.S)
    for url, name in [('{{PLAYER_CSS_URL}}', ASSETS[1]),
                      ('/static/css/lyrics-window.css', ASSETS[2])]:
        markup, count = re.subn(r'<link\b[^>]*href="' + re.escape(url) + r'"[^>]*>',
                               lambda _: '<style>' + assets[name] + '</style>', markup)
        assert count == 1, name
    return markup, inline[-1]


def generated_audio():
    # A small real PCM fixture avoids ffmpeg, network access and new dependencies.
    samples = array.array('h', (int(5000 * math.sin(2 * math.pi * 440 * i / 8000))
                               for i in range(8000)))
    if sys.byteorder != 'little':
        samples.byteswap()
    output = io.BytesIO()
    with wave.open(output, 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(samples.tobytes() * 90)
    return output.getvalue()


STATE = """() => ({time:art.currentTime,duration:art.duration,paused:art.video.paused,
    seeking:art.video.seeking,index:currentIndex,sequence:playerSwitchSequence,
    sameVideo:art.video===window.fixtureVideo,sameSource:art.video.currentSrc===window.fixtureSource,
    pendingClick:clickTimer!==null,scroll:document.getElementById('inlineLyricsLines').scrollTop})"""

PROBE = """() => {
    window.touchInputEvents=[];
    for(const type of ['pointerdown','pointermove','pointerup','pointercancel','click']) {
        document.addEventListener(type,event=>{
            const target=event.target;
            window.touchInputEvents.push({type,pointerType:event.pointerType||'',
                x:event.clientX,y:event.clientY,target:target.id||target.className||target.tagName,
                cancelable:event.cancelable,defaultPrevented:event.defaultPrevented});
        },true);
    }
}"""


def touch(page, client, inputs, name, start, end=None):
    end = end or start
    points = [{'x': start[0] + (end[0] - start[0]) * i / 12,
               'y': start[1] + (end[1] - start[1]) * i / 12} for i in range(13)]
    inputs.append({'name': name, 'method': 'CDP Input.dispatchTouchEvent',
                   'touchStart': points[0], 'touchMove': points[1:] if end != start else [],
                   'touchEnd': [], 'move_wait_ms': 24, 'release_pause_ms': 100})
    client.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [points[0]]})
    if end != start:
        for point in points[1:]:
            client.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [point]})
            page.wait_for_timeout(24)
    else:
        page.wait_for_timeout(40)
    page.wait_for_timeout(100)
    client.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})


def run(root, output, media=None, expect_broken=False):
    root = root.resolve()
    output.mkdir(parents=True, exist_ok=False)
    assets = {name: (root/name).read_text(encoding='utf-8') for name in ASSETS}
    markup, layout = shipped_page(assets)
    payload = media.read_bytes() if media else generated_audio()
    if not media:
        (output/'fixture.wav').write_bytes(payload)
    mime = 'audio/wav' if not media or media.suffix.lower() == '.wav' else 'video/mp4'
    url = 'data:' + mime + ';base64,' + base64.b64encode(payload).decode()
    report = {'format': 'frontiercloud-native-player-touch-regression', 'passed': False,
              'expected_broken': expect_broken, 'assets_root': str(root),
              'assets': {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in ASSETS},
              'media': {'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(), 'mime': mime},
              'scope': 'shipped markup/CSS/lyrics and FrontierMediaPlayer with actual decoded fixture; catalog/network startup isolated',
              'tls_errors_ignored': False, 'cases': [], 'inputs': [], 'fixture_errors': []}
    with sync_playwright() as runtime:
        options = {'headless': True, 'args': ['--no-sandbox']}
        if os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'):
            options['executable_path'] = os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE']
        browser = runtime.chromium.launch(**options)
        try:
            for width, height in VIEWPORTS:
                page = browser.new_page(viewport={'width': width, 'height': height}, has_touch=True)
                errors = []
                page.on('pageerror', lambda error, target=errors: target.append(str(error)))
                page.route('**/*', lambda route: route.abort())
                client = page.context.new_cdp_session(page)
                try:
                    page.set_content(markup)
                    page.add_script_tag(content='window.PLAYER_KIND="audio";let currentMediaList=[];')
                    for name in ASSETS[3:]:
                        page.add_script_tag(content=assets[name])
                    page.add_script_tag(content=layout)
                    page.evaluate('''url=>{
                        art=new FrontierMediaPlayer({container:'#artplayer',url});
                        currentMediaList=[{title:'one',url,type:'audio'},{title:'two',url,type:'audio'}];
                        initGestureControl();
                    }''', url)
                    page.wait_for_function('art.duration>60 && art.video.readyState>=2')
                    page.evaluate('()=>{window.fixtureVideo=art.video;window.fixtureSource=art.video.currentSrc;}')
                    page.evaluate(PROBE)

                    def prepare(lyrics='short'):
                        page.evaluate('''kind=>{
                            if(clickTimer)clearTimeout(clickTimer);clickTimer=null;
                            art.pause();art.currentTime=art.duration*.4;
                            currentIndex=0;playerSwitchSequence=0;
                            const entries=Array.from({length:7},(_,i)=>({time:i<=3?0:1000+i,
                                text:kind==='long'?('自动换行完整保留原字体和全部歌词 '.repeat(65)):'第'+i+'句歌词'}));
                            showSynchronizedLyrics(kind==='none'?[]:entries,art.currentTime);
                            document.getElementById('inlineLyricsLines').scrollTop=0;
                            art._showControls();window.touchInputEvents=[];
                        }''', lyrics)
                        page.wait_for_function('!art.video.seeking && art.video.paused')
                        page.wait_for_timeout(100)

                    def case(name, action, validate):
                        before = page.evaluate(STATE)
                        try:
                            detail = action() or {}
                            page.wait_for_timeout(420)
                            after = page.evaluate(STATE)
                            validate(before, after, detail)
                            result = {'passed': True}
                        except Exception as error:
                            after = page.evaluate(STATE)
                            result = {'passed': False, 'failure': str(error), 'failure_type': type(error).__name__}
                        report['cases'].append({'name': name, 'viewport': [width, height],
                            'before': before, 'after': after, 'events': page.evaluate('window.touchInputEvents'), **result})

                    def stable(before, after):
                        assert (after['index'],after['sequence']) == (before['index'],before['sequence'])
                        assert after['sameSource'] and after['sameVideo'] and not after['pendingClick']

                    for lyrics in ['short', 'long', 'none']:
                        for direction, drift in [(1, 0), (-1, 0), (1, 5), (-1, -5)]:
                            prepare(lyrics)
                            selector = '#inlineLyricsLines' if lyrics != 'none' else '.art-video-player'
                            box = page.locator(selector).bounding_box()
                            x = box['x'] + box['width'] * (.30 if direction > 0 else .70)
                            y = box['y'] + box['height'] * .28
                            dx = direction * min(90, box['width'] * .26)
                            hit = page.evaluate('''p=>{const e=document.elementFromPoint(p.x,p.y);
                                return {inside:e.closest(p.selector)!==null,control:isGestureControl(e),touchAction:getComputedStyle(e).touchAction};}''',
                                {'x': x, 'y': y, 'selector': selector})
                            assert hit['inside'] and not hit['control'], hit
                            name = f'horizontal_touch_{lyrics}_{direction}_{drift}'
                            def action(name=name,x=x,y=y,dx=dx,drift=drift):
                                touch(page,client,report['inputs'],name,(x,y),(x+dx,y+drift))
                                return {'expected_delta':dx*.2}
                            def validate(before,after,detail):
                                stable(before,after)
                                assert after['paused'] and abs(after['time']-before['time']-detail['expected_delta'])<.5, (before,after,detail)
                            case(name,action,validate)

                    prepare('long')
                    box = page.locator('#inlineLyricsLines').bounding_box()
                    x = box['x'] + box['width']*.24
                    y = box['y'] + box['height']*.78
                    distance = min(180,box['height']*.5)
                    font = page.locator('#inlineLyricsTrack .current').evaluate('e=>getComputedStyle(e).fontSize')
                    def vertical_up():
                        touch(page,client,report['inputs'],'native_vertical_up',(x,y),(x,y-distance))
                    def vertical_validate(before,after,detail):
                        stable(before,after)
                        assert after['paused'] and abs(after['time']-before['time'])<.05
                        assert after['scroll']>before['scroll']+5,(before,after)
                        assert page.locator('#inlineLyricsTrack .current').evaluate('e=>getComputedStyle(e).fontSize')==font
                    case('native_vertical_scroll_up',vertical_up,vertical_validate)
                    page.evaluate('window.touchInputEvents=[]')
                    def vertical_down():
                        touch(page,client,report['inputs'],'native_vertical_down',(x,y-distance),(x,y))
                    def down_validate(before,after,detail):
                        stable(before,after)
                        assert after['paused'] and abs(after['time']-before['time'])<.05 and after['scroll']<before['scroll']-5,(before,after)
                    case('native_vertical_scroll_down',vertical_down,down_validate)

                    prepare('short')
                    box = page.locator('#inlineLyricsLines').bounding_box()
                    x,y=box['x']+box['width']*.3,box['y']+box['height']*.28
                    dx=min(90,box['width']*.26)
                    def mouse_drag():
                        report['inputs'].append({'name':'mouse_lyrics_seek','method':'Playwright mouse','start':[x,y],'end':[x+dx,y],'steps':12})
                        page.mouse.move(x,y);page.mouse.down();page.mouse.move(x+dx,y,steps=12);page.mouse.up()
                    case('mouse_lyrics_seek',mouse_drag,lambda before,after,detail:(stable(before,after),
                         _require(after['paused'] and abs(after['time']-before['time']-dx*.2)<.5,'mouse seek failed')))

                    prepare('short')
                    box=page.locator('.art-center-play').bounding_box();point=(box['x']+box['width']/2,box['y']+box['height']/2)
                    assert page.evaluate('p=>document.elementFromPoint(...p)?.closest(".art-center-play")!==null',list(point))
                    case('center_play_touch_ownership',lambda:touch(page,client,report['inputs'],'center_play',point),
                         lambda before,after,detail:(stable(before,after),_require(not after['paused'] and after['time']>before['time'],'center play did not resume')))

                    prepare('short')
                    box=page.locator('.art-control-progress-inner').bounding_box();point=(box['x']+box['width']*.7,box['y']+box['height']/2)
                    case('progress_touch_ownership',lambda:touch(page,client,report['inputs'],'progress_control',point),
                         lambda before,after,detail:(stable(before,after),_require(after['paused'] and abs(after['time']-after['duration']*.7)<.5,'progress control failed')))
                    page.screenshot(path=str(output/f'player-touch-{width}.png'))
                    assert not errors, errors
                except Exception as error:
                    report['fixture_errors'].append({'viewport':[width,height],'type':type(error).__name__,'error':str(error)})
                finally:
                    client.detach();page.close()
        finally:
            browser.close()
    failed=[row for row in report['cases'] if not row['passed']]
    report['passed']=not failed and not report['fixture_errors']
    report['expected_failure_observed']=bool(failed) and all(row['name'].startswith('horizontal_touch_') for row in failed) and not report['fixture_errors']
    (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'expected_broken':expect_broken,
                      'expected_failure_observed':report['expected_failure_observed'],
                      'cases':len(report['cases']),'failed_cases':len(failed),'fixture_errors':len(report['fixture_errors'])}))
    return report['expected_failure_observed'] if expect_broken else report['passed']


def _require(condition,message):
    assert condition,message


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets-root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output',type=Path,required=True,help='New private directory; never overwrites existing evidence')
    parser.add_argument('--media',type=Path,help='Optional real media (>60 sec); defaults to a generated 90-sec PCM tone')
    parser.add_argument('--expect-broken',action='store_true')
    args=parser.parse_args()
    raise SystemExit(0 if run(args.assets_root,args.output,args.media,args.expect_broken) else 1)
