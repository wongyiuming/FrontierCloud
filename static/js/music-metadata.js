(() => {
    'use strict';
    const TAG_LIMIT = 8 * 1024 * 1024, IMAGE_LIMIT = 4 * 1024 * 1024, IMAGE_BUDGET = 32 * 1024 * 1024;
    const cache = new Map(), pictures = new Map(), pictureBlobs = new Map();
    const thumbnails = new Map();
    let thumbnailTail = Promise.resolve(), thumbnailCapability;
    let imageBytes = 0, active = 0;
    const queue = [];
    const ascii = bytes => String.fromCharCode(...bytes);
    function syncSafe(bytes) {
        if (bytes.length !== 4 || bytes.some(byte => byte > 127)) return -1;
        return bytes.reduce((n, byte) => n * 128 + byte, 0);
    }
    function unSync(bytes) {
        const out = [];
        for (let i = 0; i < bytes.length; i++) { out.push(bytes[i]); if (bytes[i] === 255 && bytes[i + 1] === 0) i++; }
        return Uint8Array.from(out);
    }
    function text(bytes, encoding = bytes[0], offset = 1) {
        if (encoding > 3 || bytes.length < offset) return '';
        try {
            const label = ['windows-1252', 'utf-16le', 'utf-16be', 'utf-8'][encoding];
            let data = bytes.subarray(offset), decoder = label;
            if (encoding === 1 && data[0] === 254 && data[1] === 255) { decoder = 'utf-16be'; data = data.subarray(2); }
            return new TextDecoder(decoder).decode(data).replace(/\0.*$/s, '').trim().slice(0, 4096);
        } catch (_) { return ''; }
    }
    function terminator(bytes, offset, wide) {
        for (let i = offset; i < bytes.length; i += wide ? 2 : 1) {
            if (bytes[i] === 0 && (!wide || bytes[i + 1] === 0)) return i + (wide ? 2 : 1);
        }
        return -1;
    }
    function picture(bytes, version) {
        if (bytes.length < 6 || bytes[0] > 3) return null;
        let offset = version === 2 ? 4 : terminator(bytes, 1, false);
        if (offset < 0 || offset >= bytes.length) return null;
        const type = bytes[offset++];
        offset = terminator(bytes, offset, bytes[0] === 1 || bytes[0] === 2);
        if (offset < 0) return null;
        const image = bytes.subarray(offset);
        if (image.length > IMAGE_LIMIT || image.length < 12) return null;
        let mime = '';
        if (image[0] === 255 && image[1] === 216 && image[2] === 255) mime = 'image/jpeg';
        else if (ascii(image.subarray(0, 8)) === '\x89PNG\r\n\x1a\n') mime = 'image/png';
        else if (['GIF87a', 'GIF89a'].includes(ascii(image.subarray(0, 6)))) mime = 'image/gif';
        else if (ascii(image.subarray(0, 4)) === 'RIFF' && ascii(image.subarray(8, 12)) === 'WEBP') mime = 'image/webp';
        return mime ? {bytes: image.slice(), mime, front: type === 3} : null;
    }
    function parse(bytes) {
        const result = {};
        if (bytes.length < 10 || ascii(bytes.subarray(0, 3)) !== 'ID3') return result;
        const version = bytes[3], size = syncSafe(bytes.subarray(6, 10));
        if (version < 2 || version > 4 || size < 0 || size > TAG_LIMIT || bytes.length < size + 10) return result;
        // ID3v2.2's compression format is not a tag frame stream.
        if (version === 2 && (bytes[5] & 64)) return result;
        let data = bytes.subarray(10, size + 10);
        if (version < 4 && (bytes[5] & 128)) data = unSync(data);
        if (version >= 3 && (bytes[5] & 64)) {
            if (data.length < 4) return result;
            const skip = version === 4 ? syncSafe(data.subarray(0, 4)) : new DataView(data.buffer, data.byteOffset, 4).getUint32(0) + 4;
            if (skip < 4 || skip > data.length) return result;
            data = data.subarray(skip);
        }
        while (data.length >= (version === 2 ? 6 : 10)) {
            const width = version === 2 ? 6 : 10, name = ascii(data.subarray(0, version === 2 ? 3 : 4));
            if (!/^[A-Z0-9]+$/.test(name)) break;
            const length = version === 2 ? data[3] * 65536 + data[4] * 256 + data[5]
                : version === 4 ? syncSafe(data.subarray(4, 8)) : new DataView(data.buffer, data.byteOffset + 4, 4).getUint32(0);
            if (length <= 0 || length > data.length - width) break;
            const flags = version === 2 ? 0 : data[9];
            let body = data.subarray(width, width + length); data = data.subarray(width + length);
            if ((version === 3 && (flags & 224)) || (version === 4 && (flags & 76))) continue;
            if (version === 4 && ((flags & 2) || (bytes[5] & 128))) body = unSync(body);
            if (version === 4 && (flags & 1)) {
                if (body.length < 4 || syncSafe(body.subarray(0, 4)) !== body.length - 4) continue;
                body = body.subarray(4);
            }
            const field = {TIT2: 'title', TT2: 'title', TPE1: 'artist', TP1: 'artist', TALB: 'album', TAL: 'album'}[name];
            if (field) result[field] = text(body);
            if (name === 'APIC' || name === 'PIC') {
                const candidate = picture(body, version);
                if (candidate && (!result.picture || candidate.front)) result.picture = candidate;
            }
        }
        return result;
    }
    function limited(work, priority = false) {
        return new Promise((resolve, reject) => {
            const job = {work, resolve, reject};
            if (priority) queue.unshift(job); else queue.push(job);
            pump();
        });
    }
    function pump() {
        while (active < 2 && queue.length) {
            const job = queue.shift(); active++;
            Promise.resolve().then(job.work).then(job.resolve, job.reject).finally(() => { active--; pump(); });
        }
    }
    async function range(url, start, end, size, signal) {
        const controller = new AbortController();
        const abort = () => controller.abort();
        if (signal?.aborted) controller.abort();
        signal?.addEventListener('abort', abort, {once: true});
        const timeout = setTimeout(() => controller.abort(), 15000);
        try {
            const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', priority: 'low', signal: controller.signal, headers: {Range: `bytes=${start}-${end}`}});
            if (response.status !== 206 || response.headers.get('Content-Range') !== `bytes ${start}-${end}/${size}`) {
                await response.body?.cancel(); throw new Error('音乐标签分段读取失败');
            }
            return await window.FrontierCryptoCommon.boundedBytes(response, end - start + 1);
        } finally {
            clearTimeout(timeout);
            signal?.removeEventListener('abort', abort);
        }
    }
    async function load(entry, signal) {
        signal?.throwIfAborted();
        if (!window.FrontierMediaCrypto || !window.FrontierCryptoCommon) throw new Error('浏览器解密组件未加载');
        const source = {...entry};
        let size;
        if (entry.encryption) {
            window.FrontierCryptoCommon.validate(entry.encryption);
            size = entry.encryption.plaintext_size;
            await window.FrontierMediaCrypto.prepareCatalog([source]);
        } else {
            size = entry.size_bytes;
            const url = new URL(entry.url, window.location.href);
            if (entry.browser_metadata !== true || !Number.isSafeInteger(size) || size < 1 || size > 10 * 1024 ** 3
                || url.origin !== window.location.origin || url.pathname !== '/api/v1/media/stream'
                || url.searchParams.get('resource_id') !== String(entry.resource_id || entry.media_id)
                || url.searchParams.get('file_path') !== (entry.media_path || entry.path))
                throw new Error('音乐标签来源无效');
        }
        if (size < 10) return {};
        const header = await range(source.url, 0, 9, size, signal);
        let result = {};
        if (ascii(header.subarray(0, 3)) === 'ID3') {
            const length = syncSafe(header.subarray(6, 10));
            if (length > 0 && length <= TAG_LIMIT && length <= size - 10) {
                const body = await range(source.url, 10, length + 9, size, signal);
                const bytes = new Uint8Array(length + 10); bytes.set(header); bytes.set(body, 10); result = parse(bytes);
            }
        }
        if (size >= 128 && (!result.title || !result.artist || !result.album)) {
            const tail = await range(source.url, size - 128, size - 1, size, signal);
            if (ascii(tail.subarray(0, 3)) === 'TAG') {
                result.title ||= text(tail.subarray(3, 33), 0, 0);
                result.artist ||= text(tail.subarray(33, 63), 0, 0);
                result.album ||= text(tail.subarray(63, 93), 0, 0);
            }
        }
        if (result.picture) {
            const image = result.picture;
            const digest = await crypto.subtle.digest('SHA-256', image.bytes);
            const identity = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
            if (!pictures.has(identity) && imageBytes + image.bytes.length <= IMAGE_BUDGET) {
                const blob = new Blob([image.bytes], {type: image.mime}), url = URL.createObjectURL(blob);
                pictures.set(identity, url); pictureBlobs.set(url, blob); imageBytes += image.bytes.length;
            }
            result.cover = pictures.get(identity); delete result.picture;
        }
        return result;
    }
    function metadata(entry, {priority = false, signal} = {}) {
        if ((!entry?.encryption && entry?.browser_metadata !== true) || !/\.mp3$/i.test(entry.media_path || entry.path || '')) return Promise.resolve({});
        // File ID, descriptor and logical identity prevent reuse after replacement.
        const key = JSON.stringify([entry.media_path || entry.path, entry.resource_id || entry.media_id, entry.encryption,
            entry.browser_metadata === true ? [entry.size_bytes, entry.etag] : null]);
        if (!cache.has(key)) {
            if (cache.size >= 128) cache.delete(cache.keys().next().value);
            const promise = limited(() => load(entry, signal), priority).catch(error => { cache.delete(key); throw error; });
            cache.set(key, promise);
        }
        return cache.get(key);
    }
    async function decorate(entries, onUpdate = () => {}) {
        await Promise.all(entries.map(async entry => {
            if (!entry?.encryption && entry?.browser_metadata !== true) return;
            try {
                const tags = await metadata(entry);
                for (const field of ['title', 'artist', 'album', 'cover']) if (tags[field]) entry[field] = tags[field];
                if (Object.keys(tags).length) onUpdate(entry);
            } catch (_) { /* Metadata failure keeps existing display and playback. */ }
        }));
    }
    // Only the audio player's small queue artwork opts into this browser-side view.
    // Originals and encrypted source bytes are never rewritten or uploaded.
    function canThumbnail() {
        if (thumbnailCapability === undefined) {
            try {
                const canvas = typeof OffscreenCanvas === 'function' ? new OffscreenCanvas(1, 1) : null;
                thumbnailCapability = typeof createImageBitmap === 'function'
                    && typeof canvas?.convertToBlob === 'function' && Boolean(canvas.getContext('2d'));
            } catch (_) { thumbnailCapability = false; }
        }
        return thumbnailCapability;
    }
    function thumbnail(source, {signal, admit = () => true} = {}) {
        const url = new URL(source, window.location.href);
        if (url.origin !== window.location.origin || !['blob:', 'http:', 'https:'].includes(url.protocol)) {
            return Promise.reject(new Error('Unsupported artwork source'));
        }
        if (thumbnails.has(source)) return thumbnails.get(source);
        if (thumbnails.size >= 128 || !canThumbnail() || typeof window.requestIdleCallback !== 'function') {
            return Promise.resolve(null);
        }
        // A separate serial channel cannot consume either encrypted metadata slot.
        const controller = new AbortController();
        const abort = () => controller.abort();
        if (signal?.aborted) controller.abort();
        signal?.addEventListener('abort', abort, {once: true});
        // Includes queue age: old work expires while waiting, not after all predecessors finish.
        const timeout = setTimeout(abort, 2000);
        let rejectCancelled;
        const cancelled = new Promise((_resolve, reject) => {
            rejectCancelled = () => reject(new Error('Artwork cancelled'));
            controller.signal.addEventListener('abort', rejectCancelled, {once: true});
            if (controller.signal.aborted) rejectCancelled();
        });
        const work = thumbnailTail.then(async () => {
            let bitmap;
            try {
                controller.signal.throwIfAborted();
                if (!admit()) return null;
                // No deadline may force artwork onto a busy main thread.
                await new Promise((resolve, reject) => {
                    const cancelled = () => { window.cancelIdleCallback(id); reject(new Error('Artwork cancelled')); };
                    const id = window.requestIdleCallback(deadline => {
                        controller.signal.removeEventListener('abort', cancelled);
                        if (deadline.timeRemaining() < 8) reject(new Error('No artwork idle budget'));
                        else resolve();
                    });
                    controller.signal.addEventListener('abort', cancelled, {once: true});
                    if (controller.signal.aborted) cancelled();
                });
                controller.signal.throwIfAborted();
                if (!admit()) return null;
                let original = pictureBlobs.get(source);
                if (!original) {
                    if (url.protocol === 'blob:') throw new Error('Unknown artwork blob');
                    // A cold cache skips the picture; it must never download an original for this tiny view.
                    const response = await fetch(url.href, {signal: controller.signal, credentials: 'same-origin', priority: 'low', mode: 'same-origin', cache: 'only-if-cached'});
                    if (!response.ok) throw new Error('Artwork unavailable');
                    const reader = response.body.getReader(), chunks = [];
                    let length = 0, complete = false;
                    try {
                        while (true) {
                            const {value, done} = await reader.read();
                            if (done) { complete = true; break; }
                            length += value.byteLength;
                            if (length > IMAGE_LIMIT) throw new Error('Artwork exceeds budget');
                            chunks.push(value);
                        }
                    } finally {
                        if (!complete) await reader.cancel().catch(() => {});
                        reader.releaseLock();
                    }
                    original = new Blob(chunks);
                }
                bitmap = await createImageBitmap(original);
                controller.signal.throwIfAborted();
                const scale = Math.min(1, 96 / Math.max(bitmap.width, bitmap.height));
                const canvas = new OffscreenCanvas(Math.max(1, Math.round(bitmap.width * scale)), Math.max(1, Math.round(bitmap.height * scale)));
                canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
                const blob = await canvas.convertToBlob({type: 'image/webp', quality: 0.85});
                controller.signal.throwIfAborted();
                if (blob.size > 64 * 1024) throw new Error('Thumbnail exceeds budget');
                return URL.createObjectURL(blob);
            } finally { bitmap?.close(); }
        });
        thumbnailTail = work.catch(() => {});
        const pending = Promise.race([work, cancelled]);
        const cleanup = () => {
            clearTimeout(timeout); signal?.removeEventListener('abort', abort);
            controller.signal.removeEventListener('abort', rejectCancelled);
        };
        pending.then(cleanup, cleanup);
        thumbnails.set(source, pending);
        const forget = () => { if (thumbnails.get(source) === pending) thumbnails.delete(source); };
        pending.then(value => { if (!value) forget(); }, forget);
        return pending;
    }
    window.addEventListener('pagehide', event => {
        if (event.persisted) return;
        for (const url of pictures.values()) URL.revokeObjectURL(url);
        pictures.clear(); pictureBlobs.clear(); cache.clear(); imageBytes = 0;
        for (const pending of thumbnails.values()) pending.then(url => URL.revokeObjectURL(url), () => {});
        thumbnails.clear();
    });
    window.FrontierMusicMetadata = {metadata, decorate, parse, thumbnail, canThumbnail};
})();
