'use strict';

importScripts('/static/js/media-crypto-common.js');
const common = self.FrontierCryptoCommon;
const capabilities = new Map();
const grants = new Map();
const pendingGrants = new Map();
const zipPlans = new Map();
const PREFIX = '/__fc_media/';

function revokeCapability(token) {
    capabilities.delete(token);
    for (const key of grants.keys()) if (key.startsWith(token + ':')) grants.delete(key);
    for (const [id, plan] of zipPlans) if (plan.token === token) zipPlans.delete(id);
}

async function registerCapability(event, data) {
    if (capabilities.size >= 256 && !capabilities.has(data.token)) {
        // A renderer crash need not send pagehide. Reclaim only capabilities
        // whose owning client is gone, and retain concurrent replacements.
        await Promise.all([...capabilities].map(async ([token, capability]) => {
            if (!await self.clients.get(capability.clientId)
                && capabilities.get(token) === capability) revokeCapability(token);
        }));
    }
    if (capabilities.size >= 256 && !capabilities.has(data.token)) {
        event.ports[0]?.postMessage({error: '加密播放会话过多，请关闭闲置页面'});
        return;
    }
    capabilities.set(data.token, {clientId: event.source.id});
    event.ports[0]?.postMessage({ok: true});
}

self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('message', event => {
    const data = event.data || {};
    if (!event.source?.id || !/^[a-f0-9]{32}$/.test(data.token || '')) return;
    if (data.type === 'fc-crypto-register') {
        const registration = registerCapability(event, data).catch(() => {
            event.ports[0]?.postMessage({error: '加密播放会话检查失败，请重试'});
        });
        event.waitUntil?.(registration);
    } else if (data.type === 'fc-crypto-invalidate'
        && capabilities.get(data.token)?.clientId === event.source.id) {
        for (const key of grants.keys()) if (key.startsWith(data.token + ':' + data.file_path + ':')) grants.delete(key);
    } else if (data.type === 'fc-crypto-zip'
        && capabilities.get(data.token)?.clientId === event.source.id) {
        const now = common.monotonicNow();
        for (const [id, plan] of zipPlans) if (plan.expires < now) zipPlans.delete(id);
        if (!Array.isArray(data.items) || data.items.length > 5000 || zipPlans.size >= 8) {
            event.ports[0]?.postMessage({error: '下载任务过多，请分批下载'});
            return;
        }
        zipPlans.set(data.download_token, {token: data.token, items: data.items, expires: now + 120000});
        event.ports[0]?.postMessage({ok: true});
    } else if (data.type === 'fc-crypto-revoke'
        && capabilities.get(data.token)?.clientId === event.source.id) {
        revokeCapability(data.token);
    }
});

async function requestGrant(token, filePath, expectedFileId = '', fallbackClientId = '') {
    const capability = capabilities.get(token);
    const client = await self.clients.get(capability?.clientId || fallbackClientId);
    if (!client) {
        capabilities.delete(token);
        const error = new Error('解密会话已失效，请刷新播放页面');
        error.status = 403;
        throw error;
    }
    return new Promise((resolve, reject) => {
        const channel = new MessageChannel();
        const timer = setTimeout(() => {
            channel.port1.close();
            reject(new Error('浏览器解密授权超时'));
        }, 20000);
        channel.port1.onmessage = event => {
            clearTimeout(timer);
            channel.port1.close();
            if (event.data?.error) {
                const error = new Error(event.data.error);
                error.status = event.data.status;
                reject(error);
            }
            else resolve(event.data);
        };
        client.postMessage({type: 'fc-crypto-grant', token, file_path: filePath, file_id: expectedFileId}, [channel.port2]);
    });
}

