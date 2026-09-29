import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../static/js/player-directory-label.js', import.meta.url), 'utf8');

const requests = [];
let firstPulls = 0;

async function nativeFetch(input, init = {}) {
    const range = new Headers(init.headers || {}).get('Range');
    requests.push({input: String(input), range});

    if (requests.length === 1) {
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
assert.equal(requests.length, 2, 'one broken transfer must be resumed instead of exposed as a failure');
assert.equal(context.frontierCloudContinuousFetchRetry.status().resume_count, 1);

const before = requests.length;
await context.fetch('/api/v1/media/catalog/categories', {credentials: 'same-origin'});
assert.equal(requests.length, before + 1, 'non-media fetches must pass straight through the wrapper');

console.log('audio-continuous-fetch-retry-smoke-ok');
