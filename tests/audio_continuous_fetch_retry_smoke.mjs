import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
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

for (const status of [401, 403, 404, 409, 422, 429]) {
    let failedRequests = 0;
    const denied = await isolatedRetry(async () => {
        failedRequests += 1;
        return new Response('authorization/integrity failure', {status});
    });
    await assert.rejects(denied.fetch('/__fc_media/session/music%2Fa.mp3', {credentials: 'same-origin'}),
        status === 429 ? /限额/ : /授权或完整性/);
    assert.equal(failedRequests, 1, `HTTP ${status} must hold encrypted playback instead of looping`);
}
let virtualRequests = 0;
const virtual = await isolatedRetry(async (_input, init) => {
    virtualRequests += 1;
    if (virtualRequests === 1) {
        let pulls = 0;
        return new Response(new ReadableStream({pull(output) {
            if (pulls++ === 0) output.enqueue(new Uint8Array([10, 11]));
            else output.error(new TypeError('encrypted source network interrupted'));
        }}), {headers: {'Content-Length': '4', ETag: '"fc-plain-file-id"'}});
    }
    assert.equal(new Headers(init.headers).get('Range'), 'bytes=2-');
    return new Response(new Uint8Array([12, 13]), {status: 206,
        headers: {'Content-Range': 'bytes 2-3/4', ETag: '"fc-plain-file-id"'}});
});
const virtualResponse = await virtual.fetch('/__fc_media/session/music%2Fa.mp3', {credentials: 'same-origin'});
assert.deepEqual([...new Uint8Array(await virtualResponse.arrayBuffer())], [10, 11, 12, 13]);
assert.equal(virtualRequests, 2, 'encrypted streaming resumes at delivered plaintext byte offset');

const tampered = await isolatedRetry(async () => new Response(new ReadableStream({
    pull(output) { output.error(new TypeError('AES-GCM authentication failed')); },
}), {headers: {'Content-Length': '4'}}));
tampered.FrontierMediaCrypto = {failureFor: () => null};
const tamperedResponse = await tampered.fetch('/__fc_media/session/music%2Fa.mp3', {credentials: 'same-origin'});
tampered.FrontierMediaCrypto.failureFor = () => '加密媒体完整性校验失败';
await assert.rejects(tamperedResponse.arrayBuffer(), /完整性校验失败/);