async function getGrant(token, filePath, expectedFileId = '', fallbackClientId = '') {
    const identity = token + ':' + filePath + ':' + expectedFileId;
    const cached = grants.get(identity);
    if (cached?.failed) {
        const error = new Error('加密媒体完整性校验失败，请重新选择文件');
        error.status = 422;
        throw error;
    }
    if (cached && cached.deadline > common.monotonicNow() + 10000) return cached;
    if (pendingGrants.has(identity)) return pendingGrants.get(identity);
    const pending = requestGrant(token, filePath, expectedFileId, fallbackClientId).then(grant => {
        common.validate(grant.encryption);
        if (!(grant.key instanceof CryptoKey) || !grant.source_url || !Number.isFinite(grant.deadline)
            || grant.deadline <= common.monotonicNow() || grant.deadline > common.monotonicNow() + 900000) {
            const error = new Error('解密授权已失效');
            error.status = 403;
            throw error;
        }
        // The worker is a transient byte adapter. Keys are never written to
        // CacheStorage, IndexedDB, cookies or local/session storage.
        if (grants.size >= 32) grants.delete(grants.keys().next().value);
        if (!capabilities.has(token) && fallbackClientId) capabilities.set(token, {clientId: fallbackClientId});
        grants.set(identity, grant);
        return grant;
    }).finally(() => pendingGrants.delete(identity));
    pendingGrants.set(identity, pending);
    return pending;
}

async function readChunk(grant, index, signal) {
    const expected = common.cipherRange(grant.encryption, index);
    const response = await fetch(grant.source_url, {
        headers: {Range: `bytes=${expected.start}-${expected.end}`},
        credentials: 'same-origin', cache: 'no-store', signal,
    });
    const contentRange = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(response.headers.get('Content-Range') || '');
    const exactRange = response.status === 206 && contentRange
        && Number(contentRange[1]) === expected.start && Number(contentRange[2]) === expected.end
        && Number(contentRange[3]) === grant.encryption.ciphertext_size;
    const completeSmallObject = response.status === 200 && expected.start === 0
        && expected.size === grant.encryption.ciphertext_size;
    if (!exactRange && !completeSmallObject) {
        await response.body?.cancel().catch(() => {});
        const error = new Error('存储节点未返回所需的加密分块');
        if (response.status >= 400 && response.status < 500) error.status = response.status;
        else if (response.status < 400) { error.fatal = true; error.status = 409; }
        throw error;
    }
    const ciphertext = await common.boundedBytes(response, expected.size);
    // AES-GCM verifies each entire chunk before any of its bytes are delivered.
    try {
        return new Uint8Array(await crypto.subtle.decrypt(
            common.parameters(grant.encryption, index), grant.key, ciphertext));
    } catch (error) {
        error.fatal = true;
        throw error;
    }
}

async function servePlaintext(request, token, filePath, download, expectedFileId = '', fallbackClientId = '') {
    const grant = await getGrant(token, filePath, expectedFileId, fallbackClientId);
    const size = grant.encryption.plaintext_size;
    const selected = common.range(request.headers.get('Range'), size);
    if (!selected) return new Response(null, {status: 416,
        headers: {'Content-Range': `bytes */${size}`, 'Cache-Control': 'no-store'}});
    const headers = new Headers({
        'Content-Type': grant.content_type || 'application/octet-stream',
        'Content-Length': String(Math.max(0, selected.end - selected.start + 1)),
        'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store',
        'X-Content-Type-Options': 'nosniff',
        'ETag': '"fc-plain-' + grant.encryption.file_id + '"',
    });
    if (selected.partial) headers.set('Content-Range', `bytes ${selected.start}-${selected.end}/${size}`);
    if (download) headers.set('Content-Disposition', `attachment; filename*=UTF-8''${encodeURIComponent(grant.filename || filePath.split('/').pop())}`);
    if (request.method === 'HEAD' || size === 0) return new Response(null,
        {status: selected.partial ? 206 : 200, headers});
    let index = Math.floor(selected.start / common.CHUNK_SIZE);
    const last = Math.floor(selected.end / common.CHUNK_SIZE);
    const controller = new AbortController();
    request.signal.addEventListener('abort', () => controller.abort(), {once: true});
    if (request.signal.aborted) controller.abort();
    const stream = new ReadableStream({
        async pull(output) {
            try {
                if (index > last) { output.close(); return; }
                if (grant.deadline <= common.monotonicNow() + 10000) {
                    const refreshed = await getGrant(token, filePath, expectedFileId, fallbackClientId);
                    if (refreshed.encryption.file_id !== grant.encryption.file_id)
                        throw new Error('播放文件已变化，请重新选择');
                    Object.assign(grant, refreshed);
                }
                const plaintext = await readChunk(grant, index, controller.signal);
                const chunkStart = index * common.CHUNK_SIZE;
                output.enqueue(plaintext.subarray(Math.max(0, selected.start - chunkStart),
                    Math.min(plaintext.length, selected.end - chunkStart + 1)));
                index += 1;
                if (index > last) output.close();
            } catch (error) {
                if (error.fatal || [401, 403, 404, 409, 422, 429].includes(error.status)) {
                    grant.failed = true;
                    const client = await self.clients.get(capabilities.get(token)?.clientId || '');
                    client?.postMessage({type: 'fc-crypto-fault', token, file_path: filePath,
                        error: error.status === 429 ? '媒体授权请求已达到限额，请稍后重新选择文件'
                            : '加密媒体授权或完整性校验失败，请重新选择文件'});
                }
                controller.abort(); output.error(error);
            }
        },
        cancel() { controller.abort(); },
    }, {highWaterMark: 0});
    return new Response(stream, {status: selected.partial ? 206 : 200, headers});
}

