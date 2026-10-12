"""Real media seeks across layout, web fullscreen and native fullscreen.

Run on the development host with Playwright and a generated, test-only MP4.
No application media or public CA/network access is needed.
"""
import argparse
import base64
import json
import pathlib

from playwright.sync_api import sync_playwright


def check(root, media, output, expect_broken=False):
    source = (root / 'static/js/player.js').read_text(encoding='utf-8')
    css = (root / 'static/css/player.css').read_text(encoding='utf-8')
    media_url = 'data:video/mp4;base64,' + base64.b64encode(media.read_bytes()).decode()
    results = []
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        for size in ((1366, 900), (390, 844)):
            for kind in ('video', 'audio'):
                page = browser.new_page(viewport={'width': size[0], 'height': size[1]}, has_touch=True)
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.set_content(f'''<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
                    <style>{css}</style></head><body class="{kind}-player-page">
                    <section id="playerSection" class="player-section"><div id="artplayer" class="artplayer-app"></div>
                    <div id="gestureHud" class="gesture-hud"></div></section><aside class="sidebar">Queue</aside>
                    </body></html>''')
                # Load after DOMContentLoaded: isolate the actual player/gesture code
                # from catalog/network startup, while keeping all real event handlers.
                page.add_script_tag(content=source)
                page.evaluate('''({kind,url})=>{
                    window.PLAYER_KIND=kind;
                    art=new FrontierMediaPlayer({container:'#artplayer',url});
                    initGestureControl();
                }''', {'kind': kind, 'url': media_url})
                page.wait_for_function('art.duration > 30 && art.video.readyState >= 2')
                duration = page.evaluate('art.duration')
                for mode in ('inline', 'web', 'native', 'inline-return'):
                    if mode == 'web':
                        page.evaluate('art.toggleWebFullscreen()')
                    elif mode == 'native':
                        page.evaluate('art.toggleWebFullscreen(); art.container.requestFullscreen()')
                        page.wait_for_function('document.fullscreenElement === art.container')
                    elif mode == 'inline-return':
                        page.evaluate('document.exitFullscreen()')
                        page.wait_for_function('!document.fullscreenElement')
                    page.wait_for_timeout(120)
                    for ratio in (0.1, 0.25, 0.5, 0.75, 0.9):
                        page.evaluate('art.pause(); art._showControls()')
                        rect = page.locator('.art-control-progress-inner').bounding_box()
                        page.mouse.click(rect['x'] + rect['width'] * ratio, rect['y'] + rect['height'] / 2)
                        page.wait_for_timeout(80)
                        actual = page.evaluate('art.currentTime')
                        row = {'viewport': size, 'kind': kind, 'mode': mode, 'input': 'click',
                               'ratio': ratio, 'expected': duration * ratio, 'actual': actual}
                        results.append(row)
                        if not expect_broken:
                            assert abs(actual - row['expected']) <= 0.15, row
                    if expect_broken:
                        continue
                    # Pointer capture must continue seeking outside the visible bar.
                    rect = page.locator('.art-control-progress-inner').bounding_box()
                    y = rect['y'] + rect['height'] / 2
                    page.mouse.move(rect['x'] + rect['width'] * .2, y)
                    page.mouse.down()
                    page.mouse.move(rect['x'] + rect['width'] * .8, y - 40, steps=6)
                    page.mouse.up()
                    page.wait_for_timeout(80)
                    actual = page.evaluate('art.currentTime')
                    assert abs(actual - duration * .8) <= .15, (mode, 'drag', actual)
                    # A touchscreen tap follows the same control ownership contract.
                    page.touchscreen.tap(rect['x'] + rect['width'] * .35, y)
                    page.wait_for_timeout(80)
                    assert abs(page.evaluate('art.currentTime') - duration * .35) <= .15
                    # Lower-quarter surface seeking still works, using current bounds.
                    page.evaluate('art.controls.show=false')
                    surface = page.locator('#artplayer').bounding_box()
                    page.mouse.click(surface['x'] + surface['width'] * .6, surface['y'] + surface['height'] * .8)
                    assert abs(page.evaluate('art.currentTime') - duration * .6) <= .15
                    page.evaluate('art._showControls()')
                    # Control clicks cannot be stolen by the lower-quarter gesture.
                    before = page.evaluate('art.currentTime')
                    page.locator('.art-control-volume').click()
                    assert page.evaluate('art.video.muted')
                    assert abs(page.evaluate('art.currentTime') - before) <= .15
                    page.locator('.art-control-volume').click()
                if not expect_broken:
                    page.locator('.art-control-fullscreen').click()
                    page.wait_for_function('document.fullscreenElement === art.container')
                    page.locator('.art-control-fullscreen').click()
                    page.wait_for_function('!document.fullscreenElement')
                    page.locator('.art-control-web-fullscreen').click()
                    assert page.evaluate('art._webFullscreen')
                    page.locator('.art-control-web-fullscreen').click()
                    assert not page.evaluate('art._webFullscreen')
                page.close()
        browser.close()
    assert not errors, errors
    if expect_broken:
        assert any(abs(v['actual'] - v['expected']) > 5 for v in results if v['mode'] in ('web', 'native'))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({'baseline': expect_broken, 'cases': results}, indent=2))
    print(json.dumps({'baseline': expect_broken, 'click_cases': len(results),
                      'maximum_time_error_seconds': max(abs(v['actual'] - v['expected']) for v in results)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[1])
    parser.add_argument('--media', type=pathlib.Path, required=True)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    parser.add_argument('--expect-broken', action='store_true')
    args = parser.parse_args()
    check(args.root, args.media, args.output, args.expect_broken)
