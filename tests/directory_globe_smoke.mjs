import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
function element(className = '') {
    const handlers = new Map(), classes = new Set(className.split(' '));
    const node = {className, children: [], attributes: {}, dataset: {pathLabel: 'Music / Artist'}, scrollLeft: 0, clientWidth: 600,
        addEventListener(type, callback) { handlers.set(type, callback); },
        setAttribute(key, value) { this.attributes[key] = value; },
        classList: {add(name) { classes.add(name); }, remove(name) { classes.delete(name); }, toggle(name, flag) { flag ? classes.add(name) : classes.delete(name); }, contains(name) { return classes.has(name); }},
        append(...nodes) { this.children.push(...nodes); }, replaceChildren(...nodes) { this.children = nodes; },
        querySelectorAll(selector) { return this.children.flatMap(child => [...(selector === '.card' && child.className === 'card' ? [child] : []), ...child.querySelectorAll(selector)]); },
        scrollTo(options) { this.lastScroll = options; this.scrollLeft = options.left; handlers.get('scroll')?.(); },
        setPointerCapture() {}, hasPointerCapture() { return false; },
        handlers};
    return node;
}
const grid = element(), cards = Array.from({length: 23}, () => element('card'));
grid.append(...cards);
const ids = ['globeStage', 'globeToggle', 'globeCount', 'globeHint', 'albumPagination', 'albumPages', 'albumPrevious', 'albumNext', 'albumPageStatus'];
const nodes = new Map(ids.map(id => [id, element()])); nodes.set('categoryGrid', grid);
const html = element('frontier-covers'); let reduced = false;
const context = {document: {documentElement: html, getElementById: id => nodes.get(id), createElement: () => element()},
    window: {dispatchEvent() {}, innerHeight: 800}, Event: class {}, matchMedia: () => ({get matches() { return reduced; }}), ResizeObserver: class {observe() {}}};
