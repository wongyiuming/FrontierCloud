'use strict';

(() => {
    const common = window.FrontierCryptoCommon;
    const admin = Boolean(document.getElementById('uploadFiles'));
    const apiBase = admin ? '/api/v1/media/admin/crypto' : '/api/v1/media/crypto';
    const token = Array.from(crypto.getRandomValues(new Uint8Array(16)),
        byte => byte.toString(16).padStart(2, '0')).join('');
    let session = null;
    let sessionPromise = null;
    let workerPromise = null;
    const envelopes = new Map();
    const faults = new Map();
    const temporaryNames = new Set();

    function requireCrypto() {
        if (!window.isSecureContext || !crypto.subtle || !common)
            throw new Error('加密功能需要 HTTPS 和支持 Web Crypto 的浏览器');
    }
    function headers() {
        return admin && typeof requestHeaders === 'function'
            ? requestHeaders() : {'Content-Type': 'application/json'};
    }
    async function jsonRequest(path, body) {
        const response = await fetch(apiBase + path, {method: 'POST', credentials: 'same-origin',
            cache: 'no-store', headers: headers(), body: JSON.stringify(body)});
        const data = await response.json();
        if (!response.ok) {
            const error = new Error(typeof data.detail === 'string' ? data.detail : '浏览器密钥授权失败');
            error.status = response.status;
            throw error;
        }
        return data;
    }
    async function getSession() {
        requireCrypto();
        if (session && session.deadline > common.monotonicNow() + 30000) return session;
        if (sessionPromise) return sessionPromise;
        sessionPromise = (async () => {
            const pair = await crypto.subtle.generateKey({name: 'ECDH', namedCurve: 'P-256'}, false, ['deriveBits']);
            const publicKey = await crypto.subtle.exportKey('raw', pair.publicKey);
            const startedAt = common.monotonicNow();
            const handshake = await jsonRequest('/session', {public_key: common.encode(publicKey)});
            if (!Number.isInteger(handshake.expires_in) || handshake.expires_in < 1 || handshake.expires_in > 900
                || !Number.isSafeInteger(handshake.expires_at)
                || handshake.expires_at <= 0) {
                const error = new Error('临时密钥会话期限无效');
                error.status = 422;
                throw error;
            }
            const deadline = startedAt + handshake.expires_in * 1000;
            if (deadline <= common.monotonicNow()) {
                const error = new Error('临时密钥授权已过期');
                error.status = 403;
                throw error;
            }
            const serverKey = await crypto.subtle.importKey('raw', common.decode(handshake.public_key),
                {name: 'ECDH', namedCurve: 'P-256'}, false, []);
            const secret = await crypto.subtle.deriveBits({name: 'ECDH', public: serverKey}, pair.privateKey, 256);
            const hkdfKey = await crypto.subtle.importKey('raw', secret, 'HKDF', false, ['deriveKey']);
            new Uint8Array(secret).fill(0);
            const wrapKey = await crypto.subtle.deriveKey({name: 'HKDF', hash: 'SHA-256',
                salt: common.decode(handshake.salt),
                info: new TextEncoder().encode('frontiercloud:browser-wrap:v1:' + handshake.session_id)},
            hkdfKey, {name: 'AES-GCM', length: 256}, false, ['decrypt']);
            session = {session_id: handshake.session_id, expires_at: handshake.expires_at, deadline, wrapKey};
            envelopes.clear();
            return session;
        })().finally(() => { sessionPromise = null; });
        return sessionPromise;
    }
    async function unwrap(activeSession, metadata, envelope) {
        common.validate(metadata);
        if (envelope.expires_at !== activeSession.expires_at || activeSession.deadline <= common.monotonicNow()) {
            const error = new Error('临时密钥授权已过期或期限不一致');
            error.status = 403;
            throw error;
        }
        const raw = await crypto.subtle.decrypt({name: 'AES-GCM', tagLength: 128,
            iv: common.decode(envelope.iv), additionalData: new TextEncoder().encode(
                `frontiercloud:key-envelope:v1:${activeSession.session_id}:${metadata.file_id}`)},
        activeSession.wrapKey, common.decode(envelope.wrapped_key));
        try {
            if (raw.byteLength !== 32) throw new Error('临时密钥长度无效');
            return await crypto.subtle.importKey('raw', raw, 'AES-GCM', false, ['encrypt', 'decrypt']);
        } finally { new Uint8Array(raw).fill(0); }
    }
    async function fileGrant(filePath, expectedFileId = '') {
        const activeSession = await getSession();
        const cached = envelopes.get(filePath);
        if (cached && cached.deadline > common.monotonicNow() + 10000
            && (!expectedFileId || cached.encryption.file_id === expectedFileId)) return cached;
        const data = await jsonRequest('/key', {session_id: activeSession.session_id, file_path: filePath});
        if (expectedFileId && data.encryption?.file_id !== expectedFileId) {
            const error = new Error('文件已替换，请刷新目录后重新选择');
            error.status = 409;
            throw error;
        }
        const key = await unwrap(activeSession, data.encryption, data.key_envelope);
        const grant = {encryption: data.encryption, key,
            source_url: data.source_url || data.download_url, content_type: data.content_type,
            filename: data.filename, expires_at: data.key_envelope.expires_at, deadline: activeSession.deadline};
        if (envelopes.size >= 32) envelopes.delete(envelopes.keys().next().value);
        envelopes.set(filePath, grant);
        return grant;
    }
    function workerMessage(worker, data) {
        return new Promise((resolve, reject) => {
            const channel = new MessageChannel();
            const timeout = setTimeout(() => { channel.port1.close(); reject(new Error('加密播放初始化超时')); }, 15000);
            channel.port1.onmessage = event => {
                clearTimeout(timeout);
                channel.port1.close();
                if (event.data?.error) reject(new Error(event.data.error));
                else resolve(event.data);
            };
            worker.postMessage(data, [channel.port2]);
        });
    }
    async function ensureWorker() {
        requireCrypto();
        if (!navigator.serviceWorker) throw new Error('此浏览器不支持加密媒体流式播放');
        if (!workerPromise) workerPromise = (async () => {
            await navigator.serviceWorker.register('/media-crypto-sw.js', {scope: '/', updateViaCache: 'none'});
            await navigator.serviceWorker.ready;
            if (!navigator.serviceWorker.controller) {
                await new Promise((resolve, reject) => {
                    const timer = setTimeout(() => {
                        navigator.serviceWorker.removeEventListener('controllerchange', changed);
                        reject(new Error('加密媒体服务尚未就绪，请刷新页面'));
                    }, 15000);
                    const changed = () => {
                        if (!navigator.serviceWorker.controller) return;
                        clearTimeout(timer);
                        navigator.serviceWorker.removeEventListener('controllerchange', changed);
                        resolve();
                    };
                    navigator.serviceWorker.addEventListener('controllerchange', changed);
                    changed();
                });
            }
        })().catch(error => { workerPromise = null; throw error; });
        await workerPromise;
        // Worker suspension discards its in-memory capabilities. Register the
        // live page again before each operation, retaining its existing keys.
        await workerMessage(navigator.serviceWorker.controller, {type: 'fc-crypto-register', token});
    }
    navigator.serviceWorker?.addEventListener('message', async event => {
        const data = event.data;
        if (!data || data.token !== token || event.source !== navigator.serviceWorker.controller) return;
        if (data.type === 'fc-crypto-fault') {
            faults.set(data.file_path, data.error || '加密媒体完整性校验失败');
            window.dispatchEvent(new CustomEvent('frontier-crypto-fault', {detail: data}));
            return;
        }
        if (data.type !== 'fc-crypto-grant' || !event.ports[0]) return;
        try { event.ports[0].postMessage(await fileGrant(data.file_path, data.file_id)); }
        catch (error) { event.ports[0].postMessage({error: error.message,
            status: error.status || (error.name === 'OperationError' ? 422 : undefined)}); }
    });
    function virtualUrl(filePath, download = false, fileId = '') {
        const query = new URLSearchParams();
        if (download) query.set('download', '1');
        if (fileId) query.set('object', fileId);
        return `/__fc_media/${token}/${encodeURIComponent(filePath)}${query.size ? '?' + query : ''}`;
    }
    async function prepareCatalog(entries) {
        if (!entries?.some(media => media.encryption || media.encrypted)) return entries;
        await ensureWorker();
        for (const media of entries) {
            if (!media.encryption && !media.encrypted) continue;
            const path = media.media_path || media.path;
            if (!path) throw new Error('加密媒体路径缺失');
            media.url = virtualUrl(path, false, media.encryption?.file_id);
        }
        return entries;
    }
    async function textFor(filePath, fileId = '') {
        await ensureWorker();
        const response = await fetch(virtualUrl(filePath, false, fileId), {credentials: 'same-origin', cache: 'no-store'});
        if (!response.ok) throw new Error('加密歌词读取失败');
        // Lyrics are a separately bounded business object (not media content).
        const limit = 8 * 1024 * 1024;
        const expected = Number(response.headers.get('Content-Length'));
        if (!Number.isSafeInteger(expected) || expected < 0 || expected > limit) {
            await response.body?.cancel();
            throw new Error('歌词超出浏览器读取限制');
        }
        // ParseLRC rejects invalid UTF-8 and removes exactly one leading BOM.
        // Preserve that BOM for the shared parser rather than removing it twice.
        return new TextDecoder('utf-8', {fatal: true, ignoreBOM: true})
            .decode(await common.boundedBytes(response, expected));
    }
    const MAX_RECORDING_LYRIC_BYTES = 1400 * 1024;
    function recordingLyrics(entries) {
        if (!Array.isArray(entries) || entries.length > 10000
            || entries.some(entry => !Number.isFinite(entry?.time) || entry.time < 0
                || typeof entry.text !== 'string' || entry.text.length > 4096
                || new TextEncoder().encode(entry.text).byteLength > 4096))
            throw new Error('录音歌词快照超过行数或内容限制');
        return entries.map(entry => ({time: entry.time, text: entry.text}));
    }
    async function encryptRecordingLyrics(entries, prepare) {
        const normalized = recordingLyrics(entries);
        const encoder = new TextEncoder();
        let size = 2 + Math.max(0, normalized.length - 1);
        for (const entry of normalized) {
            size += encoder.encode(JSON.stringify(entry)).byteLength;
            if (size > MAX_RECORDING_LYRIC_BYTES) throw new Error('录音歌词快照超过 1400 KiB 限制');
        }
        const plaintext = encoder.encode(JSON.stringify(normalized));
        try {
            if (plaintext.byteLength > MAX_RECORDING_LYRIC_BYTES)
                throw new Error('录音歌词快照超过 1400 KiB 限制');
            const activeSession = await getSession();
            const prepared = await prepare({session_id: activeSession.session_id, plaintext_size: plaintext.byteLength});
            const metadata = common.validate(prepared.encryption);
            if (metadata.plaintext_size !== plaintext.byteLength || !prepared.preparation_token)
                throw new Error('录音歌词加密准备无效');
            const key = await unwrap(activeSession, metadata, prepared.key_envelope);
            const ciphertext = new Uint8Array(metadata.ciphertext_size);
            for (let start = 0, index = 0; start < plaintext.byteLength; start += common.CHUNK_SIZE, index += 1) {
                const chunk = plaintext.subarray(start, Math.min(plaintext.byteLength, start + common.CHUNK_SIZE));
                ciphertext.set(new Uint8Array(await crypto.subtle.encrypt(common.parameters(metadata, index), key, chunk)),
                    common.cipherRange(metadata, index).start);
            }
            return {encrypted_lyrics: {encryption: metadata, ciphertext: common.encode(ciphertext)},
                preparation_token: prepared.preparation_token};
        } finally { plaintext.fill(0); }
    }
    async function decryptRecordingLyrics(authorize) {
        const activeSession = await getSession();
        const grant = await authorize({session_id: activeSession.session_id});
        const snapshot = grant.encrypted_lyrics;
        const metadata = common.validate(snapshot?.encryption);
        if (metadata.plaintext_size > MAX_RECORDING_LYRIC_BYTES || typeof snapshot.ciphertext !== 'string'
            || snapshot.ciphertext.length !== 4 * Math.ceil(metadata.ciphertext_size / 3)
            || !/^[A-Za-z0-9+/]*={0,2}$/.test(snapshot.ciphertext))
            throw new Error('加密录音歌词描述无效');
        const ciphertext = common.decode(snapshot.ciphertext);
        if (ciphertext.byteLength !== metadata.ciphertext_size || common.encode(ciphertext) !== snapshot.ciphertext)
            throw new Error('加密录音歌词长度无效');
        const key = await unwrap(activeSession, metadata, grant.key_envelope);
        const plaintext = new Uint8Array(metadata.plaintext_size);
        try {
            for (let index = 0; index < Math.ceil(metadata.plaintext_size / common.CHUNK_SIZE); index += 1) {
                const range = common.cipherRange(metadata, index);
                const chunk = new Uint8Array(await crypto.subtle.decrypt(common.parameters(metadata, index), key,
                    ciphertext.subarray(range.start, range.end + 1)));
                try { plaintext.set(chunk, index * common.CHUNK_SIZE); }
                finally { chunk.fill(0); }
            }
            return recordingLyrics(JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(plaintext)));
        } catch (error) {
            error.status = error.status || 422;
            throw error;
        } finally { plaintext.fill(0); }
    }
    async function temporaryDirectory() {
        if (!navigator.storage?.getDirectory) throw new Error('加密上传需要支持临时文件存储的浏览器');
        const directory = await navigator.storage.getDirectory();
        return directory.getDirectoryHandle('frontiercloud-cipher-upload-v1', {create: true});
    }
    async function cleanAbandonedTemporaryFiles() {
        if (!navigator.storage?.getDirectory) return;
        const directory = await temporaryDirectory();
        for await (const [name] of directory.entries()) {
            // A live upload is bounded to minutes. Leave other tabs' current
            // files alone; crash leftovers become removable after one day.
            const created = Number(name.split('-', 1)[0]);
            if (created > 0 && Date.now() - created > 86400000)
                await directory.removeEntry(name).catch(() => {});
        }
    }
    async function encryptUploadFile(file, onProgress = () => {}) {
        requireCrypto();
        const extension = /\.[^.]+$/.exec(file.name || '')?.[0].toLowerCase();
        if (extension === '.lrc') {
            if (file.size < 1 || file.size > 2 * 1024 * 1024)
                throw new Error('歌词文件最多允许 2 MiB');
            const payload = await file.slice(0, file.size).arrayBuffer();
            try {
                common.parseLyrics(new TextDecoder('utf-8', {fatal: true, ignoreBOM: true}).decode(payload));
            } finally { new Uint8Array(payload).fill(0); }
        } else {
            const head = new Uint8Array(await file.slice(0, Math.min(file.size, 12)).arrayBuffer());
            const starts = values => values.every((value, index) => head[index] === value);
            const word = (start, end) => String.fromCharCode(...head.subarray(start, end));
            const valid = extension === '.mp3' && (word(0, 3) === 'ID3'
                    || head.length >= 2 && head[0] === 0xff && (head[1] & 0xe0) === 0xe0)
                || extension === '.flac' && word(0, 4) === 'fLaC'
                || extension === '.wav' && head.length >= 12 && word(0, 4) === 'RIFF' && word(8, 12) === 'WAVE'
                || ['.m4a', '.mp4'].includes(extension) && head.length >= 12 && word(4, 8) === 'ftyp'
                || ['.webm', '.mkv'].includes(extension) && starts([0x1a, 0x45, 0xdf, 0xa3]);
            head.fill(0);
            if (!valid) throw new Error('上传失败，文件内容不是受支持的媒体格式');
        }
        // Validate the bounded plaintext locally before key preparation or
        // encryption. Neither content validation nor media encryption uses the Master.
        return encryptFile(file, onProgress);
    }
    async function encryptFile(file, onProgress = () => {}) {
        const activeSession = await getSession();
        const prepared = await jsonRequest('/prepare', {session_id: activeSession.session_id, plaintext_size: file.size});
        const metadata = common.validate(prepared.encryption);
        if (metadata.plaintext_size !== file.size) throw new Error('加密准备与文件长度不一致');
        const key = await unwrap(activeSession, metadata, prepared.key_envelope);
        const directory = await temporaryDirectory();
        const name = `${Date.now()}-${token}-${metadata.file_id}`;
        const handle = await directory.getFileHandle(name, {create: true});
        temporaryNames.add(name);
        let output;
        const cleanup = async () => {
            await directory.removeEntry(name).catch(() => {});
            temporaryNames.delete(name);
        };
        try {
            output = await handle.createWritable();
            for (let start = 0, index = 0; start < file.size; start += common.CHUNK_SIZE, index += 1) {
                const plaintext = await file.slice(start, Math.min(file.size, start + common.CHUNK_SIZE)).arrayBuffer();
                try {
                    const ciphertext = await crypto.subtle.encrypt(common.parameters(metadata, index), key, plaintext);
                    await output.write(ciphertext);
                } finally { new Uint8Array(plaintext).fill(0); }
                onProgress(Math.min(1, (start + common.CHUNK_SIZE) / Math.max(1, file.size)));
            }
            await output.close();
            const ciphertext = await handle.getFile();
            if (ciphertext.size !== metadata.ciphertext_size) throw new Error('加密文件长度校验失败');
            return {file: ciphertext, encryption: metadata, preparation_token: prepared.preparation_token, cleanup};
        } catch (error) {
            if (output) await output.abort().catch(() => {});
            await cleanup();
            throw error;
        }
    }
    function failureFor(input) {
        try {
            const path = new URL(String(input), location.href).pathname;
            const prefix = `/__fc_media/${token}/`;
            return path.startsWith(prefix) ? faults.get(decodeURIComponent(path.slice(prefix.length))) : null;
        } catch (_) { return null; }
    }
    function resetFault(filePath) {
        faults.delete(filePath);
        envelopes.delete(filePath);
        navigator.serviceWorker?.controller?.postMessage({type: 'fc-crypto-invalidate', token, file_path: filePath});
    }

    // Stored ZIP entries avoid compression and retain a one-chunk memory bound.
    // ZIP64 supports large folders; browser download receives a
    // ReadableStream through a worker route, rather than an album-sized Blob.
    const crcTable = Uint32Array.from({length: 256}, (_, value) => {
        let crc = value;
        for (let bit = 0; bit < 8; bit += 1) crc = (crc & 1) ? 0xedb88320 ^ (crc >>> 1) : crc >>> 1;
        return crc >>> 0;
    });
    function crcUpdate(crc, bytes) {
        for (const byte of bytes) crc = crcTable[(crc ^ byte) & 255] ^ (crc >>> 8);
        return crc;
    }
    async function downloadPlan(items) {
        await ensureWorker();
        const normalized = items.map(item => ({...item, file_path: item.path,
            plaintext_size: item.encryption?.plaintext_size ?? item.size ?? item.size_bytes,
            url: item.encryption ? virtualUrl(item.path, false, item.encryption.file_id) : item.url}));
        if (normalized.length === 1) {
            const anchor = document.createElement('a');
            anchor.href = normalized[0].encryption
                ? virtualUrl(normalized[0].path, true, normalized[0].encryption.file_id) : normalized[0].url;
            if (!normalized[0].encryption)
                anchor.download = normalized[0].filename || normalized[0].path.split('/').pop();
            anchor.click();
            return;
        }
        if (!normalized.length || normalized.length > 5000) throw new Error('下载任务最多包含 5000 个文件，请分批下载');
        let total = 98;
        for (const item of normalized) {
            const nameBytes = new TextEncoder().encode(item.filename || item.path);
            if (!Number.isSafeInteger(item.plaintext_size) || item.plaintext_size < 0
                || nameBytes.length > 65535)
                throw new Error('下载文件描述无效');
            total += item.plaintext_size + 148 + nameBytes.length * 2;
        }
        if (!Number.isSafeInteger(total)) throw new Error('下载总量超出浏览器长度限制，请分批下载');
        const downloadToken = token + '-' + crypto.randomUUID();
        await workerMessage(navigator.serviceWorker.controller,
            {type: 'fc-crypto-zip', token, download_token: downloadToken, items: normalized});
        const anchor = document.createElement('a');
        anchor.href = '/__fc_zip/' + downloadToken;
        // Attachment navigation lets the worker serve the download body.
        // Chromium's download attribute can bypass its fetch handler.
        anchor.click();
    }
    window.addEventListener('pagehide', event => {
        if (event.persisted) return;
        navigator.serviceWorker?.controller?.postMessage({type: 'fc-crypto-revoke', token});
        session = null;
        envelopes.clear();
        if (temporaryNames.size) void temporaryDirectory().then(async directory => {
            for (const name of temporaryNames) await directory.removeEntry(name).catch(() => {});
        }).catch(() => {});
    });
    window.FrontierMediaCrypto = {prepareCatalog, ensureWorker, encryptFile, encryptUploadFile, textFor,
        encryptRecordingLyrics, decryptRecordingLyrics,
        virtualUrl, failureFor, resetFault, downloadPlan, parseLyrics: common?.parseLyrics, crcUpdate};
    void cleanAbandonedTemporaryFiles().catch(() => {});
})();
