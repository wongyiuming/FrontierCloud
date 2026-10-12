'use strict';

// Shared by the page and its service worker. Metadata describes independently
// authenticated chunks, so a seek only needs the chunks covering its byte range.
(() => {
    const CHUNK_SIZE = 1024 * 1024;
    const TAG_SIZE = 16;
    const encoder = new TextEncoder();
    function monotonicNow() {
        // This timestamp can cross page/worker contexts without depending on
        // later changes to the user's wall clock.
        const value = globalThis.performance?.timeOrigin + globalThis.performance?.now?.();
        if (!Number.isFinite(value)) {
            const error = new Error('浏览器单调时钟不可用');
            error.status = 422;
            throw error;
        }
        return value;
    }
    function decode(value) {
        const binary = atob(value);
        return Uint8Array.from(binary, character => character.charCodeAt(0));
    }
    function encode(value) {
        return btoa(Array.from(new Uint8Array(value), byte => String.fromCharCode(byte)).join(''));
    }
    function validate(metadata) {
        if (!metadata || metadata.version !== 1 || metadata.algorithm !== 'AES-256-GCM'
            || metadata.chunk_size !== CHUNK_SIZE || !/^[a-f0-9]{32}$/.test(metadata.file_id)
            || !Number.isSafeInteger(metadata.plaintext_size) || metadata.plaintext_size <= 0
            || metadata.plaintext_size > 10 * 1024 * 1024 * 1024
            || Math.ceil(metadata.plaintext_size / CHUNK_SIZE) >= 2 ** 32
            || decode(metadata.nonce_prefix).byteLength !== 8
            || metadata.ciphertext_size !== metadata.plaintext_size
                + TAG_SIZE * Math.ceil(metadata.plaintext_size / CHUNK_SIZE)) {
            throw new Error('加密文件描述无效');
        }
        return metadata;
    }
    function parameters(metadata, index) {
        const iv = new Uint8Array(12);
        iv.set(decode(metadata.nonce_prefix));
        new DataView(iv.buffer).setUint32(8, index, false);
        return {name: 'AES-GCM', iv, tagLength: 128,
            additionalData: encoder.encode(`frontiercloud:chunk:v1:${metadata.file_id}:${metadata.plaintext_size}:${index}`)};
    }
    function cipherRange(metadata, index) {
        const start = index * (CHUNK_SIZE + TAG_SIZE);
        const plainSize = Math.min(CHUNK_SIZE, metadata.plaintext_size - index * CHUNK_SIZE);
        return {start, end: start + plainSize + TAG_SIZE - 1, size: plainSize + TAG_SIZE};
    }
    function range(header, size) {
        if (!header) return {start: 0, end: size - 1, partial: false};
        const match = /^bytes=(\d*)-(\d*)$/.exec(header);
        if (!match || (!match[1] && !match[2]) || size === 0) return null;
        let start;
        let end;
        if (!match[1]) {
            const suffix = Number(match[2]);
            if (!Number.isSafeInteger(suffix) || suffix <= 0) return null;
            start = Math.max(0, size - suffix);
            end = size - 1;
        } else {
            start = Number(match[1]);
            end = match[2] ? Number(match[2]) : size - 1;
        }
        if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end)
            || start < 0 || start >= size || end < start) return null;
        return {start, end: Math.min(end, size - 1), partial: true};
    }
    async function boundedBytes(response, maximum) {
        if (!response.body) throw new Error('加密数据读取失败');
        const reader = response.body.getReader();
        const chunks = [];
        let length = 0;
        try {
            while (true) {
                const {done, value} = await reader.read();
                if (done) break;
                length += value.byteLength;
                if (length > maximum) throw new Error('存储响应超过加密分块边界');
                chunks.push(value);
            }
        } catch (error) {
            await reader.cancel().catch(() => {});
            throw error;
        } finally { reader.releaseLock(); }
        if (length !== maximum) throw new Error('加密数据分块不完整');
        const bytes = new Uint8Array(length);
        let offset = 0;
        for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
        return bytes;
    }
    function parseLyrics(text) {
        // Keep encrypted lyrics identical to media.ParseLRC: one leading BOM,
        // Go's Unicode whitespace/line boundaries, and millisecond timestamps.
        text = text.replace(/^\uFEFF/, '');
        if (!text || text.includes('\0') || /[\uD800-\uDFFF]/u.test(text))
            throw new Error('歌词文件必须使用非空 UTF-8 编码');
        let offset = 0;
        const offsetMatch = /\[offset:([+-]?\d+)\]/i.exec(text);
        if (offsetMatch) {
            offset = Number(offsetMatch[1]);
            if (!Number.isSafeInteger(offset) || offset < -1000000000 || offset > 1000000000)
                throw new Error('invalid lyric offset');
        }
        const entries = [];
        const seen = new Set();
        const timeTag = /\[(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?\]/g;
        const whitespace = /^[\u0009-\u000D\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]+|[\u0009-\u000D\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]+$/g;
        for (const line of text.split(/[\n\r\v\f\u001C-\u001E\u0085\u2028\u2029]/)) {
            const tags = [...line.matchAll(timeTag)];
            if (!tags.length) continue;
            const value = line.replace(timeTag, '').replace(whitespace, '');
            if (!value) continue;
            if ([...value].length > 4000) throw new Error('单行歌词最多允许 4000 个字符');
            for (const tag of tags) {
                const fraction = tag[3] ? Number(tag[3].padEnd(3, '0')) : 0;
                const milliseconds = Math.max(0, Number(tag[1]) * 60000 + Number(tag[2]) * 1000 + fraction + offset);
                const identity = milliseconds + ':' + value;
                if (seen.has(identity)) continue;
                seen.add(identity);
                entries.push({time: milliseconds / 1000, text: value});
                if (entries.length > 10000) throw new Error('歌词最多允许 10000 行');
            }
        }
        if (!entries.length) throw new Error('LRC 歌词没有可展示的时间轴内容');
        return entries.sort((left, right) => left.time - right.time);
    }
    globalThis.FrontierCryptoCommon = {CHUNK_SIZE, TAG_SIZE, monotonicNow, encode, decode, validate,
        parameters, cipherRange, range, boundedBytes, parseLyrics};
})();
