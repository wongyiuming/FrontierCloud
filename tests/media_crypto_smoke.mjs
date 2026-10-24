import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';
import {MessageChannel} from 'node:worker_threads';

const directory = process.env.FRONTIER_CRYPTO_JS_DIR
    || (process.env.FC_CRYPTO_COMPILED === '1' ? 'static/js/compiled' : 'static/js');
const source = name => fs.readFileSync(`${directory}/${name}`, 'utf8');
let now = Date.now();
let serverNow = now - 7 * 60 * 60 * 1000;
let elapsed = 0;
const performance = {timeOrigin: now, now: () => elapsed};
function advance(milliseconds) {
    elapsed += milliseconds;
    serverNow += milliseconds;
    now += milliseconds;
}
class Clock extends Date { static now() { return now; } }
const calls = [];
const records = new Map();
const pathRecords = new Map();
const sessions = new Map();
const temporary = new Map();
const plainReads = [];
const cipherRequests = [];
const controllerMessages = [];
const downloadAnchors = [];
const workerListeners = new Map();
let pageMessage;
let nextId = 1;
let nextSession = 1;
let keyDelay = 0;
let mismatchedEnvelope = false;
let sessionLifetime = 899;
let handshakeDelay = 0;
let sessionStatus = 0;
let keyStatus = 0;
let cipherStatus = 0;
let worker;
let common;

const opfs = {
    async getDirectoryHandle() { return this; },
    async *entries() { for (const entry of temporary) yield entry; },
    async removeEntry(name) { temporary.delete(name); },
    async getFileHandle(name) {
        temporary.set(name, []);
        return {
            async createWritable() {
                return {
                    async write(value) { temporary.get(name).push(new Uint8Array(value).slice()); },
                    async close() {}, async abort() { temporary.delete(name); },
                };
            },
            async getFile() { return new Blob(temporary.get(name)); },
        };
    },
};
const client = {id: 'browser-tab', postMessage(data, ports = []) {
    pageMessage({data, source: controller, ports});
}};
const controller = {postMessage(data, ports = []) {
    controllerMessages.push(data.type);
    for (const listener of workerListeners.get('message') || []) listener({data, ports, source: client});
}};
async function workerFetch(input, init = {}) {
    const request = new Request(new URL(String(input), 'https://cloud.example'), init);
    let response;
    for (const listener of workerListeners.get('fetch') || []) {
        listener({request, clientId: client.id, respondWith(value) { response = Promise.resolve(value); }});
    }
    assert(response, 'virtual encrypted bytes must be handled by the service worker');
    return response;
}
async function envelope(active, record) {
    const iv = webcrypto.getRandomValues(new Uint8Array(12));
    const wrapped = await webcrypto.subtle.encrypt({name: 'AES-GCM', iv,
        additionalData: new TextEncoder().encode(`frontiercloud:key-envelope:v1:${active.id}:${record.meta.file_id}`)},
    active.wrapKey, record.raw);
    return {iv: common.encode(iv), wrapped_key: common.encode(wrapped), expires_at: active.expires + (mismatchedEnvelope ? 1 : 0)};
}
async function backendFetch(input, init = {}) {
    const url = new URL(String(input), 'https://cloud.example');
    if (url.pathname.startsWith('/__fc_')) return workerFetch(url, init);
    if (!url.pathname.includes('/crypto/')) {
        const bytes = new TextEncoder().encode('plain legacy lyric');
        return new Response(bytes, {headers: {'Content-Length': String(bytes.length)}});
    }
    const body = init.body ? JSON.parse(init.body) : {};
    calls.push({url: url.pathname, body});
    if (url.pathname.endsWith('/session')) {
        if (sessionStatus) return Response.json({detail: 'temporary key session limit'}, {status: sessionStatus});
        const pair = await webcrypto.subtle.generateKey({name: 'ECDH', namedCurve: 'P-256'}, false, ['deriveBits']);
        const clientKey = await webcrypto.subtle.importKey('raw', common.decode(body.public_key),
            {name: 'ECDH', namedCurve: 'P-256'}, false, []);
        const shared = await webcrypto.subtle.deriveBits({name: 'ECDH', public: clientKey}, pair.privateKey, 256);
        const hkdf = await webcrypto.subtle.importKey('raw', shared, 'HKDF', false, ['deriveKey']);
        const salt = new Uint8Array(32).fill(71);
        const id = 'session-' + nextSession++;
        const wrapKey = await webcrypto.subtle.deriveKey({name: 'HKDF', hash: 'SHA-256', salt,
            info: new TextEncoder().encode('frontiercloud:browser-wrap:v1:' + id)},
        hkdf, {name: 'AES-GCM', length: 256}, false, ['encrypt']);
        const expires = Math.floor(serverNow / 1000) + 900;
        sessions.set(id, {id, wrapKey, expires});
        advance(handshakeDelay);
        return Response.json({session_id: id, expires_at: expires, expires_in: sessionLifetime, salt: common.encode(salt),
            public_key: common.encode(await webcrypto.subtle.exportKey('raw', pair.publicKey))});
    }
    const active = sessions.get(body.session_id);
    if (!active || active.expires * 1000 <= serverNow) return Response.json({detail: 'expired session'}, {status: 403});
    if (url.pathname.endsWith('/prepare')) {
        const size = body.plaintext_size;
        const meta = {version: 1, algorithm: 'AES-256-GCM', file_id: (nextId++).toString(16).padStart(32, '0'),
            nonce_prefix: common.encode(webcrypto.getRandomValues(new Uint8Array(8))), plaintext_size: size,
            chunk_size: 1048576, ciphertext_size: size + 16 * Math.ceil(size / 1048576)};
        const record = {meta, raw: webcrypto.getRandomValues(new Uint8Array(32))};
        records.set(meta.file_id, record);
        return Response.json({encryption: meta, key_envelope: await envelope(active, record), preparation_token: 'signed-descriptor'});
    }
    if (url.pathname.endsWith('/key')) {
        if (keyStatus) return Response.json({detail: 'current object access denied'}, {status: keyStatus});
        advance(keyDelay);
        const record = pathRecords.get(body.file_path);
        if (!record) return Response.json({detail: 'missing'}, {status: 404});
        return Response.json({encryption: record.meta, key_envelope: await envelope(active, record),
            source_url: record.source, content_type: 'audio/mpeg', filename: body.file_path.split('/').pop()});
    }
    throw new Error('unexpected crypto API: ' + url);
}

