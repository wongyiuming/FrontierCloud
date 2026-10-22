import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';
const context = {window: {addEventListener() {}}, TextDecoder, Uint8Array, DataView, Blob, URL, crypto: webcrypto, AbortController, setTimeout, clearTimeout};
vm.runInNewContext(fs.readFileSync(new URL('../static/js/music-metadata.js', import.meta.url), 'utf8'), context);
const api = context.window.FrontierMusicMetadata;
const size = n => [n >> 21 & 127, n >> 14 & 127, n >> 7 & 127, n & 127];
const ascii = text => [...new TextEncoder().encode(text)];
const frame = (version, name, body) => [...ascii(name), ...(version === 2 ? [0, body.length >> 8, body.length & 255] : version === 3 ? [0, 0, body.length >> 8, body.length & 255, 0, 0] : [...size(body.length), 0, 0]), ...body];
const tag = (version, body) => Uint8Array.from([73, 68, 51, version, 0, 0, ...size(body.length), ...body]);
const image = [255, 216, 255, 224, ...Array(100).fill(0)];
for (const version of [2, 3, 4]) {
    const data = tag(version, [
        ...frame(version, version === 2 ? 'TT2' : 'TIT2', [1, 255, 254, 0x60, 0x4f, 0x7d, 0x59]),
        ...frame(version, version === 2 ? 'TP1' : 'TPE1', [3, ...ascii('Artist')]),
        ...frame(version, version === 2 ? 'TAL' : 'TALB', [3, ...ascii('Album')]),
        ...frame(version, version === 2 ? 'PIC' : 'APIC', [3, ...ascii(version === 2 ? 'JPG' : 'image/jpeg\0'), 3, 0, ...image]),
    ]);
    const result = api.parse(data);
    assert.equal(result.title, '\u4f60\u597d'); assert.equal(result.artist, 'Artist'); assert.equal(result.album, 'Album');
    assert.equal(result.picture.mime, 'image/jpeg'); assert.deepEqual(Array.from(result.picture.bytes), image);
}
for (const invalid of [new Uint8Array(), Uint8Array.from([73, 68, 51, 4, 0, 0, 127, 127, 127, 127]), tag(4, frame(4, 'APIC', [3, ...ascii('image/svg+xml\0'), 3, 0, ...ascii('<svg onload="bad()"></svg>')]))]) {
    assert.equal(api.parse(invalid).picture, undefined);
}
assert.equal(Object.keys(await api.metadata({media_path: 'music/plain.mp3'})).length, 0);
{
    const withIndicator = (name, body, indicator = size(body.length), unsync = false) => {
        const payload = unsync ? body.flatMap(byte => byte === 255 ? [255, 0] : [byte]) : body;
        const bytes = frame(4, name, [...indicator, ...payload]); bytes[9] = unsync ? 3 : 1; return bytes;
    };
    const title = [3, ...ascii('Indicator title')], art = [3, ...ascii('image/jpeg\0'), 3, 0, ...image];
    const result = api.parse(tag(4, [...withIndicator('TIT2', title), ...withIndicator('APIC', art, size(art.length), true)]));
    assert.equal(result.title, 'Indicator title');
    assert.deepEqual(Array.from(result.picture.bytes), image, 'DLI length describes the de-unsynchronized payload');
    for (const indicator of [[128, 0, 0, 0], size(title.length + 1)]) {
        assert.equal(api.parse(tag(4, withIndicator('TIT2', title, indicator))).title, undefined);
    }
}
{
    const requests = [];
    let invalidHeader;
    context.window.FrontierMediaCrypto = {prepareCatalog: async entries => { entries[0].url = '/authorized'; }};
    context.window.FrontierCryptoCommon = {validate() {}, boundedBytes: async response => response.bytes};
    context.fetch = async (_url, options) => {
        const range = options.headers.Range; requests.push(range);
        const tail = new Uint8Array(128); tail.set(ascii('TAGFooter title'), 0); tail.set(ascii('Footer artist'), 33); tail.set(ascii('Footer album'), 63);
        return {status: 206, headers: {get: () => `bytes ${range.slice(6)}/256`}, bytes: range === 'bytes=0-9' ? invalidHeader : tail};
    };
    for (const [index, length] of [[128, 0, 0, 0], size(9 * 1024 * 1024), size(300)].entries()) {
        invalidHeader = Uint8Array.from([73, 68, 51, 4, 0, 0, ...length]); requests.length = 0;
        const tags = await api.metadata({media_path: `music/bad-v2-${index}.mp3`, encryption: {plaintext_size: 256}});
        assert.equal(tags.title, 'Footer title'); assert.equal(tags.artist, 'Footer artist'); assert.equal(tags.album, 'Footer album');
        assert.deepEqual(requests, ['bytes=0-9', 'bytes=128-255'], 'invalid/oversized v2 must retain the bounded v1 fallback without fetching the tag');
    }
}
{
    let expire;
    context.setTimeout=(callback,delay)=>{assert.equal(delay,15000);expire=callback;return 1;};
    context.clearTimeout=()=>{};
    context.fetch=async (_url,options)=>new Promise((_resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('test slow Range aborted')),{once:true}));
    const entry={media_path:'music/slow-retry.mp3',encryption:{plaintext_size:256}};
    const pending=api.metadata(entry);
    await new Promise(setImmediate);
    assert.ok(expire,'Range request must own a bounded deadline');
    expire();await assert.rejects(pending,/slow Range aborted/);
    context.fetch=async (_url,options)=>({status:206,headers:{get:()=>`bytes ${options.headers.Range.slice(6)}/256`},bytes:new Uint8Array(options.headers.Range==='bytes=0-9'?10:128)});
    assert.deepEqual(Object.keys(await api.metadata(entry)),[],'timed-out metadata must remain retryable');
}
{
    context.window.location = {href:'https://media.test/album',origin:'https://media.test'};
    const data = tag(4,[...frame(4,'TIT2',[3,...ascii('S3 title')]),...frame(4,'TPE1',[3,...ascii('S3 artist')]),
        ...frame(4,'TALB',[3,...ascii('S3 album')]),...frame(4,'APIC',[3,...ascii('image/jpeg\0'),3,0,...image])]);
    const path='music/s3/song.mp3',identity='a'.repeat(64),requests=[];
    const entry={media_path:path,resource_id:identity,browser_metadata:true,size_bytes:data.length,etag:'version-1',
        url:'/api/v1/media/stream?'+new URLSearchParams({file_path:path,resource_id:identity})};
    context.window.FrontierMediaCrypto = {prepareCatalog:async()=>{throw new Error('plaintext must not request a key');}};
    context.window.FrontierCryptoCommon = {validate(){throw new Error('plaintext descriptor forbidden');},boundedBytes:async r=>r.bytes};
    context.setTimeout=setTimeout;context.clearTimeout=clearTimeout;
    context.fetch=async(url,options)=>{
        assert.equal(url,entry.url);assert.equal(options.priority,'low');
        const [start,end]=options.headers.Range.slice(6).split('-').map(Number);requests.push([start,end]);
        return {status:206,headers:{get:()=>`bytes ${start}-${end}/${data.length}`},bytes:data.slice(start,end+1)};
    };
    await api.decorate([entry]);
    assert.equal(entry.title,'S3 title');assert.equal(entry.artist,'S3 artist');assert.ok(entry.cover.startsWith('blob:'));
    assert.deepEqual(requests,[[0,9],[10,data.length-1]],'plaintext S3 tags use only bounded browser Range reads');
    await api.metadata(entry);assert.equal(requests.length,2,'unchanged logical object reuses tag metadata');
    await assert.rejects(api.metadata({...entry,url:'https://other.test/file',etag:'wrong-origin'}),/来源无效/);
    assert.equal(requests.length,2,'invalid sources never initiate requests');
    context.window.FrontierMediaCrypto = {prepareCatalog:async entries=>{entries[0].url='/authorized';}};
    context.window.FrontierCryptoCommon = {validate(){},boundedBytes:async r=>r.bytes};
}
{
    context.window.location = {href: 'https://media.test/album', origin: 'https://media.test'};
    context.window.requestIdleCallback = callback => setImmediate(() => callback({timeRemaining: () => 20}));
    context.window.cancelIdleCallback = clearImmediate;
    context.setTimeout = setTimeout; context.clearTimeout = clearTimeout;
    let reads = 0, closes = 0, dimensions;
    context.fetch = async (_url, options) => {
        reads++;
        assert.equal(options.cache, 'only-if-cached', 'small artwork cannot initiate an original network download');
        assert.equal(options.mode, 'same-origin'); assert.equal(options.priority, 'low');
        return new Response(Uint8Array.from(image));
    };
    context.createImageBitmap = async () => ({width: 1000, height: 500, close() { closes++; }});
    context.OffscreenCanvas = class {
        constructor(width, height) { this.width = width; this.height = height; dimensions = [width, height]; }
        getContext() { return {drawImage() {}}; }
        async convertToBlob(options) { assert.equal(options.type, 'image/webp'); return new Blob(['thumbnail'], {type: options.type}); }
    };
    const [one, two] = await Promise.all([api.thumbnail('/api/v1/media/cover?one'), api.thumbnail('/api/v1/media/cover?one')]);
    assert.equal(one, two); assert.equal(reads, 1, 'concurrent thumbnail views share one read');
    assert.deepEqual(dimensions, [96, 48], 'queue artwork preserves aspect ratio without upscaling');
    assert.equal(closes, 1, 'decoded originals are released after rendering');
    assert.equal((await (await fetch(one)).blob()).type, 'image/webp');
    assert.equal(await api.thumbnail('/api/v1/media/cover?deferred', {admit: () => false}), null);
    assert.ok((await api.thumbnail('/api/v1/media/cover?deferred')).startsWith('blob:'), 'deferred admission is retryable when playback has spare buffer');
    context.fetch = async () => new Response('', {status: 504});
    await assert.rejects(api.thumbnail('/api/v1/media/cover?cold-cache'), /Artwork unavailable/);
    const controller = new AbortController();
    let idle, idleCancelled = false;
    context.window.requestIdleCallback = callback => {idle = callback; return 1;};
    context.window.cancelIdleCallback = () => {idleCancelled = true;};
    const cancelled = api.thumbnail('/api/v1/media/cover?cancelled', {signal: controller.signal});
    await new Promise(setImmediate);
    controller.abort();
    await assert.rejects(cancelled, /Artwork cancelled/);
    assert.equal(idleCancelled, true, 'buffering cancellation removes the queued idle task');
    context.window.requestIdleCallback = callback => setImmediate(() => callback({timeRemaining: () => 20}));
    context.window.cancelIdleCallback = clearImmediate;
    await assert.rejects(api.thumbnail('https://foreign.test/cover'), /Unsupported artwork source/);
    context.fetch = async () => new Response(new Uint8Array(4 * 1024 * 1024 + 1));
    await assert.rejects(api.thumbnail('/api/v1/media/cover?oversize'), /exceeds budget/);
    context.fetch = async () => new Response(Uint8Array.from(image));
    assert.ok((await api.thumbnail('/api/v1/media/cover?oversize')).startsWith('blob:'), 'failed thumbnails remain retryable');
    let thumbnailReads = 0, metadataReads = 0;
    const releases = [];
    context.fetch = async (url, options) => {
        if (String(url).includes('/api/v1/media/cover?reserved')) {
            thumbnailReads++;
            return new Promise(resolve => releases.push(() => resolve(new Response(Uint8Array.from(image)))));
        }
        metadataReads++;
        const range = options.headers.Range;
        return {status: 206, headers: {get: () => `bytes ${range.slice(6)}/256`}, bytes: new Uint8Array(range === 'bytes=0-9' ? 10 : 128)};
    };
    const first = api.thumbnail('/api/v1/media/cover?reserved-one');
    const second = api.thumbnail('/api/v1/media/cover?reserved-two');
    const queuedController = new AbortController();
    const abandoned = api.thumbnail('/api/v1/media/cover?reserved-abandoned', {signal: queuedController.signal});
    await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(thumbnailReads, 1, 'thumbnail reads have exactly one independent active slot');
    queuedController.abort();
    await assert.rejects(abandoned, /Artwork cancelled/, 'queued cancellation settles before the active predecessor finishes');
    const selected = api.metadata({media_path: 'music/reserved-selection.mp3', encryption: {plaintext_size: 256}}, {priority: true});
    await new Promise(setImmediate);
    assert.equal(metadataReads, 2, 'a slow thumbnail must not consume selected metadata capacity');
    await selected;
    releases.shift()(); await first; await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(thumbnailReads, 2);
    releases.shift()(); await second;
    const deadlines = [];
    context.setTimeout = (callback, delay) => {assert.equal(delay, 2000); deadlines.push(callback); return deadlines.length;};
    context.clearTimeout = () => {};
    const stale = api.thumbnail('/api/v1/media/cover?expired-queue');
    deadlines[0]();
    await assert.rejects(stale, /Artwork cancelled/, 'the total lifetime includes queue waiting time');
    context.setTimeout = setTimeout; context.clearTimeout = clearTimeout;
}
for (const canvas of [undefined, class {getContext() { return {}; }}, class {getContext() { return null; } convertToBlob() {}}]) {
    const legacy = {window: {addEventListener() {}, location: {href: 'https://media.test/', origin: 'https://media.test'}},
        URL, Blob, OffscreenCanvas: canvas, createImageBitmap: () => {}, TextDecoder, Uint8Array, DataView};
    vm.runInNewContext(fs.readFileSync(new URL('../static/js/music-metadata.js', import.meta.url), 'utf8'), legacy);
    assert.equal(legacy.window.FrontierMusicMetadata.canThumbnail(), false, 'missing canvas/encoder/2D context skips optional artwork');
    assert.equal(await legacy.window.FrontierMusicMetadata.thumbnail('/api/v1/media/cover?legacy'), null);
}
console.log('music-metadata-smoke-ok: ID3, bounded retry and isolated small-artwork rendering');
