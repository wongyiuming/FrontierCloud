"""Real Chromium: shipped lyric overrides retain rows while wrapping and moving.

Catalog, audio decoding and network startup are isolated. --assets-root permits
the same regression against an unchanged released tree as well as new assets.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re

from playwright.sync_api import sync_playwright


VIEWPORTS = [(390, 844), (768, 1024), (1360, 768)]
ASSETS = ["static/media/audio-player.html", "static/css/player.css",
          "static/css/lyrics-window.css", "static/js/player.js",
          "static/js/audio-continuous-stream.js", "static/js/lyrics-window.js"]

# Use actual element/text-node identity, independent of implementation row keys.
PROBE = r"""() => {
    const track = document.getElementById('inlineLyricsTrack'), viewport = track.parentElement;
    const failures = [], stages = [];
    let stage = null, frames = 0, stopped = false;
    const fail = (kind, detail) => {
        if (failures.length < 25) failures.push({kind, ...detail});
    };
    // The temporary departing row is outside the seven-line logical window.
    const snapshot = () => new Map([...track.children].filter(row => row.textContent.trim() && !row.classList.contains('departing'))
        .map(row => [row.textContent, {row, textNode: row.firstChild}]));
    const shared = () => {
        const wanted = new Set(activeLyricEntries.slice(Math.max(0, stage.index - 3), stage.index + 4)
            .map(entry => entry.text));
        return [...stage.before].filter(([text]) => wanted.has(text));
    };
    const mutations = records => {
        if (!stage) return;
        for (const [text, old] of shared()) {
            for (const mutation of records) {
                if (mutation.type === 'characterData' && old.row.contains(mutation.target)) {
                    fail('retained_text_rewritten', {text});
                }
                if (mutation.type === 'childList' && [...mutation.removedNodes]
                    .some(node => node === old.row || node.contains?.(old.row) || node === old.textNode)) {
                    fail('retained_row_or_text_removed', {text});
                }
            }
        }
    };
    const observer = new MutationObserver(mutations);
    observer.observe(track, {childList: true, characterData: true, subtree: true});
    const finishStage = () => {
        if (!stage) return;
        mutations(observer.takeRecords());
        const now = snapshot(), common = shared();
        for (const [text, old] of common) {
            const found = now.get(text);
            if (!found || found.row !== old.row || !old.row.isConnected || !track.contains(old.row)) {
                fail('retained_row_identity_lost', {text});
            } else if (found.textNode !== old.textNode) {
                fail('retained_text_node_identity_lost', {text});
            }
        }
        stages.push({index: stage.index, retained: common.length, rows: track.children.length});
        stage = null;
    };
    const sample = () => {
        if (stopped) return;
        frames++;
        if (stage) {
            const rows = track.querySelectorAll('.sync-lyric.current'), row = rows[0];
            const expected = activeLyricEntries[stage.index]?.text;
            if (rows.length !== 1 || !row || !row.textContent.trim() || row.textContent !== expected) {
                fail('current_missing_empty_or_stale', {index: stage.index, text: row?.textContent});
            } else {
                const box = row.getBoundingClientRect(), clip = viewport.getBoundingClientRect();
                const css = getComputedStyle(row);
                // Huge wrapped rows need a visible intersection, not an impossible
                // requirement that their entire height fits a small viewport.
                const visibleWidth = Math.min(box.right, clip.right, innerWidth) - Math.max(box.left, clip.left, 0);
                const visibleHeight = Math.min(box.bottom, clip.bottom, innerHeight) - Math.max(box.top, clip.top, 0);
                if (css.display === 'none' || css.visibility !== 'visible' || Number(css.opacity) <= .01 ||
                    visibleWidth <= 1 || visibleHeight <= 1) {
                    fail('current_not_painted_in_viewport', {index: stage.index, visibleWidth, visibleHeight});
                }
            }
        }
        requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
    window.lyricPaintProbe = {
        begin(index) { finishStage(); stage = {index, before: snapshot()}; },
        finish() {
            finishStage(); stopped = true; observer.disconnect();
            return {failures, stages, frames, settled: lyricSlideTimer === null};
        },
    };
}"""

ROW_FACTS = r"""row => {
    const css = getComputedStyle(row), box = row.getBoundingClientRect();
    const range = document.createRange(); range.selectNodeContents(row);
    const fragments = [...range.getClientRects()];
    return {text: row.textContent, fontSize: css.fontSize, whiteSpace: css.whiteSpace,
        height: box.height, scrollHeight: row.scrollHeight, clientHeight: row.clientHeight,
        scrollWidth: row.scrollWidth, clientWidth: row.clientWidth, lines: fragments.length,
        allTextInsideRow: fragments.every(rect => rect.top >= box.top - 1 && rect.bottom <= box.bottom + 1),
        expectedFontSize: Math.max(14, Math.min(44, document.getElementById('inlineLyrics').clientHeight / 7.3)) * 1.16};
}"""


def shipped_page(assets):
    template = assets[ASSETS[0]]
    assert template.index('{{PLAYER_CSS_URL}}') < template.index('/static/css/lyrics-window.css')
    assert template.index('{{PLAYER_JS_URL}}') < template.index('/static/js/audio-continuous-stream.js') < template.index('/static/js/lyrics-window.js')
    inline = re.findall(r'<script\s*>(.*?)</script>', template, re.S)
    assert len(inline) == 2 and 'FrontierPlayerLayout' in inline[-1]
    markup = re.sub(r'<script\b[^>]*>.*?</script>', '', template, flags=re.S)
    for url, name in [('{{PLAYER_CSS_URL}}', 'static/css/player.css'),
                      ('/static/css/lyrics-window.css', 'static/css/lyrics-window.css')]:
        tag = re.compile(r'<link\b[^>]*href="' + re.escape(url) + r'"[^>]*>')
        markup, count = tag.subn(lambda _match: '<style>' + assets[name] + '</style>', markup)
        assert count == 1, name
    return markup, inline[-1]


def exercise(browser, output, assets_root=None):
    root = (assets_root or Path(__file__).resolve().parents[1]).resolve()
    assets = {name: (root/name).read_text(encoding='utf-8') for name in ASSETS}
    markup, layout_script = shipped_page(assets)
    output.mkdir(parents=True, exist_ok=True)
    results, failures = [], []
    report = {'real_chromium': True, 'assets_root': str(root),
              'assets': {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in ASSETS},
              'shipped_template_and_shared_overrides_loaded': True,
              'catalog_audio_and_network_startup_isolated': True,
              'layouts': results, 'failures': failures, 'inputs': [], 'passed': False}

    def check(condition, name, detail):
        if not condition:
            failures.append({'check': name, **detail})

    try:
        for width, height in VIEWPORTS:
            page = browser.new_page(viewport={'width': width, 'height': height})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', lambda route: route.abort())
            row = {'viewport': [width, height], 'transitions': [], 'page_errors': errors}
            results.append(row)
            try:
                page.set_content(markup)
                # Load after DOMContentLoaded to isolate rendering from APIs.
                page.add_script_tag(content="window.PLAYER_KIND = 'audio';")
                for name in ASSETS[3:]:
                    page.add_script_tag(content=assets[name])
                page.add_script_tag(content=layout_script)
                page.wait_for_timeout(300)
                short = [{'time': index, 'text': f'第{index:02d}句 短歌词'} for index in range(18)]
                mixed = [{'time': index, 'text': (f'第{index:02d}句 长歌词自动换行不改变字体大小并且完整保留文字顶端和底部' * 3)
                          if index % 2 == 0 else f'第{index:02d}句 短歌词'} for index in range(18)]

                def show(entries, index):
                    report['inputs'].append({'action': 'showSynchronizedLyrics', 'viewport': [width, height], 'entries': entries, 'index': index})
                    page.evaluate('({entries,index}) => showSynchronizedLyrics(entries,index)', {'entries': entries, 'index': index})
                    page.wait_for_timeout(80)

                def transition(name, steps, resize=None):
                    report['inputs'].append({'action': 'lyric transition', 'viewport': [width, height], 'name': name, 'steps': steps, 'resize': resize, 'settlement_wait_ms': 650})
                    page.evaluate(PROBE)
                    if resize:
                        index = page.evaluate('activeLyricIndex')
                        page.evaluate('(index) => lyricPaintProbe.begin(index)', index)
                        page.set_viewport_size({'width': resize[0], 'height': resize[1]})
                        page.evaluate('sizeSynchronizedLyrics()')
                    else:
                        for index, force, delay in steps:
                            page.evaluate('({index,force}) => { lyricPaintProbe.begin(index); updateSynchronizedLyrics(index,force); }',
                                          {'index': index, 'force': force})
                            if delay:
                                page.wait_for_timeout(delay)
                    # Real 480 ms transition plus 30 ms settlement, no clock jump.
                    page.wait_for_timeout(650)
                    facts = page.evaluate('lyricPaintProbe.finish()')
                    row['transitions'].append({'name': name, **facts})
                    check(not facts['failures'], name + ':retention_and_paint', {'viewport': [width, height], **facts})
                    check(facts['frames'] >= 3 and facts['settled'], name + ':actual_settlement', {'viewport': [width, height], **facts})
                    check(all(stage['retained'] > 0 for stage in facts['stages']), name + ':overlap_observed', {'viewport': [width, height], **facts})

                show(short, 5)
                baseline_font = page.locator('.sync-lyric.current').evaluate('(element) => getComputedStyle(element).fontSize')
                transition('ordinary_cue_and_timer_settlement', [(6, False, 0)])
                transition('repeat_current_cue', [(6, True, 0)])
                check(page.locator('.sync-lyric.current').evaluate('(element) => getComputedStyle(element).fontSize') == baseline_font,
                      'cue_does_not_shrink_font', {'viewport': [width, height]})
                transition('quick_cues_and_backward_seek', [(7, False, 45), (8, False, 45), (5, True, 0)])

                show(mixed, 5)
                transition('short_to_wrapped_cue', [(6, False, 0)])
                inline = page.locator('.sync-lyric.current').evaluate(ROW_FACTS)
                row['inline'] = inline
                check(inline['lines'] > 1 and inline['allTextInsideRow'] and inline['whiteSpace'] == 'normal',
                      'long_cue_wraps_without_glyph_clipping', {'viewport': [width, height], **inline})
                check(inline['scrollHeight'] <= inline['clientHeight'] + 1 and inline['scrollWidth'] <= inline['clientWidth'] + 1,
                      'long_cue_has_no_internal_clipping', {'viewport': [width, height], **inline})
                check(abs(float(inline['fontSize'][:-2]) - inline['expectedFontSize']) < .1,
                      'original_shared_font_size_preserved', {'viewport': [width, height], **inline})
                page.screenshot(path=str(output/f'inline-lyrics-{width}.png'))
                transition('wrapped_to_short_cue', [(7, False, 0)])
                transition('resize_keeps_cue_rows', [], resize=(width - 24, height - 40))
                page.set_viewport_size({'width': width, 'height': height})
                page.wait_for_timeout(300)

                replacement = [{'time': index, 'text': f'新曲目 第{index:02d}句'} for index in range(18)]
                show(replacement, 5)
                new_text = page.locator('.sync-lyric.current').text_content()
                row['replacement_current'] = new_text
                check(new_text == replacement[5]['text'], 'new_track_has_new_text', {'viewport': [width, height], 'actual': new_text})

                show(mixed, 6)
                page.evaluate('openFullscreenLyrics()')
                page.wait_for_timeout(80)
                fullscreen = page.locator('.fullscreen-lyric-line').evaluate_all("""rows => rows.map(element => ({
                    text: element.textContent, whiteSpace: getComputedStyle(element).whiteSpace,
                    height: element.clientHeight, scroll: element.scrollHeight,
                    fontSize: parseFloat(getComputedStyle(element).fontSize)}))""")
                row['fullscreen'] = fullscreen
                check([entry['text'] for entry in fullscreen] == [entry['text'] for entry in mixed],
                      'fullscreen_preserves_all_lyrics', {'viewport': [width, height]})
                check(page.locator('.fullscreen-lyric-line.current').text_content() == mixed[6]['text'],
                      'fullscreen_highlights_current_cue', {'viewport': [width, height]})
                check(all(entry['whiteSpace'] == 'normal' and entry['scroll'] <= entry['height'] + 1 and 10 <= entry['fontSize'] <= 48
                          for entry in fullscreen), 'fullscreen_wrap_and_font_rules', {'viewport': [width, height]})
                page.screenshot(path=str(output/f'lyrics-{width}.png'))
                page.evaluate('closeFullscreenLyrics()')
                check(not errors, 'no_browser_script_errors', {'viewport': [width, height], 'errors': errors})
            except Exception as error:
                failures.append({'check': 'browser_scenario_exception', 'viewport': [width, height],
                                 'error_type': type(error).__name__, 'detail': str(error)[:1200]})
            finally:
                page.close()
    finally:
        report['passed'] = not failures and len(results) == len(VIEWPORTS)
        (output/'evidence.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    assert report['passed'], json.dumps(failures, ensure_ascii=False)
    print('PASS: shipped lyric overrides retain rows/text nodes and stay visible through cue settlement, seek and resize at unchanged wrapping/font rules')

    return report


def run(output, assets_root=None):
    with sync_playwright() as runtime:
        options = {'headless': True, 'args': ['--no-sandbox']}
        if os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'):
            options['executable_path'] = os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE']
        browser = runtime.chromium.launch(**options)
        try:
            return exercise(browser, output, assets_root)
        finally:
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--assets-root', type=Path)
    options = parser.parse_args()
    run(options.output, options.assets_root)