const page = {
    crypto: webcrypto, TextEncoder, TextDecoder, Uint8Array, ArrayBuffer, DataView, URL, URLSearchParams,
    Blob, Headers, Request, Response, ReadableStream, MessageChannel, Date: Clock, performance,
    atob, btoa, setTimeout, clearTimeout, console, isSecureContext: true,
    location: new URL('https://cloud.example/api/v1/media/admin'),
    document: {getElementById: id => id === 'uploadFiles' ? {} : null,
        createElement() { return {click() { downloadAnchors.push(this); }}; }},
    navigator: {storage: {getDirectory: async () => opfs}, serviceWorker: {controller,
        register: async () => ({}), ready: Promise.resolve({}),
        addEventListener(type, listener) { if (type === 'message') pageMessage = listener; },
    }},
    requestHeaders: () => ({'Content-Type': 'application/json', 'X-CSRF-Token': 'test'}),
    addEventListener() {}, dispatchEvent() {}, CustomEvent: class {}, fetch: backendFetch,
};
page.window = page;
vm.createContext(page);
vm.runInContext(source('media-crypto-common.js'), page);
common = page.FrontierCryptoCommon;
vm.runInContext(source('media-crypto.js'), page);
await new Promise(resolve => setTimeout(resolve, 0));

function inputFile(bytes, name) {
    return {name, size: bytes.length, slice(start, end) {
        return {async arrayBuffer() {
            plainReads.push(end - start);
            return bytes.slice(start, end).buffer;
        }};
    }, async arrayBuffer() { throw new Error('whole-file plaintext buffering is forbidden'); }};
}
async function prepare(bytes, path, sourceUrl) {
    const result = await page.FrontierMediaCrypto.encryptFile(inputFile(bytes, path.split('/').pop()));
    const record = records.get(result.encryption.file_id);
    record.ciphertext = new Uint8Array(await result.file.arrayBuffer());
    record.source = sourceUrl;
    pathRecords.set(path, record);
    assert.equal(result.file.size, result.encryption.ciphertext_size);
    await result.cleanup();
    return record;
}
const validationCalls = calls.length;
for (const name of ['bad.mp3', 'bad.flac', 'bad.wav', 'bad.m4a', 'bad.mp4', 'bad.webm', 'bad.mkv', 'bad.txt']) {
    await assert.rejects(page.FrontierMediaCrypto.encryptUploadFile(
        inputFile(new TextEncoder().encode('not media'), name)), /媒体格式/);
}
for (const invalid of [new TextEncoder().encode('not lyrics'), new Uint8Array([0xff, 0xfe]),
    new TextEncoder().encode('[00:01]bad\0lyric'), new Uint8Array(2 * 1024 * 1024 + 1)]) {
    await assert.rejects(page.FrontierMediaCrypto.encryptUploadFile(inputFile(invalid, 'bad.lrc')));
}
assert.equal(calls.length, validationCalls, 'invalid plaintext creates no handshake/preparation requests');
assert.equal(temporary.size, 0, 'invalid plaintext creates no ciphertext temporary object');
for (const [name, header] of [
    ['valid.mp3', new TextEncoder().encode('ID3')],
    ['valid.flac', new TextEncoder().encode('fLaC')],
    ['valid.wav', new TextEncoder().encode('RIFF0000WAVE')],
    ['valid.m4a', new TextEncoder().encode('0000ftyp0000')],
    ['valid.mp4', new TextEncoder().encode('0000ftyp0000')],
    ['valid.webm', new Uint8Array([0x1a, 0x45, 0xdf, 0xa3])],
    ['valid.mkv', new Uint8Array([0x1a, 0x45, 0xdf, 0xa3])],
    ['valid.lrc', new TextEncoder().encode('[00:01.00]valid lyric')],
]) {
    const validUpload = await page.FrontierMediaCrypto.encryptUploadFile(inputFile(header, name));
    assert.equal(validUpload.encryption.plaintext_size, header.length);
    await validUpload.cleanup();
}
const bytes = Uint8Array.from({length: 2 * 1048576 + 97}, (_, index) => index % 251);
const first = await prepare(bytes, 'music/a.mp3', 'https://direct.example/cipher/a');
const secondBytes = new TextEncoder().encode('second encrypted resource');
const second = await prepare(secondBytes, 'music/b.mp3', 'https://cloud.example/relay/b');
assert.equal(calls.filter(call => call.url.endsWith('/session')).length, 1, 'multiple upload files reuse one P-256 handshake');
assert.equal(temporary.size, 0, 'ciphertext temporary files are removed after upload');
assert(plainReads.every(length => length <= 1048576), 'plaintext reads remain within one encryption chunk');
assert.notDeepEqual(first.ciphertext.subarray(0, 1024), bytes.subarray(0, 1024));
const fileKey = await webcrypto.subtle.importKey('raw', first.raw, 'AES-GCM', false, ['decrypt']);
const firstRange = common.cipherRange(first.meta, 0);
assert.deepEqual(new Uint8Array(await webcrypto.subtle.decrypt(common.parameters(first.meta, 0), fileKey,
    first.ciphertext.subarray(firstRange.start, firstRange.end + 1))), bytes.subarray(0, 1048576));
