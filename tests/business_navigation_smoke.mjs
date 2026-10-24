import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function fixture(parent, current = 'https://media.test/api/v1/media/music/category?path=music%2Fa%2Fb') {
    const handlers = new Map(), assigned = [], storage = new Map();
    const body = {parentElement: null}, root = {clientWidth: 400};
    const target = {parentElement: body, closest: () => null};
    const location = {href: current, origin: new URL(current).origin, assign: href => assigned.push(href)};
    const scope = {URL, location, innerWidth: 400, Date,
        history: {back() { throw Error('browser history must not be used'); }},
        sessionStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
        getComputedStyle: node => ({overflowX: node.overflowX || 'visible'}),
        document: {body, documentElement: root, querySelector: () => parent ? {href: new URL(parent, current).href} : null,
            addEventListener: (name, callback) => handlers.set(name, callback)},
        window: {addEventListener: (name, callback) => handlers.set(name, callback)}};
    vm.runInNewContext(fs.readFileSync('static/js/business-navigation.js', 'utf8'), scope);
    const fire = (name, values = {}) => {
        const event = {target, touches: [], changedTouches: [], timeStamp: 1000, cancelable: true,
            preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}, ...values};
        handlers.get(name)?.(event);
        return event;
    };
    const swipe = (x = 390, end = 280, dy = 0) => {
        fire('touchstart', {touches: [{identifier: 1, clientX: x, clientY: 100}]});
        fire('touchmove', {touches: [{identifier: 1, clientX: end, clientY: 100 + dy}], timeStamp: 1100});
        return fire('touchend', {changedTouches: [{identifier: 1, clientX: end, clientY: 100 + dy}], timeStamp: 1200});
    };
    return {assigned, fire, swipe, target, body, storage};
}
for (const parent of ['/api/v1/media', '/api/v1/media/music', '/api/v1/media/music/category?path=music%2Fa&include_hidden=true']) {
    const test = fixture(parent);
    assert.equal(test.swipe().defaultPrevented, true);
    assert.deepEqual(test.assigned, ['https://media.test' + parent]);
}
for (const [x, end, dy] of [[200, 80, 0], [390, 375, 0], [390, 280, 130], [390, 399, 0]]) {
    const test = fixture('/api/v1/media/music'); test.swipe(x, end, dy);
    assert.equal(test.assigned.length, 0, 'interior paging, vertical scroll and short/reverse gestures are retained');
}
const cancelled = fixture('/api/v1/media/music');
cancelled.fire('touchstart', {touches: [{identifier: 1, clientX: 390, clientY: 100}]});
cancelled.fire('touchcancel');
cancelled.fire('touchend', {changedTouches: [{identifier: 1, clientX: 200, clientY: 100}]});
assert.equal(cancelled.assigned.length, 0);
const trackpad = fixture('/api/v1/media/music');
trackpad.fire('wheel', {deltaX: 50, deltaY: 0, deltaMode: 0});
trackpad.fire('wheel', {deltaX: 50, deltaY: 0, deltaMode: 0, timeStamp: 1100});
assert.equal(trackpad.assigned.length, 1);
const paging = fixture('/api/v1/media/music');
paging.target.scrollWidth = 800; paging.target.clientWidth = 400; paging.target.scrollLeft = 0; paging.target.overflowX = 'auto';
assert.equal(paging.fire('wheel', {deltaX: 100, deltaY: 0}).defaultPrevented, undefined);
assert.equal(paging.assigned.length, 0);
paging.target.scrollLeft = 400;
paging.fire('wheel', {deltaX: 100, deltaY: 0});
assert.equal(paging.assigned.length, 1);
const inertia = fixture('/api/v1/media/music');
inertia.storage.set('frontier:business-up:at', String(Date.now()));
assert.equal(inertia.fire('wheel', {deltaX: 150, deltaY: 0}).defaultPrevented, true);
assert.equal(inertia.assigned.length, 0, 'trackpad inertia must not skip another business level');
assert.equal(fixture('https://other.test/').fire('wheel', {deltaX: 150, deltaY: 0}).defaultPrevented, undefined);
assert.equal(fixture(null).assigned.length, 0);
console.log('business-navigation-smoke-ok: business parent, no browser history, touch/trackpad, paging, cancellation and inertia');