self.addEventListener('fetch', event => {
    const url = new URL(event.request.url);
    if (url.origin !== self.location.origin || !url.pathname.startsWith(PREFIX)) return;
    event.respondWith((async () => {
        if (!['GET', 'HEAD'].includes(event.request.method)) return new Response(null, {status: 405});
        const parts = url.pathname.slice(PREFIX.length).split('/');
        const token = parts.shift();
        if (!/^[a-f0-9]{32}$/.test(token || '')) return new Response('解密会话已失效', {status: 403});
        try {
            const filePath = decodeURIComponent(parts.join('/'));
            return await servePlaintext(event.request, token, filePath, url.searchParams.has('download'),
                url.searchParams.get('object') || '', event.clientId);
        } catch (error) {
            return new Response(error.message || '加密媒体暂不可用', {status: error.status || 502,
                headers: {'Cache-Control': 'no-store', 'Content-Type': 'text/plain; charset=utf-8'}});
        }
    })());
});

const crcTable = Uint32Array.from({length: 256}, (_, value) => {
    let crc = value;
    for (let bit = 0; bit < 8; bit += 1) crc = (crc & 1) ? 0xedb88320 ^ (crc >>> 1) : crc >>> 1;
    return crc >>> 0;
});
function crcUpdate(crc, bytes) {
    for (const byte of bytes) crc = crcTable[(crc ^ byte) & 255] ^ (crc >>> 8);
    return crc;
}
function zipHeader(size, signature) {
    const bytes = new Uint8Array(size);
    const view = new DataView(bytes.buffer);
    view.setUint32(0, signature, true);
    return {bytes, view};
}
async function* archive(plan, signal) {
    const central = [];
    let offset = 0;
    for (const item of plan.items) {
        const name = new TextEncoder().encode(item.path || item.filename);
        if (!name.length || name.length > 65535 || !Number.isSafeInteger(item.plaintext_size)
            || item.plaintext_size < 0) throw new Error('下载文件描述无效');
        const start = offset;
        const local = zipHeader(30 + name.length + 20, 0x04034b50);
        local.view.setUint16(4, 45, true);
        local.view.setUint16(6, 0x0808, true); // UTF-8 + trailing data descriptor.
        local.view.setUint32(18, 0xffffffff, true);
        local.view.setUint32(22, 0xffffffff, true);
        local.view.setUint16(26, name.length, true);
        local.view.setUint16(28, 20, true);
        local.bytes.set(name, 30);
        const extra = 30 + name.length;
        local.view.setUint16(extra, 1, true);
        local.view.setUint16(extra + 2, 16, true);
        local.view.setBigUint64(extra + 4, BigInt(item.plaintext_size), true);
        local.view.setBigUint64(extra + 12, BigInt(item.plaintext_size), true);
        yield local.bytes;
        offset += local.bytes.length;
        const response = item.encryption
            ? await servePlaintext(new Request(new URL(item.url, self.location.origin), {signal}),
                plan.token, item.file_path, false, item.encryption.file_id)
            : await fetch(item.url, {credentials: 'same-origin', cache: 'no-store', signal});
        if (!response.ok || !response.body) throw new Error('下载文件读取失败');
        const reader = response.body.getReader();
        let size = 0;
        let crc = 0xffffffff;
        try {
            while (true) {
                const {done, value} = await reader.read();
                if (done) break;
                size += value.byteLength;
                if (size > item.plaintext_size) throw new Error('下载文件长度发生变化');
                crc = crcUpdate(crc, value);
                yield value;
                offset += value.byteLength;
            }
        } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
        if (size !== item.plaintext_size) throw new Error('下载文件不完整');
        crc = (crc ^ 0xffffffff) >>> 0;
        const descriptor = zipHeader(24, 0x08074b50);
        descriptor.view.setUint32(4, crc, true);
        descriptor.view.setBigUint64(8, BigInt(size), true);
        descriptor.view.setBigUint64(16, BigInt(size), true);
        yield descriptor.bytes;
        offset += descriptor.bytes.length;
        const entry = zipHeader(46 + name.length + 28, 0x02014b50);
        entry.view.setUint16(4, 45, true);
        entry.view.setUint16(6, 45, true);
        entry.view.setUint16(8, 0x0808, true);
        entry.view.setUint32(16, crc, true);
        entry.view.setUint32(20, 0xffffffff, true);
        entry.view.setUint32(24, 0xffffffff, true);
        entry.view.setUint16(28, name.length, true);
        entry.view.setUint16(30, 28, true);
        entry.view.setUint32(42, 0xffffffff, true);
        entry.bytes.set(name, 46);
        const centralExtra = 46 + name.length;
        entry.view.setUint16(centralExtra, 1, true);
        entry.view.setUint16(centralExtra + 2, 24, true);
        entry.view.setBigUint64(centralExtra + 4, BigInt(size), true);
        entry.view.setBigUint64(centralExtra + 12, BigInt(size), true);
        entry.view.setBigUint64(centralExtra + 20, BigInt(start), true);
        central.push(entry.bytes);
    }
    const centralOffset = offset;
    for (const bytes of central) { yield bytes; offset += bytes.length; }
    const centralSize = offset - centralOffset;
    const end = zipHeader(56, 0x06064b50);
    end.view.setBigUint64(4, 44n, true);
    end.view.setUint16(12, 45, true);
    end.view.setUint16(14, 45, true);
    end.view.setBigUint64(24, BigInt(central.length), true);
    end.view.setBigUint64(32, BigInt(central.length), true);
    end.view.setBigUint64(40, BigInt(centralSize), true);
    end.view.setBigUint64(48, BigInt(centralOffset), true);
    yield end.bytes;
    const locator = zipHeader(20, 0x07064b50);
    locator.view.setBigUint64(8, BigInt(offset), true);
    locator.view.setUint32(16, 1, true);
    yield locator.bytes;
    const legacyEnd = zipHeader(22, 0x06054b50);
    legacyEnd.view.setUint16(8, 0xffff, true);
    legacyEnd.view.setUint16(10, 0xffff, true);
    legacyEnd.view.setUint32(12, 0xffffffff, true);
    legacyEnd.view.setUint32(16, 0xffffffff, true);
    yield legacyEnd.bytes;
}
self.addEventListener('fetch', event => {
    const url = new URL(event.request.url);
    if (url.origin !== self.location.origin || !url.pathname.startsWith('/__fc_zip/')) return;
    event.respondWith((async () => {
        const id = url.pathname.slice('/__fc_zip/'.length);
        const plan = zipPlans.get(id);
        if (!plan || plan.expires < common.monotonicNow() || !capabilities.has(plan.token))
            return new Response('下载授权已失效', {status: 403});
        zipPlans.delete(id);
        const controller = new AbortController();
        event.request.signal.addEventListener('abort', () => controller.abort(), {once: true});
        const iterator = archive(plan, controller.signal);
        const stream = new ReadableStream({
            async pull(output) {
                try {
                    const {value, done} = await iterator.next();
                    if (done) output.close(); else output.enqueue(value);
                } catch (error) { controller.abort(); output.error(error); }
            },
            async cancel() { controller.abort(); await iterator.return(); },
        }, {highWaterMark: 0});
        return new Response(stream, {headers: {'Content-Type': 'application/zip',
            'Content-Disposition': 'attachment; filename="media-download.zip"', 'Cache-Control': 'no-store'}});
    })());
});