await assert.rejects(webcrypto.subtle.decrypt(common.parameters(first.meta, 1), fileKey,
    first.ciphertext.subarray(firstRange.start, firstRange.end + 1)), 'moving chunks must fail authentication');
await assert.rejects(webcrypto.subtle.decrypt(common.parameters({...first.meta, plaintext_size: first.meta.plaintext_size - 1}, 0),
    fileKey, first.ciphertext.subarray(firstRange.start, firstRange.end + 1)), 'length is authenticated');
assert.equal(fileKey.extractable, false);
assert.throws(() => common.validate({...first.meta, ciphertext_size: bytes.length}));
assert.throws(() => common.validate({...first.meta, plaintext_size: 0, ciphertext_size: 0}));
assert.equal(common.range('bytes=0-1,3-4', bytes.length), null);
assert.equal(common.range('bytes=99999999999-', bytes.length), null);
assert.deepEqual(JSON.parse(JSON.stringify(common.parseLyrics('[offset:100]\n[00:01.20][00:02.30]词\n[ar:Artist]'))),
    [{time: 1.3, text: '词'}, {time: 2.4, text: '词'}]);
// These expectations are shared with the server's ParseLRC contract. The
// compiled and source modules must preserve exactly the same valid grammar.
const lyricVectors = JSON.parse(fs.readFileSync('protocol/lrc_parsing_vectors.json', 'utf8'));
for (const vector of lyricVectors) {
    if (vector.error) assert.throws(() => common.parseLyrics(vector.input),
        error => error.message === vector.error, vector.name);
    else assert.deepEqual(JSON.parse(JSON.stringify(common.parseLyrics(vector.input))), vector.entries, vector.name);
}
assert.equal(common.parseLyrics('[00:01]' + '😀'.repeat(4000))[0].text, '😀'.repeat(4000),
    'the 4000-character boundary counts Unicode code points, as Go counts UTF-8 runes');
assert.throws(() => common.parseLyrics('[00:01]' + '乐'.repeat(4001)), /4000/);
assert.throws(() => common.parseLyrics('[00:01]' + '😀'.repeat(4001)), /4000/);
assert.throws(() => common.parseLyrics('[00:01]bad\ud800'), /UTF-8/);
const maximumLyrics = Array.from({length: 10000}, (_, index) => '[00:01]' + index).join('\n');
assert.equal(common.parseLyrics(maximumLyrics).length, 10000);
assert.throws(() => common.parseLyrics(maximumLyrics + '\n[00:01]extra'), /10000/);
assert.equal(common.parseLyrics(Array.from({length: 10001}, () => '[00:01]duplicate').join('\n')).length, 1,
    'duplicate entries are removed before the 10000-entry limit');

// A recorded encrypted-source lyric is an independent opaque object. Its
// plaintext never enters the preparation request or the stored footer shape.
const snapshotLyrics = Array.from({length: 1200}, (_, index) => ({time: index / 10,
    text: 'private recorded lyric marker ' + 'x'.repeat(900)}));
const snapshotRequests = [];
const prepareSnapshot = async fields => {
    snapshotRequests.push(fields);
    return (await backendFetch('/api/v1/karaoke/account/recordings/crypto/prepare', {
        method: 'POST', body: JSON.stringify(fields),
    })).json();
};
const snapshot = await page.FrontierMediaCrypto.encryptRecordingLyrics(snapshotLyrics, prepareSnapshot);
assert(snapshot.encrypted_lyrics.encryption.plaintext_size > 1048576, 'snapshot exercises both authenticated chunks');
assert.deepEqual(Object.keys(snapshotRequests[0]).sort(), ['plaintext_size', 'session_id']);
assert(!JSON.stringify(snapshot).includes('private recorded lyric marker'), 'ticket/footer contain no original lyric text');
assert.deepEqual(Object.keys(snapshot.encrypted_lyrics).sort(), ['ciphertext', 'encryption']);
assert.equal(snapshot.preparation_token, 'signed-descriptor');
const snapshotRecord = records.get(snapshot.encrypted_lyrics.encryption.file_id);
let returnedSnapshot = snapshot.encrypted_lyrics;
const authorizeSnapshot = async fields => {
    assert.deepEqual(Object.keys(fields), ['session_id']);
    return {encrypted_lyrics: returnedSnapshot, key_envelope: await envelope(sessions.get(fields.session_id), snapshotRecord)};
};
assert.deepEqual(JSON.parse(JSON.stringify(await page.FrontierMediaCrypto.decryptRecordingLyrics(authorizeSnapshot))), snapshotLyrics);
const anotherSnapshot = await page.FrontierMediaCrypto.encryptRecordingLyrics(snapshotLyrics, prepareSnapshot);
assert.notEqual(anotherSnapshot.encrypted_lyrics.encryption.file_id, snapshot.encrypted_lyrics.encryption.file_id,
    'each saved recording gets a new independent immutable key/nonce identity');
assert.equal(calls.filter(call => call.url.endsWith('/session')).length, 1,
    'recordings and media retain the existing multi-file P-256 session');
const snapshotCipher = common.decode(snapshot.encrypted_lyrics.ciphertext);
snapshotCipher[1048592] ^= 1;
returnedSnapshot = {...snapshot.encrypted_lyrics, ciphertext: common.encode(snapshotCipher)};
await assert.rejects(page.FrontierMediaCrypto.decryptRecordingLyrics(authorizeSnapshot), 'second-chunk tag failure returns no partial lyric snapshot');
returnedSnapshot = {...snapshot.encrypted_lyrics, ciphertext: snapshot.encrypted_lyrics.ciphertext + '\n'};
await assert.rejects(page.FrontierMediaCrypto.decryptRecordingLyrics(authorizeSnapshot), /描述无效/, 'snapshot requires canonical strict base64');
returnedSnapshot = snapshot.encrypted_lyrics;
await assert.rejects(page.FrontierMediaCrypto.decryptRecordingLyrics(async () => {
    const error = new Error('not current owner'); error.status = 403; throw error;
}), /not current owner/, 'owner authorization errors never fall through to plaintext lyrics');
const snapshotsBeforeOversize = snapshotRequests.length;
await assert.rejects(page.FrontierMediaCrypto.encryptRecordingLyrics(
    Array.from({length: 1500}, (_, index) => ({time: index, text: 'x'.repeat(1000)})), prepareSnapshot), /1400 KiB/);