vm.runInNewContext(fs.readFileSync(new URL('../static/js/directory-globe.js', import.meta.url), 'utf8'), context);
context.window.FrontierDirectoryGlobe.refresh();
assert.deepEqual(grid.children.map(group => group.children.length), [9, 9, 5]);
assert.deepEqual(grid.querySelectorAll('.card'), cards, 'pagination must preserve server priority order');
nodes.get('globeStage').handlers.get('keydown')({key: 'ArrowRight', preventDefault() {}});
assert.equal(grid.scrollLeft, 600); assert.equal(grid.lastScroll.behavior, 'smooth');
assert.equal(nodes.get('albumPageStatus').textContent, '2 / 3');
assert.deepEqual(Array.from(context.window.FrontierDirectoryGlobe.visibleCards()), cards.slice(9, 18));
nodes.get('globeStage').getBoundingClientRect = () => ({height: 1200});
let wheelPrevented = false;
nodes.get('globeStage').handlers.get('wheel')({deltaY: 100, deltaX: 0, timeStamp: 1000, preventDefault() {wheelPrevented = true;}});
assert.equal(wheelPrevented, false, 'tall cover pages must preserve vertical scrolling');
assert.equal(grid.scrollLeft, 600, 'vertical wheel over a tall grid must not flip pages');
const click = {preventDefault() { this.prevented = true; }, stopPropagation() {}};
grid.handlers.get('pointerdown')({pointerType: 'mouse', button: 0, pointerId: 1, clientX: 400});
grid.handlers.get('pointermove')({pointerId: 1, clientX: 230});
grid.handlers.get('pointerup')({pointerId: 1, clientX: 230});
grid.handlers.get('click')({...click, detail: 1});
assert.equal(grid.scrollLeft, 1200, 'desktop drag must advance one page');
assert.equal(nodes.get('albumPageStatus').textContent, '3 / 3');
grid.handlers.get('pointerdown')({pointerType: 'mouse', button: 0, pointerId: 2, clientX: 400});
grid.handlers.get('pointermove')({pointerId: 2, clientX: 230});
grid.handlers.get('pointercancel')({pointerId: 2});
const afterCancel = {...click, detail: 1}; grid.handlers.get('click')(afterCancel);
assert.equal(afterCancel.prevented, undefined, 'cancelled drag must not consume the next album selection');
reduced = true; nodes.get('globeStage').handlers.get('keydown')({key: 'ArrowLeft', preventDefault() {}});
assert.equal(grid.lastScroll.behavior, 'instant');
nodes.get('globeToggle').handlers.get('click')();
assert.equal(html.classList.contains('frontier-covers'), false);
assert.deepEqual(grid.children, cards, 'list must use the original cards and order');
assert.equal(nodes.get('albumPagination').hidden, true);
nodes.get('globeToggle').handlers.get('click')();
assert.deepEqual(grid.children.map(group => group.children.length), [9, 9, 5]);
const template = fs.readFileSync(new URL('../static/media/category.html', import.meta.url), 'utf8');
for (const fixture of [
    {kind: 'video', title: '前沿视讯', path: '', category: '视频', item: '视频', action: '观看', planet: '视频星球'},
    {kind: 'music', title: '前沿音乐', path: '', category: '音乐', item: '专辑', action: '聆听', planet: '音乐星球'},
    {kind: 'video', title: '前沿视讯 - 演出', path: 'vido/演出', category: '视频', item: '视频', action: '观看', planet: '视频星球'},
    {kind: 'music', title: '前沿音乐 - 歌手', path: 'music/歌手', category: '音乐', item: '专辑', action: '聆听', planet: '音乐星球'},
]) {
    const nodes = new Map([...template.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], element()]));
    for (const [id, node] of nodes) {
        const tag = template.match(new RegExp('<[^>]*\\bid="' + id + '"[^>]*>'))?.[0] || '';
        const aria = tag.match(/aria-label="([^"]+)"/); if (aria) node.attributes['aria-label'] = aria[1];
    }
    nodes.get('pageBrand').dataset.pageTitle = fixture.title;
    nodes.get('globeTitle').textContent = template.match(/<h1 id="globeTitle">([^<]+)<\/h1>/)[1];
    const config = fixture.path ? {bootstrap: [], url: '', cacheKey: ''}
        : {url: '/api/v1/media/catalog/categories?media_type=' + fixture.kind};
    const inline = template.match(/<script>([\s\S]*?)<\/script>/)[1]
        .replace('{{STUN_URLS_JSON}}', '[]').replace('{{WEBRTC_INTERVAL_MS}}', '1000')
        .replace('{{CATALOG_CONFIG_JSON}}', JSON.stringify(config));
    const context = {document: {documentElement: element('frontier-covers'),
        getElementById: id => nodes.get(id), createElement: () => element()}, URL,
        location: {href: 'https://media.test/api/v1/media/' + fixture.kind + (fixture.path ? '/category?path=' + encodeURIComponent(fixture.path) : '')},
        window: {dispatchEvent() {}, innerHeight: 800}, Event: class {}, matchMedia: () => ({matches: false}),
        ResizeObserver: class {observe() {}}};
    vm.runInNewContext(inline, context);
    assert.equal(nodes.get('globeStage').attributes['aria-label'], `九宫格${fixture.item}目录，左右滑动翻页`);
    assert.equal(nodes.get('directoryEyebrow').textContent, '前沿UI / ' + fixture.kind.toUpperCase());
    assert.equal(nodes.get('globePanel').attributes['aria-label'], fixture.category + '目录');
    assert.equal(nodes.get('albumPagination').attributes['aria-label'], fixture.item + '翻页');
    assert.equal(context.window.frontierCloudCatalogRevealKind, fixture.kind);
    if (!fixture.path) {
        assert.equal(nodes.get('pageBrandSuffix').textContent, fixture.category + '目录');
        assert.equal(nodes.get('globeTitle').textContent, 'This is my party');
    } else assert.equal(nodes.get('globeTitle').textContent, fixture.path.split('/')[1]);
    const pathLabel = nodes.get('globeHint').textContent;
    vm.runInNewContext(fs.readFileSync(new URL('../static/js/directory-globe.js', import.meta.url), 'utf8'), context);
    nodes.get('globeToggle').handlers.get('click')();
    assert.equal(nodes.get('globeStage').attributes['aria-label'], fixture.category + '目录列表');
    assert.equal(nodes.get('globeHint').textContent, '');
    nodes.get('globeToggle').handlers.get('click')();
    assert.equal(nodes.get('globeStage').attributes['aria-label'], `九宫格${fixture.item}目录，左右滑动翻页`);
    assert.equal(nodes.get('globeHint').textContent, pathLabel);
}
console.log('directory-paging-smoke-ok: nine covers, swipe, stable order, list retention, reduced motion, music/video labels');
