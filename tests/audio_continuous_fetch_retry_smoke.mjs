import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../static/js/player-directory-label.js', import.meta.url), 'utf8');

const requests = [];
let firstPulls = 0;

async function nativeFetch(input, init = {}) {
    const url = new URL(String(input), 'https://520mall.cc');
    const range = new Headers(init.headers || {}).get('Range');
    requests.push({input: url.href, range});

    if (url.pathname !== '/api/v1/media/stream') {
        return new Response('{}', {
            status: 200,
            headers: {'Content-Type': 'application/json'},
        });
    }

    if (url.searchParams.get('probe') === 'seek') {
        const seekRequests = requests.filter(request => new URL(request.input).searchParams.get('probe') === 'seek');
        if (seekRequests.length === 1) {
            assert.equal(range, 'bytes=100-', 'an explicit seek Range must reach the origin request');
            let pulls = 0;
            return new Response(new ReadableStream({
                pull(controller) {
                    if (pulls++ === 0) controller.enqueue(new Uint8Array([5, 6]));
                    else controller.error(new Error('seek transfer interrupted'));
                },
            }), {
                status: 206,
                headers: {
                    'Content-Type': 'audio/mpeg',
                    'Content-Length': '4',
                    'Content-Range': 'bytes 100-103/104',
                },
            });
        }
        assert.equal(range, 'bytes=102-', 'seek retry must include the initial Range base offset');
        return new Response(new Uint8Array([7, 8]), {
            status: 206,
            headers: {
                'Content-Type': 'audio/mpeg',
                'Content-Length': '2',
                'Content-Range': 'bytes 102-103/104',
            },
        });
    }

    const mediaRequests = requests.filter(request => new URL(request.input).pathname === '/api/v1/media/stream');
    if (mediaRequests.length === 1) {
        const body = new ReadableStream({
            pull(controller) {
                if (firstPulls === 0) {
                    firstPulls += 1;
                    controller.enqueue(new Uint8Array([1, 2]));
                    return;
                }
                controller.error(new Error('simulated connection reset'));
            },
        });
        return new Response(body, {
            status: 200,
            headers: {
                'Content-Type': 'audio/mpeg',
                'Content-Length': '4',
            },
        });
    }

    assert.equal(range, 'bytes=2-', 'retry must resume from the exact byte offset already delivered');
    return new Response(new Uint8Array([3, 4]), {
        status: 206,
        headers: {
            'Content-Type': 'audio/mpeg',
            'Content-Length': '2',
            'Content-Range': 'bytes 2-3/4',
        },
    });
}

const host = {textContent: '', title: ''};
const context = {
    PLAYER_KIND: 'audio',
    playerSwitchSequence: 7,
    document: {
        getElementById(id) { return id === 'playerDirectoryLabel' ? host : null; },
    },
    location: {
        href: 'https://520mall.cc/media/player?path=music%2Ffixture',
        origin: 'https://520mall.cc',
        search: '?path=music%2Ffixture',
    },
    fetch: nativeFetch,
    Headers,
    Response,
    ReadableStream,
    URL,
    URLSearchParams,
    Uint8Array,
    DOMException,
    AbortController,
    console,
    setTimeout(callback) { callback(); return 1; },
    clearTimeout() {},
};
context.window = context;
vm.runInNewContext(source, context);

assert.equal(context.frontierCloudContinuousFetchRetry?.installed, true);
assert.equal(host.textContent, 'fixture');

const response = await context.fetch(
    '/api/v1/media/stream?file_path=music%2Ffixture%2Fa.mp3',
    {credentials: 'same-origin'},
);
const bytes = [...new Uint8Array(await response.arrayBuffer())];
assert.deepEqual(bytes, [1, 2, 3, 4]);
const mediaRequests = requests.filter(request => new URL(request.input).pathname === '/api/v1/media/stream');
assert.equal(mediaRequests.length, 2, 'one broken transfer must be resumed instead of exposed as a failure');
assert.equal(context.frontierCloudContinuousFetchRetry.status().resume_count, 1);