assert.equal(snapshotRequests.length, snapshotsBeforeOversize, 'oversize plaintext never leaves the browser even in preparation');
await assert.rejects(page.FrontierMediaCrypto.encryptRecordingLyrics([{time: Infinity, text: 'bad'}], prepareSnapshot));
await assert.rejects(page.FrontierMediaCrypto.encryptRecordingLyrics([{time: 0, text: 'x'.repeat(4097)}], prepareSnapshot));

const cryptoKeyConstructor = fileKey.constructor;
function createWorker() {
    workerListeners.clear();
    worker = {
        crypto: webcrypto, CryptoKey: cryptoKeyConstructor, TextEncoder, TextDecoder, Uint8Array, Uint32Array,
        ArrayBuffer, DataView, URL, URLSearchParams, Headers, Request, Response, ReadableStream, AbortController,
        MessageChannel, Date: Clock,
        performance: {timeOrigin: performance.timeOrigin - 5000, now: () => elapsed + 5000},
        atob, btoa, setTimeout, clearTimeout,
        location: new URL('https://cloud.example/media-crypto-sw.js'),
        clients: {get: async id => id === client.id ? client : null, claim: async () => {}},
        skipWaiting: async () => {},
        addEventListener(type, listener) {
            if (!workerListeners.has(type)) workerListeners.set(type, []);
            workerListeners.get(type).push(listener);
        },
        importScripts() { vm.runInContext(source('media-crypto-common.js'), worker); },
        async fetch(input, init = {}) {
            const record = [...records.values()].reverse().find(value => value.source === String(input));
            if (!record) return backendFetch(input, init);
            const header = new Headers(init.headers).get('Range');
            cipherRequests.push({url: String(input), range: header});
            if (cipherStatus) return new Response('ciphertext request limit', {status: cipherStatus});
            const range = /^bytes=(\d+)-(\d+)$/.exec(header);
            assert(range, 'ciphertext fetches must name one exact authenticated chunk');
            const start = Number(range[1]); const end = Number(range[2]);
            assert(end - start + 1 <= 1048576 + 16, 'network reads remain within one ciphertext chunk');
            const payload = record.ciphertext.slice(start, end + 1);
            return new Response(payload, {status: 206, headers: {
                'Content-Range': `bytes ${start}-${end}/${record.ciphertext.length}`,
                'Content-Length': String(payload.length), 'Content-Type': 'application/octet-stream',
            }});
        },
    };
    worker.self = worker;
    vm.createContext(worker);
    vm.runInContext(source('media-crypto-sw.js'), worker);
}
createWorker();
const catalog = [{media_path: 'music/a.mp3', encryption: first.meta}, {media_path: 'music/b.mp3', encryption: second.meta}];
await page.FrontierMediaCrypto.prepareCatalog(catalog);
assert.match(catalog[0].url, /^\/__fc_media\/.*\?object=/);
vm.runInContext(`capabilities.clear();
    for (let index = 0; index < 256; index++)
        capabilities.set(index.toString(16).padStart(32, '0'), {clientId: 'crashed-' + index});
    grants.set('00000000000000000000000000000000:dead:', {});
    zipPlans.set('dead-plan', {token: '00000000000000000000000000000000'});`, worker);
await page.FrontierMediaCrypto.ensureWorker();
assert.equal(vm.runInContext('capabilities.size', worker), 1, 'crashed clients cannot permanently exhaust registration');
assert.equal(vm.runInContext("grants.has('00000000000000000000000000000000:dead:') || zipPlans.has('dead-plan')", worker), false,
    'dead-client reclamation removes its cached keys and ZIP plans');
vm.runInContext(`capabilities.clear();
    for (let index = 0; index < 256; index++)
        capabilities.set(index.toString(16).padStart(32, '0'), {clientId: 'browser-tab'});`, worker);
let fullRegistration;
const fullEvent = {data: {type: 'fc-crypto-register', token: 'f'.repeat(32)}, source: client,
    ports: [{postMessage(value) { fullRegistration = value; }}], waitUntil: value => value};
for (const listener of workerListeners.get('message')) listener(fullEvent);
await new Promise(resolve => setTimeout(resolve, 0));
assert(fullRegistration.error, '256 live capabilities retain the cap');
assert.equal(vm.runInContext('capabilities.size', worker), 256);
vm.runInContext('capabilities.clear()', worker);
await page.FrontierMediaCrypto.ensureWorker();