// Exercise actual HTTP headers and body boundaries, including a 79-byte range
// crossing the browser encryption chunk boundary. These requests must not turn
// into an open-ended retry or expose the complete object's Content-Length.
const fixtureBytes = Buffer.alloc(1024 * 1024 + 200);
for (let i = 0; i < fixtureBytes.length; i += 1) fixtureBytes[i] = i % 251;
const httpRequests = [];
const server = http.createServer((req, res) => {
    const range = req.headers.range;
    httpRequests.push({method: req.method, range, marker: req.headers['x-fixture-marker']});
    let start = 0;
    let end = fixtureBytes.length - 1;
    if (range !== undefined) {
        const match = /^bytes=(\d*)-(\d*)$/.exec(range);
        if (match && (match[1] || match[2])) {
            if (match[1]) {
                start = Number(match[1]);
                if (match[2]) end = Math.min(end, Number(match[2]));
            } else {
                start = Math.max(0, fixtureBytes.length - Number(match[2]));
            }
        } else {
            start = NaN;
        }
        if (!Number.isSafeInteger(start) || start > end) {
            res.writeHead(416, {'Content-Range': `bytes */${fixtureBytes.length}`, 'Content-Length': '0'});
            res.end();
            return;
        }
    }
    const headers = {'Content-Type': 'audio/mpeg', 'Accept-Ranges': 'bytes',
        'Content-Length': String(end - start + 1), ETag: '"http-range-fixture"'};
    if (range !== undefined) headers['Content-Range'] = `bytes ${start}-${end}/${fixtureBytes.length}`;
    res.writeHead(range === undefined ? 200 : 206, headers);
    res.end(req.method === 'HEAD' ? undefined : fixtureBytes.subarray(start, end + 1));
});
await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
});
try {
    const origin = `http://127.0.0.1:${server.address().port}`;
    const httpInit = {credentials: 'same-origin', signal: AbortSignal.timeout(5000)};
    const httpBox = await isolatedRetry((input, init) => globalThis.fetch(
        typeof input === 'string' || input instanceof URL ? new URL(String(input), origin) : input, init));
    httpBox.location = {href: `${origin}/media/player`, origin, search: ''};
    for (const path of ['/api/v1/media/stream', '/__fc_media/session/fixture.mp3']) {
        for (const [range, start, end] of [
            ['bytes=1048541-1048619', 1048541, 1048619],
            ['bytes=-23', fixtureBytes.length - 23, fixtureBytes.length - 1],
        ]) {
            const before = httpRequests.length;
            const response = await httpBox.fetch(path, {...httpInit, headers: {Range: range}});
            const bytes = Buffer.from(await response.arrayBuffer());
            assert.equal(response.status, 206);
            assert.equal(response.headers.get('Content-Length'), String(end - start + 1));
            assert.equal(response.headers.get('Content-Range'), `bytes ${start}-${end}/${fixtureBytes.length}`);
            assert.equal(response.headers.get('Content-Type'), 'audio/mpeg');
            assert.deepEqual(bytes, fixtureBytes.subarray(start, end + 1));
            assert.equal(httpRequests.length, before + 1, `${range} must never expand into a retry`);
            assert.equal(httpRequests.at(-1).range, range);
        }
        for (const range of ['bytes=invalid', 'bytes=40-20', 'bytes=1-2,4-5', 'bytes=9007199254740992-']) {
            const before = httpRequests.length;
            const response = await httpBox.fetch(path, {...httpInit, headers: {Range: range}});
            assert.equal(response.status, 416, 'unsupported or malformed Range keeps the native error response');
            assert.equal(response.headers.get('Content-Range'), `bytes */${fixtureBytes.length}`);
            assert.equal((await response.arrayBuffer()).byteLength, 0);
            assert.equal(httpRequests.length, before + 1);
            assert.equal(httpRequests.at(-1).range, range);
        }
        const before = httpRequests.length;
        const head = await httpBox.fetch(new Request(`${origin}${path}`, {method: 'HEAD'}),
            httpInit);
        assert.equal(head.status, 200);
        assert.equal(head.headers.get('Content-Length'), String(fixtureBytes.length));
        assert.equal((await head.arrayBuffer()).byteLength, 0);
        assert.equal(httpRequests.length, before + 1, 'HEAD without a body must not enter stream retries');
        assert.equal(httpRequests.at(-1).method, 'HEAD');
    }

    const requestRange = new Request(`${origin}/api/v1/media/stream`,
        {headers: {Range: 'bytes=100-122', 'X-Fixture-Marker': 'request-header'}});
    const beforeRequest = httpRequests.length;
    const boundedRequest = await httpBox.fetch(requestRange, httpInit);
    assert.equal(boundedRequest.headers.get('Content-Length'), '23');
    assert.deepEqual(Buffer.from(await boundedRequest.arrayBuffer()), fixtureBytes.subarray(100, 123));
    assert.equal(httpRequests.length, beforeRequest + 1);
    assert.equal(httpRequests.at(-1).range, 'bytes=100-122');
    assert.equal(httpRequests.at(-1).marker, 'request-header');

    const overridden = await httpBox.fetch(requestRange, {...httpInit,
        headers: {Range: 'bytes=150-159'}});
    assert.equal(overridden.headers.get('Content-Length'), '10');
    assert.deepEqual(Buffer.from(await overridden.arrayBuffer()), fixtureBytes.subarray(150, 160));
    assert.equal(httpRequests.at(-1).range, 'bytes=150-159', 'init headers override Request headers');
    assert.equal(httpRequests.at(-1).marker, undefined);

    const openRequest = new Request(`${origin}/__fc_media/session/fixture.mp3`,
        {headers: {Range: 'bytes=1048500-', 'X-Fixture-Marker': 'open-request-header'}});
    const open = await httpBox.fetch(openRequest, httpInit);
    assert.equal(open.status, 206);
    assert.equal(open.headers.get('Content-Length'), String(fixtureBytes.length - 1048500));
    assert.deepEqual(Buffer.from(await open.arrayBuffer()), fixtureBytes.subarray(1048500));
    assert.equal(httpRequests.at(-1).range, 'bytes=1048500-');
    assert.equal(httpRequests.at(-1).marker, 'open-request-header');

    const full = await httpBox.fetch('/api/v1/media/stream', httpInit);
    assert.equal(full.status, 200);
    assert.equal(full.headers.get('Content-Length'), String(fixtureBytes.length));
    assert.deepEqual(Buffer.from(await full.arrayBuffer()), fixtureBytes);

    const beforeEOF = httpRequests.length;
    await assert.rejects(httpBox.fetch('/api/v1/media/stream', {credentials: 'same-origin',
        headers: {Range: `bytes=${fixtureBytes.length}-`}, signal: AbortSignal.timeout(500)}), /请求范围无效/);
    assert.equal(httpRequests.length, beforeEOF + 1, 'an open Range past EOF must terminate on its first 416');
    assert.equal(httpBox.frontierCloudContinuousFetchRetry.status().retry_count, 0);
    assert.equal(httpBox.frontierCloudContinuousFetchRetry.status().resume_count, 0);
} finally {
    server.closeAllConnections();
    await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
}

const requestAbort = new AbortController();
const requestSignal = await isolatedRetry(async (_input, init) => {
    return new Response(new ReadableStream({
        start(c) { init.signal.addEventListener('abort', () => c.error(init.signal.reason), {once: true}); },
    }));
});
const requestSignalResponse = await requestSignal.fetch(new Request('https://520mall.cc/api/v1/media/stream',
    {signal: requestAbort.signal}), {credentials: 'same-origin'});
requestAbort.abort(new DOMException('request cancelled', 'AbortError'));
await assert.rejects(requestSignalResponse.arrayBuffer(), error => error.name === 'AbortError');

console.log('audio-continuous-fetch-retry-smoke-ok');
