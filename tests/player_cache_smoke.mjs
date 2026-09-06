import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync('static/js/player.js', 'utf8');
const flush = () => new Promise(setImmediate);
function playerContext(fetchImpl) {
    let clock = 10000;
    const requests = [], revoked = [], objects = [];
    const element = {style: {}, classList: {add() {}, remove() {}}};
    const context = vm.createContext({
        window: {addEventListener() {}}, navigator: {},
        document: {getElementById: () => element, querySelector: () => null, querySelectorAll: () => []},
        performance: {now: () => clock}, AbortController, Blob, setTimeout, clearTimeout,
        URL: {
            createObjectURL(blob) { objects.push(blob); return `blob:track-${objects.length}`; },
            revokeObjectURL(url) { revoked.push(url); },
        },
        currentMediaList: ['a', 'b', 'c'].map(id => ({media_id: id, type: 'audio', url: `/${id}`, cover: '', play_score: 0})),
        fetch: async (url, options) => { requests.push({url, options}); return fetchImpl(url, options); },
    });
    vm.runInContext(source, context);
    vm.runInContext("art = {duration: 100, currentTime: 5, playing: false, url: '/a', play: async () => {}, notice: {}}", context);
    return {context, requests, revoked, objects, advance: n => {clock += n;}, run: code => vm.runInContext(code, context)};
}

{
    const p = playerContext(async () => new Response(new Uint8Array([1, 2, 3]), {headers: {'Content-Type': 'audio/wav'}}));
    p.run('checkAndPreloadNext(5)');
    await flush();
    assert.equal(p.run('nextPreload.status'), 'ready');
    assert.equal(p.objects[0].size, 3);
    assert.equal(p.requests[0].options.headers, undefined);
    p.run('currentMediaList[2].preference = 7; playNext()');
    await flush();
    assert.equal(p.run('art.url'), 'blob:track-1');
    assert.equal(p.run('currentIndex'), 1);
    assert.equal(p.requests.length, 1);
    p.run('playNext()');
    assert.equal(p.run('art.url'), '/c');
    assert.deepEqual(p.revoked, ['blob:track-1']);
}

for (const failure of ['network', 'status', 'truncated']) {
    let attempts = 0;
    const p = playerContext(async () => {
        if (attempts++ === 0) {
            if (failure === 'network') throw new Error('offline');
            if (failure === 'status') return new Response('failure', {status: 500});
            return new Response(new ReadableStream({start(controller) {controller.error(new Error('body failed'));}}));
        }
        return new Response('complete');
    });
    p.run('checkAndPreloadNext(5)');
    await flush();
    assert.equal(p.run('nextPreload.status'), 'failed');
    p.run('checkAndPreloadNext(6)');
    assert.equal(p.requests.length, 1);
    p.advance(4000);
    p.run('checkAndPreloadNext(10)');
    await flush();
    assert.equal(p.run('nextPreload.status'), 'ready');
    assert.equal(p.requests.length, 2);
}

{
    let release;
    const pending = new Promise(resolve => {release = resolve;});
    const p = playerContext(async () => pending);
    p.run('checkAndPreloadNext(5); playNext()');
    assert.equal(p.run('art.url'), '/b');
    assert.equal(p.requests[0].options.signal.aborted, true);
    release(new Response('late data'));
    await flush();
    assert.equal(p.objects.length, 0);
}

{
    const p = playerContext(async () => new Response('large', {headers: {'Content-Length': String(129 * 1024 * 1024)}}));
    p.run('checkAndPreloadNext(5)');
    await flush();
    assert.equal(p.run('nextPreload.status'), 'skipped');
    assert.equal(p.requests[0].options.signal.aborted, true);
    p.advance(60000);
    p.run('checkAndPreloadNext(65)');
    assert.equal(p.requests.length, 1);
}

{
    let respond;
    const p = playerContext(() => new Promise(resolve => {respond = resolve;}));
    p.context.playbackSessionId = 'session';
    p.run("resetPlaybackAccounting(currentMediaList[0]); playbackState.accumulated=30; reportValidPlayback()");
    p.run('resetPlaybackAccounting(currentMediaList[1])');
    respond(new Response(JSON.stringify({play_score: 1})));
    await flush();
    assert.equal(p.run('playbackState.reported'), false);
    assert.equal(p.run('currentMediaList[0].play_score'), 1);
}

console.log('player-cache-smoke-ok');