let response = await workerFetch(catalog[0].url, {headers: {Range: 'bytes=1048541-1048619'}});
assert.equal(response.status, 206);
assert.equal(response.headers.get('Content-Range'), `bytes 1048541-1048619/${bytes.length}`);
assert.equal(response.headers.get('Content-Length'), '79');
assert.deepEqual(new Uint8Array(await response.arrayBuffer()), bytes.subarray(1048541, 1048620));
assert.equal(cipherRequests.length, 2, 'a cross-chunk seek only fetches its two covering chunks');
response = await workerFetch(catalog[0].url, {headers: {Range: 'bytes=0-31'}});
assert.deepEqual(new Uint8Array(await response.arrayBuffer()), bytes.subarray(0, 32));
assert.equal(cipherRequests.length, 2, 'a repeated tag read reuses authenticated ciphertext without another network read');
assert.ok(vm.runInContext('cipherBytes <= CIPHER_CACHE_BYTES', worker));
response = await workerFetch(catalog[0].url, {headers: {Range: 'bytes=-17'}});
assert.deepEqual(new Uint8Array(await response.arrayBuffer()), bytes.slice(-17));
assert.equal(cipherRequests.length, 3, 'suffix Range fetches the final chunk only');
response = await workerFetch(catalog[0].url, {method: 'HEAD'});
assert.equal(response.headers.get('Content-Length'), String(bytes.length));
assert.equal((await response.arrayBuffer()).byteLength, 0);
response = await workerFetch(catalog[0].url, {headers: {Range: 'bytes=9000000-'}});
assert.equal(response.status, 416);
assert.equal(cipherRequests.length, 3, 'HEAD and invalid Range never consume ciphertext');
response = await workerFetch(catalog[1].url);
assert.deepEqual(new Uint8Array(await response.arrayBuffer()), secondBytes);
assert.equal(calls.filter(call => call.url.endsWith('/session')).length, 1, 'playback across files reuses the same session');
assert.equal(calls.filter(call => call.url.endsWith('/key')).length, 3,
    'cached ciphertext requires a fresh key authorization while uncached reads retain normal envelope reuse');

const originalDeadline = vm.runInContext('[...grants.values()][0].deadline', worker);
assert.equal(originalDeadline, performance.timeOrigin + 899000, 'grant lifetime starts before the handshake POST');
for (const skew of [-7, 7, -7]) {
    now = serverNow + skew * 60 * 60 * 1000;
    response = await workerFetch(catalog[1].url);
    assert.deepEqual(new Uint8Array(await response.arrayBuffer()), secondBytes);
    assert.equal(calls.filter(call => call.url.endsWith('/session')).length, 1,
        'wall-clock offsets and jumps cannot expire or renew live authorization');
}
keyDelay = 5000;
page.FrontierMediaCrypto.resetFault('music/b.mp3');
response = await workerFetch(catalog[1].url);
assert.deepEqual(new Uint8Array(await response.arrayBuffer()), secondBytes);
assert.equal(vm.runInContext('[...grants.values()].at(-1).deadline', worker), originalDeadline,
    'later file grants and response delays never extend the fixed session deadline');
keyDelay = 0;
mismatchedEnvelope = true;
page.FrontierMediaCrypto.resetFault('music/b.mp3');
assert.equal((await workerFetch(catalog[1].url)).status, 403, 'envelope lifetime must match its handshake exactly');
mismatchedEnvelope = false;
page.FrontierMediaCrypto.resetFault('music/b.mp3');
const originalPageMessage = pageMessage;
for (const invalidDeadline of [NaN, Infinity, common.monotonicNow() - 1, common.monotonicNow() + 900001]) {
    pageMessage = event => {
        const reply = event.ports[0];
        return originalPageMessage({...event, ports: [{postMessage(grant) {
            reply.postMessage({...grant, deadline: invalidDeadline});
        }}]});
    };
    page.FrontierMediaCrypto.resetFault('music/b.mp3');
    assert.equal((await workerFetch(catalog[1].url)).status, 403,
        'worker rejects missing/expired/unbounded deadlines without a wall-clock fallback');
}
pageMessage = originalPageMessage;
page.FrontierMediaCrypto.resetFault('music/b.mp3');

advance(901000);
response = await workerFetch(catalog[1].url);
assert.deepEqual(new Uint8Array(await response.arrayBuffer()), secondBytes);
assert.equal(calls.filter(call => call.url.endsWith('/session')).length, 2, 'expired authorization performs one fresh handshake');

// Recreated workers lose all in-memory keys/capabilities. A valid live tab can
// reauthorize its next seek, without keeping keys in persistent browser storage.
vm.runInContext('capabilities.clear(); grants.clear();', worker);
response = await workerFetch(catalog[1].url);
assert.deepEqual(new Uint8Array(await response.arrayBuffer()), secondBytes);

// A stopped worker starts with a fresh global scope. ZIP must register the
// already initialized page directly, before any virtual fetch can restore it.
createWorker();
assert.equal(vm.runInContext('capabilities.size + grants.size + zipPlans.size', worker), 0);
const restartMessages = controllerMessages.length;
const restartSessions = calls.filter(call => call.url.endsWith('/session')).length;
await page.FrontierMediaCrypto.downloadPlan([
    {path: 'music/b.mp3', filename: 'b.mp3', encryption: second.meta, size_bytes: secondBytes.length},
    {path: 'lyrics/plain.lrc', filename: 'plain.lrc', url: '/plain-lyric', size_bytes: 18},
    {path: 'music/other/b.mp3', filename: 'b.mp3', url: '/plain-lyric', size_bytes: 18},
]);
assert.deepEqual(controllerMessages.slice(restartMessages), ['fc-crypto-register', 'fc-crypto-zip']);
assert(!Object.hasOwn(downloadAnchors.at(-1), 'download'),
    'virtual ZIP uses attachment navigation so Chromium dispatches the worker fetch');