const seekResponse = await context.fetch(
    '/api/v1/media/stream?file_path=music%2Ffixture%2Fa.mp3&probe=seek',
    {credentials: 'same-origin', headers: {Range: 'bytes=100-'}},
);
assert.equal(seekResponse.headers.get('Content-Length'), '4');
assert.deepEqual([...new Uint8Array(await seekResponse.arrayBuffer())], [5, 6, 7, 8]);
assert.equal(context.frontierCloudContinuousFetchRetry.status().resume_count, 2);

const before = requests.length;
await context.fetch('/api/v1/media/catalog/categories', {credentials: 'same-origin'});
assert.equal(requests.length, before + 1, 'non-media fetches must pass straight through the wrapper');
assert.equal(requests.at(-1).range, null, 'non-media fetches must never receive Range retry headers');


async function isolatedRetry(fetchImpl) {
    const box = {...context, fetch: fetchImpl, playerSwitchSequence: 1,
        setTimeout: (callback) => setTimeout(callback, 2), clearTimeout};
    delete box.frontierCloudContinuousFetchRetry;
    box.window = box;
    vm.runInNewContext(source, box);
    return box;
}

let attempts = 0;
const outage = await isolatedRetry(async (_input, init) => {
    attempts += 1;
    if (attempts <= 4) return new Response('unavailable', {status: 503});
    assert.equal(init.signal.aborted, false);
    return new Response(new Uint8Array([9]), {headers: {'Content-Type': 'audio/mpeg', 'Content-Length': '1'}});
});
const recovered = await outage.fetch('/api/v1/media/stream', {credentials: 'same-origin'});
assert.deepEqual([...new Uint8Array(await recovered.arrayBuffer())], [9]);
assert.equal(attempts, 5, 'repeated HTTP outage must wait rather than reject or advance');
assert.equal(outage.playerSwitchSequence, 1);

let mismatchAttempts = 0;
let cancelledMismatches = 0;
const identity = await isolatedRetry(async (_input, init) => {
    mismatchAttempts += 1;
    const headers = new Headers(init.headers);
    if (mismatchAttempts === 1) {
        let reads = 0;
        return new Response(new ReadableStream({pull(c) {
            if (reads++ === 0) c.enqueue(new Uint8Array([1, 2]));
            else c.error(new Error('connection reset'));
        }}), {headers: {'Content-Length': '4', ETag: '"fixed-media"'}});
    }
    assert.equal(headers.get('Range'), 'bytes=2-');
    assert.equal(headers.get('If-Range'), '"fixed-media"');
    if (mismatchAttempts <= 3) {
        return new Response(new ReadableStream({cancel() { cancelledMismatches += 1; }}), {
            status: 206, headers: {'Content-Range': 'bytes 2-3/4', ETag: '"replacement-media"'},
        });
    }
    return new Response(new Uint8Array([3, 4]), {
        status: 206, headers: {'Content-Range': 'bytes 2-3/4', ETag: '"fixed-media"'},
    });
});
const unchanged = await identity.fetch('/api/v1/media/stream', {credentials: 'same-origin'});
assert.deepEqual([...new Uint8Array(await unchanged.arrayBuffer())], [1, 2, 3, 4]);
assert.equal(cancelledMismatches, 2, 'changed object bytes must never be concatenated');

let signalSeen;
let abortCalls = 0;
const cancellation = await isolatedRetry(async (_input, init) => {
    signalSeen = init.signal;
    return new Response(new ReadableStream({
        start(c) {
            init.signal.addEventListener('abort', () => {
                abortCalls += 1;
                c.error(new DOMException('cancelled', 'AbortError'));
            }, {once:true});
        },
    }));
});
const cancelledResponse = await cancellation.fetch('/api/v1/media/stream', {credentials: 'same-origin'});
await cancelledResponse.body.cancel('manual track change');
assert.equal(signalSeen.aborted, true, 'cancelling the exposed stream must abort the underlying fetch');
assert.equal(abortCalls, 1);

const stale = await isolatedRetry(async () => new Response('outage', {status:503}));
const oldRequest = stale.fetch('/api/v1/media/stream', {credentials:'same-origin'});
stale.playerSwitchSequence += 1;
await assert.rejects(oldRequest, error => error.name === 'AbortError');

console.log('audio-continuous-fetch-retry-smoke-ok');