const zipId = vm.runInContext('[...zipPlans.keys()][0]', worker);
const zipItems = vm.runInContext('zipPlans.get(' + JSON.stringify(zipId) + ').items', worker);
response = await workerFetch('/__fc_zip/' + zipId);
const zip = new Uint8Array(await response.arrayBuffer());
assert.equal(response.headers.get('Content-Type'), 'application/zip');
let cursor = 0;
const archived = [];
const archivedNames = [];
for (let file = 0; file < 3; file += 1) {
    const view = new DataView(zip.buffer, cursor);
    assert.equal(view.getUint32(0, true), 0x04034b50);
    const nameLength = view.getUint16(26, true);
    const extraLength = view.getUint16(28, true);
    archivedNames.push(new TextDecoder().decode(zip.slice(cursor + 30, cursor + 30 + nameLength)));
    const size = Number(view.getBigUint64(30 + nameLength + 4, true));
    const start = cursor + 30 + nameLength + extraLength;
    archived.push(zip.slice(start, start + size));
    const descriptor = new DataView(zip.buffer, start + size);
    assert.equal(descriptor.getUint32(0, true), 0x08074b50);
    let crc = page.FrontierMediaCrypto.crcUpdate(0xffffffff, archived.at(-1));
    assert.equal(descriptor.getUint32(4, true), (crc ^ 0xffffffff) >>> 0);
    assert.equal(Number(descriptor.getBigUint64(8, true)), size);
    cursor = start + size + 24;
}
assert.deepEqual(archived[0], secondBytes);
assert.equal(new TextDecoder().decode(archived[1]), 'plain legacy lyric');
assert.equal(new TextDecoder().decode(archived[2]), 'plain legacy lyric');
assert.deepEqual(archivedNames, ['music/b.mp3', 'lyrics/plain.lrc', 'music/other/b.mp3'],
    'ZIP entries retain logical directories even when API filenames are identical basenames');
assert.equal(new DataView(zip.buffer, cursor).getUint32(0, true), 0x02014b50);
assert.equal(new DataView(zip.buffer, zip.length - 98).getUint32(0, true), 0x06064b50);
assert.equal(Number(new DataView(zip.buffer, zip.length - 98).getBigUint64(24, true)), 3);
assert.equal(calls.filter(call => call.url.endsWith('/session')).length, restartSessions,
    'worker restart restores its page capability and decrypts ZIP without another master handshake');
assert.equal((await workerFetch('/__fc_zip/' + zipId)).status, 403, 'download plan capability is consumed once');

// Exercise admission through the real worker message handler. Eight abandoned
// plans must expire without evicting any other valid plan or relaxing bounds.
const zipCapability = vm.runInContext('[...capabilities.keys()][0]', worker);
function queueZip(id, items = zipItems, sourceClient = client) {
    let reply;
    for (const listener of workerListeners.get('message') || []) {
        listener({data: {type: 'fc-crypto-zip', token: zipCapability, download_token: id, items},
            source: sourceClient, ports: [{postMessage(value) { reply = value; }}]});
    }
    return reply;
}
const planKeys = () => [...vm.runInContext('[...zipPlans.keys()]', worker)];
const boundarySessions = calls.filter(call => call.url.endsWith('/session')).length;
for (const invalidItems of [null, {}, Array(5001).fill(zipItems[0])]) {
    assert(queueZip('invalid-plan', invalidItems).error, 'worker rejects malformed or oversized ZIP plans');
    assert.equal(planKeys().length, 0);
}
const abandonedIds = Array.from({length: 8}, (_, index) => zipCapability + '-abandoned-' + index);
for (const [index, id] of abandonedIds.entries()) {
    assert.equal(queueZip(id, index === 0 ? Array(5000).fill(zipItems[0]) : zipItems).ok, true,
        'the existing 5000-entry limit remains inclusive');
}
assert.deepEqual(planKeys(), abandonedIds);
assert(queueZip('over-cap').error, 'eight live plans refuse a ninth plan');
assert(queueZip(abandonedIds[0]).error, 'a full queue cannot overwrite another live plan');
assert.equal(queueZip('another-client', zipItems, {id: 'other-browser-tab'}), undefined,
    'another client cannot admit plans using this page capability');
assert.deepEqual(planKeys(), abandonedIds);
advance(120000);
assert(queueZip('at-deadline').error, 'the exact expiry boundary retains the existing fetch validity');
assert.deepEqual(planKeys(), abandonedIds, 'the capacity check must retain every still-valid plan');
advance(1);
const firstRecoveredId = zipCapability + '-after-expiry';
assert.equal(queueZip(firstRecoveredId).ok, true, 'eight expired plans cannot permanently block new ZIP downloads');
assert.deepEqual(planKeys(), [firstRecoveredId]);
for (const id of abandonedIds) {
    assert.equal((await workerFetch('/__fc_zip/' + id)).status, 403, 'an expired plan cannot be consumed later');
}
for (let index = 0; index < 6; index += 1) assert.equal(queueZip('older-' + index).ok, true);
advance(60000);
const survivorId = zipCapability + '-survivor';
assert.equal(queueZip(survivorId).ok, true);
const survivorDeadline = vm.runInContext('zipPlans.get(' + JSON.stringify(survivorId) + ').expires', worker);
assert(queueZip('mixed-over-cap').error);
advance(60001);
const laterId = zipCapability + '-later';
assert.equal(queueZip(laterId).ok, true);
assert.deepEqual(planKeys(), [survivorId, laterId], 'a sweep removes only expired plans from a mixed queue');
assert.equal(vm.runInContext('zipPlans.get(' + JSON.stringify(survivorId) + ').expires', worker), survivorDeadline,
    'admitting another ZIP never extends an existing plan deadline');

// A consumed archive is held by its stream, not the pending-plan queue. Reading
// it may outlive the two-minute admission capability without a key renewal.
const activeArchive = await workerFetch('/__fc_zip/' + survivorId);
assert.equal(activeArchive.status, 200);
const activeReader = activeArchive.body.getReader();
const activeParts = [(await activeReader.read()).value];
advance(120001);
const currentId = zipCapability + '-current';
assert.equal(queueZip(currentId).ok, true);
assert.deepEqual(planKeys(), [currentId]);
while (true) {
    const part = await activeReader.read();
    if (part.done) break;
    activeParts.push(part.value);
}
const recoveredZip = new Uint8Array(await new Blob(activeParts).arrayBuffer());
let recoveredCursor = 0;
for (const [index, item] of zipItems.entries()) {
    const header = new DataView(recoveredZip.buffer, recoveredCursor);
    assert.equal(header.getUint32(0, true), 0x04034b50);
    const nameLength = header.getUint16(26, true);
    const extraLength = header.getUint16(28, true);
    assert.equal(new TextDecoder().decode(recoveredZip.slice(recoveredCursor + 30, recoveredCursor + 30 + nameLength)), item.path);
    const size = Number(header.getBigUint64(30 + nameLength + 4, true));
    const start = recoveredCursor + 30 + nameLength + extraLength;
    const body = recoveredZip.slice(start, start + size);
    assert.deepEqual(body, index === 0 ? secondBytes : new TextEncoder().encode('plain legacy lyric'));
    const descriptor = new DataView(recoveredZip.buffer, start + size);
    assert.equal(descriptor.getUint32(0, true), 0x08074b50);
    assert.equal(descriptor.getUint32(4, true), (page.FrontierMediaCrypto.crcUpdate(0xffffffff, body) ^ 0xffffffff) >>> 0);
    assert.equal(Number(descriptor.getBigUint64(8, true)), size);
    recoveredCursor = start + size + 24;
}
assert.equal(new DataView(recoveredZip.buffer, recoveredCursor).getUint32(0, true), 0x02014b50);
assert.equal(new DataView(recoveredZip.buffer, recoveredZip.length - 98).getUint32(0, true), 0x06064b50);
assert.equal(Number(new DataView(recoveredZip.buffer, recoveredZip.length - 98).getBigUint64(24, true)), zipItems.length);
assert.equal((await workerFetch('/__fc_zip/' + survivorId)).status, 403, 'a consumed active archive remains one-shot');
assert.equal((await workerFetch('/__fc_zip/' + laterId)).status, 403, 'swept plans cannot be revived');
const currentArchive = await workerFetch('/__fc_zip/' + currentId);
assert.deepEqual(new Uint8Array(await currentArchive.arrayBuffer()), recoveredZip,
    'new admission after a full expired queue produces the same complete ZIP bytes');
assert.equal((await workerFetch('/__fc_zip/' + currentId)).status, 403);
assert.equal(calls.filter(call => call.url.endsWith('/session')).length, boundarySessions,
    'ZIP queue expiry and a slow active stream cannot renew the fixed key session');

await page.FrontierMediaCrypto.downloadPlan([{path: 'music/b.mp3', encryption: second.meta}]);
assert(!Object.hasOwn(downloadAnchors.at(-1), 'download'),
    'single encrypted attachment also reaches the worker through navigation');
assert.match(downloadAnchors.at(-1).href, /[?&]download=1/);
await page.FrontierMediaCrypto.downloadPlan([{path: 'lyrics/plain.lrc', url: '/plain-lyric', filename: 'plain.lrc'}]);
assert.equal(downloadAnchors.at(-1).download, 'plain.lrc', 'normal backend downloads retain their filename attribute');

const oldUrl = catalog[1].url;
const replacement = await prepare(new TextEncoder().encode('replaced'), 'music/b.mp3', second.source);
assert.equal(await page.FrontierMediaCrypto.textFor('music/b.mp3', replacement.meta.file_id), 'replaced',
    'a fresh lyric descriptor must replace a same-path cached key before its authorization expires');
advance(901000);
assert.equal((await workerFetch(oldUrl)).status, 409, 'same-path replacement cannot reuse another object key');
const fresh = [{media_path: 'music/b.mp3', encryption: replacement.meta}];
await page.FrontierMediaCrypto.prepareCatalog(fresh);
response = await workerFetch(fresh[0].url);
assert.equal(new TextDecoder().decode(await response.arrayBuffer()), 'replaced');

// Exercise text decoding through authenticated chunk playback, rather than
// passing already-decoded strings to the parser. A literal replacement code
// point is valid; invalid byte sequences cannot be silently replaced by it.
const encodedLyric = new TextEncoder().encode('\ufeff\ufeff[00:01]词\ufeff\n[00:02]�');
const lyricRecord = await prepare(encodedLyric, 'lyrics/utf8.lrc', 'https://cloud.example/relay/utf8-lyric');
const decodedLyric = await page.FrontierMediaCrypto.textFor('lyrics/utf8.lrc', lyricRecord.meta.file_id);
assert.equal(decodedLyric, '\ufeff\ufeff[00:01]词\ufeff\n[00:02]�', 'the decoder preserves the single-BOM parsing contract');
assert.deepEqual(JSON.parse(JSON.stringify(common.parseLyrics(decodedLyric))),
    [{time: 1, text: '\ufeff词\ufeff'}, {time: 2, text: '�'}]);
for (const [index, invalidBytes] of [
    [0xff], [0xe2, 0x82], [0xed, 0xa0, 0x80], [0xc0, 0xaf],
].entries()) {
    const invalidLyric = await prepare(Uint8Array.from(invalidBytes), 'lyrics/invalid-' + index + '.lrc',
        'https://cloud.example/relay/invalid-lyric-' + index);
    await assert.rejects(page.FrontierMediaCrypto.textFor('lyrics/invalid-' + index + '.lrc', invalidLyric.meta.file_id),
        error => error.name === 'TypeError', 'invalid UTF-8 must fail after successful authenticated decryption');
}

// A source or mid-transfer authorization limit must hold playback for an
// explicit user retry, rather than being classified as a resumable outage.
page.FrontierMediaCrypto.resetFault('music/a.mp3');
cipherStatus = 429;
response = await workerFetch(catalog[0].url);
await assert.rejects(response.arrayBuffer(), /存储节点/);
assert.match(page.FrontierMediaCrypto.failureFor(catalog[0].url), /限额/);
cipherStatus = 0;
page.FrontierMediaCrypto.resetFault('music/a.mp3');

replacement.ciphertext[0] ^= 1;
advance(300001);
response = await workerFetch(fresh[0].url);
await assert.rejects(response.arrayBuffer(), 'tampered ciphertext cannot return plaintext');
assert(page.FrontierMediaCrypto.failureFor(fresh[0].url), 'tag failures propagate to the playback retry guard');
assert.equal((await workerFetch(fresh[0].url)).status, 422, 'integrity failure must not be retried as a network failure');

advance(901000);
sessionStatus = 429;
const limitedSessionStart = calls.filter(call => call.url.endsWith('/session')).length;
const limitedCipherStart = cipherRequests.length;
const limitedKeysStart = calls.filter(call => call.url.endsWith('/key')).length;
const limitedRequests = await Promise.all([workerFetch(catalog[0].url), workerFetch(catalog[0].url)]);
for (const limited of limitedRequests) {
    assert.equal(limited.status, 429, 'an expired session preserves its failed handshake status');
    assert.equal(await limited.text(), 'temporary key session limit');
}
assert.equal(calls.filter(call => call.url.endsWith('/session')).length, limitedSessionStart + 1,
    'simultaneous requests coalesce one limited handshake without renewal loops');
assert.equal(calls.filter(call => call.url.endsWith('/key')).length, limitedKeysStart);
assert.equal(cipherRequests.length, limitedCipherStart, 'no ciphertext request can use an expired or denied session');
sessionStatus = 0;
for (const invalidLifetime of [0, 901, 1.5]) {
    sessionLifetime = invalidLifetime;
    await assert.rejects(page.FrontierMediaCrypto.encryptFile(inputFile(secondBytes, 'clock.mp3')),
        /会话期限无效/, 'invalid server lifetime cannot create a browser key authorization');
}
sessionLifetime = 899;
handshakeDelay = 900000;
await assert.rejects(page.FrontierMediaCrypto.encryptFile(inputFile(secondBytes, 'clock.mp3')),
    /授权已过期/, 'delayed handshake responses cannot restart the authorization lifetime');
handshakeDelay = 0;
const originalOrigin = performance.timeOrigin;
performance.timeOrigin = NaN;
assert.throws(() => common.monotonicNow(), /单调时钟不可用/, 'clock failure has no wall-clock fallback');
performance.timeOrigin = originalOrigin;
const guardedBytes = new TextEncoder().encode('cache authorization must stay current');
const guarded = await prepare(guardedBytes, 'music/cache-guard.mp3', 'https://cloud.example/relay/cache-guard');
const guardedCatalog = [{media_path: 'music/cache-guard.mp3', encryption: guarded.meta}];
await page.FrontierMediaCrypto.prepareCatalog(guardedCatalog);
response = await workerFetch(guardedCatalog[0].url);
assert.deepEqual(new Uint8Array(await response.arrayBuffer()), guardedBytes);
const beforeDeniedCache = cipherRequests.length;
keyStatus = 404;
response = await workerFetch(guardedCatalog[0].url);
await assert.rejects(response.arrayBuffer(), /current object access denied/,
    'a live cached envelope and warm ciphertext must not bypass hidden/deleted object authorization');
assert.equal(cipherRequests.length, beforeDeniedCache);
keyStatus = 0;
page.FrontierMediaCrypto.resetFault('music/cache-guard.mp3');
response = await workerFetch(guardedCatalog[0].url);
assert.deepEqual(new Uint8Array(await response.arrayBuffer()), guardedBytes);
await prepare(new TextEncoder().encode('different object'), 'music/cache-guard.mp3', guarded.source);
response = await workerFetch(guardedCatalog[0].url);
await assert.rejects(response.arrayBuffer(), /文件已替换/,
    'same-path replacement is rejected immediately even before the old envelope expires');

const legacy = await prepare(guardedBytes, 'music/old-page.mp3', 'https://cloud.example/relay/old-page');
const legacyCatalog = [{media_path: 'music/old-page.mp3', encryption: legacy.meta}];
await page.FrontierMediaCrypto.prepareCatalog(legacyCatalog);
response = await workerFetch(legacyCatalog[0].url);
await response.arrayBuffer();
const currentPageMessage = pageMessage;
pageMessage = event => {
    const port = event.ports[0], send = port.postMessage.bind(port);
    port.postMessage = value => { const previous = {...value}; delete previous.revalidated; send(previous); };
    currentPageMessage(event);
};
const beforeLegacy = cipherRequests.length;
response = await workerFetch(legacyCatalog[0].url);
assert.deepEqual(new Uint8Array(await response.arrayBuffer()), guardedBytes);
assert.equal(cipherRequests.length, beforeLegacy + 1,
    'a page without fresh-authorization acknowledgement must use normal source reads');
pageMessage = currentPageMessage;
vm.runInContext(`cipherChunks.clear(); cipherBytes = 0;
    for (let i = 0; i < 5; i++) rememberCipherChunk('bounded-' + i, new Uint8Array(8 * 1024 * 1024));`, worker);
assert.equal(vm.runInContext('cipherBytes', worker), 32 * 1024 * 1024);
assert.equal(vm.runInContext("cipherChunks.has('bounded-0')", worker), false, 'oldest ciphertext is evicted at the byte budget');
vm.runInContext("cachedCipherChunk('bounded-1'); rememberCipherChunk('bounded-5', new Uint8Array(8 * 1024 * 1024))", worker);
assert.equal(vm.runInContext("cipherChunks.has('bounded-1') && !cipherChunks.has('bounded-2')", worker), true,
    'cache hits protect recently used ciphertext from eviction');
advance(300001);
assert.equal(vm.runInContext("cachedCipherChunk('bounded-1')", worker), null, 'expired ciphertext must be fetched again');
assert.equal(vm.runInContext('cipherBytes', worker), 0);
console.log('media-crypto-smoke-ok (' + directory + ')');
