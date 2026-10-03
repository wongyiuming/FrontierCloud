'use strict';

(() => {
    if (typeof PLAYER_KIND === 'undefined' || PLAYER_KIND !== 'audio') return;

    const MIME = 'audio/mpeg';
    const LOOKAHEAD_TRACKS = 2;
    const MAX_TRACK_BYTES = 128 * 1024 * 1024;
    const FIRST_APPEND_BYTES = 64 * 1024;
    const APPEND_BATCH_BYTES = 512 * 1024;
    const APPEND_MAX_WAIT_MS = 1000;
    const DURATION_PROBE_BYTES = 128 * 1024;
    const MAX_BUFFER_AHEAD_SECONDS = 30;
    const SEEK_RANGE_ALIGNMENT_BYTES = 64 * 1024;
    const SEEK_RESTART_DELAY_MS = 120;
    const BOUNDARY_EPSILON = 0.08;
    const PRUNE_AFTER_SECONDS = 2;
    const NOTICE_MS = 3500;

    const legacy = {
        initPlayer,
        playNext,
        playPrev,
        checkAndPreloadNext,
        applyMediaCatalog,
        renderPlaylist,
    };

    let session = null;
    let sessionGeneration = 0;
    let mseSupport = null;

    function mediaPath(media) {
        return String(media?.media_path || '').split('?', 1)[0].toLowerCase();
    }

    function browserSupportsContinuousAudio() {
        if (mseSupport !== null) return mseSupport;
        mseSupport = typeof MediaSource === 'function'
            && typeof MediaSource.isTypeSupported === 'function'
            && MediaSource.isTypeSupported(MIME);
        return mseSupport;
    }

    function streamCompatible(media) {
        return browserSupportsContinuousAudio()
            && media?.type === 'audio'
            && mediaPath(media).endsWith('.mp3');
    }

    function annotateCatalog(entries) {
        if (!Array.isArray(entries)) return entries;
        const supported = browserSupportsContinuousAudio();
        for (const media of entries) {
            const compatible = Boolean(supported && media?.type === 'audio' && mediaPath(media).endsWith('.mp3'));
            media.continuous_stream_compatible = compatible;
            media.continuous_stream_skip_reason = supported && media?.type === 'audio' && !compatible
                ? '连续流不兼容 · 自动续播跳过'
                : '';
        }
        return entries;
    }

    function compatibleIndices() {
        const result = [];
        for (let index = 0; index < (currentMediaList?.length || 0); index += 1) {
            if (streamCompatible(currentMediaList[index])) result.push(index);
        }
        return result;
    }

    function cyclicOrder(startIndex) {
        const indices = compatibleIndices();
        if (!indices.length) return [];
        const startAt = indices.indexOf(startIndex);
        if (startAt < 0) return indices;
        return [...indices.slice(startAt), ...indices.slice(0, startAt)];
    }

    function nextCompatibleIndex(fromIndex, direction) {
        const length = currentMediaList?.length || 0;
        if (!length) return null;
        for (let offset = 1; offset <= length; offset += 1) {
            const index = (fromIndex + direction * offset + length * 2) % length;
            if (streamCompatible(currentMediaList[index])) return index;
        }
        return null;
    }

    function skippedBetween(fromIndex, toIndex) {
        const length = currentMediaList?.length || 0;
        if (!length || fromIndex === toIndex) return 0;
        let count = 0;
        let index = fromIndex;
        for (let guard = 0; guard < length; guard += 1) {
            index = (index + 1) % length;
            if (index === toIndex) break;
            if (!streamCompatible(currentMediaList[index])) count += 1;
        }
        return count;
    }

    function setPlaylistActive(index) {
        document.querySelectorAll('.media-item').forEach(item => item.classList.remove('active'));
        const target = document.querySelector(`.media-item[data-index="${index}"]`);
        if (!target) return;
        target.classList.add('active');
        target.scrollIntoView({block: 'nearest', behavior: 'auto'});
    }

    function showNotice(message) {
        if (!art?.notice || !message) return;
        art.notice.show = message;
        window.setTimeout(() => {
            if (art?.notice?.show === message) art.notice.show = '';
        }, NOTICE_MS);
    }

    function decoratePlaylist() {
        const entries = currentMediaList || [];
        const hasWarning = entries.some(media => Boolean(media.continuous_stream_skip_reason));
        const existing = document.querySelector('.continuous-stream-warning');
        if (!hasWarning && !existing) return;
        for (const [index, media] of entries.entries()) {
            const row = document.querySelector(`.media-item[data-index="${index}"]`);
            if (!row) continue;
            row.querySelector('.continuous-stream-warning')?.remove();
            row.classList.toggle('continuous-stream-incompatible', Boolean(media.continuous_stream_skip_reason));
            if (!media.continuous_stream_skip_reason) continue;
            const info = row.querySelector('.media-info');
            if (!info) continue;
            const warning = document.createElement('div');
            warning.className = 'media-artist continuous-stream-warning';
            warning.textContent = media.continuous_stream_skip_reason;
            info.append(warning);
        }
    }

    function activateBusinessTrack(index, skipped = 0) {
        const media = currentMediaList?.[index];
        if (!media || !art) return;

        accountPlaybackTime();
        void reportValidPlayback();
        currentIndex = index;
        resetPlaybackAccounting(media);
        setPlaylistActive(index);

        showSynchronizedLyrics([]);
        void loadInlineLyrics(media);
        const lyricsLink = document.getElementById('lyricsLink');
        if (lyricsLink) {
            lyricsLink.classList.toggle('unavailable', !media.has_lyrics);
            lyricsLink.disabled = !media.has_lyrics;
            lyricsLink.setAttribute(
                'aria-label',
                media.has_lyrics ? `全屏显示 ${media.title} 的歌词` : `${media.title} 暂无歌词`,
            );
        }

        art.title = middleEllipsis(media.title, 54);
        updateMediaSession(media);
        art._syncTime?.();
        art._syncBuffered?.();
        if (art.playing) startLyricClock();
        if (skipped > 0) showNotice(`已跳过 ${skipped} 首连续流不兼容曲目`);
    }

    function waitForEvent(target, success, failure = []) {
        return new Promise((resolve, reject) => {
            const cleanup = () => {
                target.removeEventListener(success, onSuccess);
                for (const name of failure) target.removeEventListener(name, onFailure);
            };
            const onSuccess = () => { cleanup(); resolve(); };
            const onFailure = event => { cleanup(); reject(event?.error || new Error(`${success} failed`)); };
            target.addEventListener(success, onSuccess, {once: true});
            for (const name of failure) target.addEventListener(name, onFailure, {once: true});
        });
    }

    function concatChunks(chunks, totalBytes) {
        if (chunks.length === 1 && chunks[0].byteOffset === 0 && chunks[0].byteLength === chunks[0].buffer.byteLength) {
            return chunks[0];
        }
        const merged = new Uint8Array(totalBytes);
        let offset = 0;
        for (const chunk of chunks) {
            merged.set(chunk, offset);
            offset += chunk.byteLength;
        }
        return merged;
    }

    async function readBeforeFlushDeadline(read, delay) {
        let timer;
        try {
            return await Promise.race([
                read,
                new Promise(resolve => {
                    timer = window.setTimeout(() => resolve(null), Math.max(0, delay));
                }),
            ]);
        } finally {
            window.clearTimeout(timer);
        }
    }

    function readUint32(bytes, offset) {
        if (offset < 0 || offset + 4 > bytes.length) return 0;
        return ((bytes[offset] << 24) >>> 0)
            + (bytes[offset + 1] << 16)
            + (bytes[offset + 2] << 8)
            + bytes[offset + 3];
    }

    function id3v2Size(bytes) {
        if (bytes.length < 10 || bytes[0] !== 0x49 || bytes[1] !== 0x44 || bytes[2] !== 0x33) return 0;
        const size = ((bytes[6] & 0x7f) << 21)
            | ((bytes[7] & 0x7f) << 14)
            | ((bytes[8] & 0x7f) << 7)
            | (bytes[9] & 0x7f);
        return 10 + size + ((bytes[5] & 0x10) ? 10 : 0);
    }

    function asciiAt(bytes, offset, text) {
        if (offset < 0 || offset + text.length > bytes.length) return false;
        for (let index = 0; index < text.length; index += 1) {
            if (bytes[offset + index] !== text.charCodeAt(index)) return false;
        }
        return true;
    }

    function estimateMp3Duration(bytes, contentLength) {
        if (!(bytes instanceof Uint8Array) || bytes.length < 4) return 0;
        const start = Math.min(id3v2Size(bytes), bytes.length - 4);
        const limit = Math.min(bytes.length - 4, start + DURATION_PROBE_BYTES);
        let frame = -1;
        for (let offset = start; offset <= limit; offset += 1) {
            if (bytes[offset] === 0xff && (bytes[offset + 1] & 0xe0) === 0xe0) {
                const version = (bytes[offset + 1] >> 3) & 0x03;
                const layer = (bytes[offset + 1] >> 1) & 0x03;
                const bitrateIndex = (bytes[offset + 2] >> 4) & 0x0f;
                const sampleIndex = (bytes[offset + 2] >> 2) & 0x03;
                if (version !== 1 && layer === 1 && bitrateIndex > 0 && bitrateIndex < 15 && sampleIndex < 3) {
                    frame = offset;
                    break;
                }
            }
        }
        if (frame < 0) return 0;

        const byte1 = bytes[frame + 1];
        const byte2 = bytes[frame + 2];
        const byte3 = bytes[frame + 3];
        const version = (byte1 >> 3) & 0x03;
        const bitrateIndex = (byte2 >> 4) & 0x0f;
        const sampleIndex = (byte2 >> 2) & 0x03;
        const mono = ((byte3 >> 6) & 0x03) === 3;
        const hasCrc = (byte1 & 0x01) === 0;
        const mpeg1 = version === 3;
        const bitrateTable = mpeg1
            ? [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
            : [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0];
        const baseRates = [44100, 48000, 32000];
        const sampleRate = version === 3 ? baseRates[sampleIndex]
            : version === 2 ? baseRates[sampleIndex] / 2
                : baseRates[sampleIndex] / 4;
        const samplesPerFrame = mpeg1 ? 1152 : 576;
        const sideInfo = mpeg1 ? (mono ? 17 : 32) : (mono ? 9 : 17);
        const xing = frame + 4 + (hasCrc ? 2 : 0) + sideInfo;
        if ((asciiAt(bytes, xing, 'Xing') || asciiAt(bytes, xing, 'Info')) && xing + 12 <= bytes.length) {
            const flags = readUint32(bytes, xing + 4);
            if (flags & 0x01) {
                const frames = readUint32(bytes, xing + 8);
                const duration = frames * samplesPerFrame / sampleRate;
                if (Number.isFinite(duration) && duration > 0) return duration;
            }
        }

        const vbri = frame + 4 + 32;
        if (asciiAt(bytes, vbri, 'VBRI') && vbri + 18 <= bytes.length) {
            const frames = readUint32(bytes, vbri + 14);
            const duration = frames * samplesPerFrame / sampleRate;
            if (Number.isFinite(duration) && duration > 0) return duration;
        }

        const bitrate = bitrateTable[bitrateIndex] * 1000;
        if (bitrate > 0 && Number(contentLength) > 0) {
            const duration = Math.max(0, (Number(contentLength) - start) * 8 / bitrate);
            if (Number.isFinite(duration) && duration > 0) return duration;
        }
        return 0;
    }

    function presentationDuration(segment) {
        const value = Number(segment?.presentationDuration || 0);
        return Number.isFinite(value) && value > 0 ? value : 0;
    }

    function latchPresentationDuration(segment, value) {
        if (!segment) return 0;
        const current = presentationDuration(segment);
        if (current > 0) return current;
        const next = Number(value);
        if (!Number.isFinite(next) || next <= 0) return 0;
        segment.presentationDuration = next;
        return next;
    }

    function responseContentRange(headers) {
        const raw = String(headers?.get?.('Content-Range') || '');
        const match = /^bytes\s+(\d+)-(\d+)\/(\d+|\*)$/i.exec(raw);
        if (!match) return null;
        return {
            start: Number(match[1]),
            end: Number(match[2]),
            total: match[3] === '*' ? 0 : Number(match[3]),
        };
    }

    function seekRangePlan(seekSeconds, totalBytes, duration) {
        const target = Math.min(Math.max(0, Number(seekSeconds) || 0), duration);
        const rawOffset = Math.floor(totalBytes * target / duration);
        const rangeStart = Math.min(
            totalBytes - 1,
            Math.max(0, Math.floor(rawOffset / SEEK_RANGE_ALIGNMENT_BYTES) * SEEK_RANGE_ALIGNMENT_BYTES
                - SEEK_RANGE_ALIGNMENT_BYTES),
        );
        return {
            seekSeconds: target,
            totalBytes,
            duration,
            rangeStart,
            localOffset: duration * rangeStart / totalBytes,
        };
    }

    class ContinuousAudioSession {
        constructor(startIndex, options = {}) {
            this.generation = ++sessionGeneration;
            this.order = cyclicOrder(startIndex);
            this.orderPosition = 0;
            this.mediaSource = new MediaSource();
            this.objectUrl = URL.createObjectURL(this.mediaSource);
            this.sourceBuffer = null;
            this.segments = [];
            this.activeSegment = null;
            this.appendPromise = null;
            this.closed = false;
            this.fetchController = new AbortController();
            this.startIndex = startIndex;
            this.lastPrunedBefore = 0;
            this.quotaWaitCount = 0;
            this.seekTimer = null;
            const seekSeconds = Number(options.seekSeconds);
            const totalBytes = Number(options.totalBytes);
            const duration = Number(options.duration);
            this.initialSeek = Number.isFinite(seekSeconds) && seekSeconds >= 0
                && Number.isFinite(totalBytes) && totalBytes > 0
                && Number.isFinite(duration) && duration > 0
                ? {seekSeconds: Math.min(seekSeconds, duration), totalBytes, duration}
                : null;
            this.initialSeekConsumed = false;
            this.resumeAfterSeek = Boolean(options.resumeAfterSeek);
        }

        owns(player) {
            return !this.closed && player === art && activeObjectUrl === this.objectUrl;
        }

        bufferedEnd() {
            const ranges = this.sourceBuffer?.buffered;
            if (!ranges?.length) return 0;
            return Number(ranges.end(ranges.length - 1)) || 0;
        }

        localTime() {
            if (!this.activeSegment || !art?.video) return Number(art?.video?.currentTime) || 0;
            if (Number.isFinite(this.activeSegment.pendingSeekLocal)) return this.activeSegment.pendingSeekLocal;
            const origin = Number(this.activeSegment.localOffset) || 0;
            return Math.max(0, origin + (Number(art.video.currentTime) || 0) - this.activeSegment.start);
        }

        localDuration() {
            return presentationDuration(this.activeSegment);
        }

        syncTimeUi(player) {
            const duration = this.localDuration();
            const local = Math.max(0, this.localTime());
            const current = duration > 0 ? Math.min(local, duration) : local;
            player.currentElement.textContent = formatTime(current);
            player.durationElement.textContent = formatTime(duration);
            const ratio = duration > 0 ? Math.min(1, Math.max(0, current / duration)) : 0;
            const percent = `${ratio * 100}%`;
            player.progressPlayed.style.width = percent;
            player.progressIndicator.style.left = percent;
            player.progressControl.setAttribute('aria-valuemin', '0');
            player.progressControl.setAttribute('aria-valuemax', String(duration || 0));
            player.progressControl.setAttribute('aria-valuenow', String(current || 0));
        }

        seekLocal(value) {
            if (!art?.video || !this.activeSegment) return;
            const next = Number(value);
            if (!Number.isFinite(next)) return;
            const duration = this.localDuration();
            const local = duration > 0 ? Math.min(duration, Math.max(0, next)) : Math.max(0, next);
            const origin = Number(this.activeSegment.localOffset) || 0;
            const target = this.activeSegment.start + local - origin;
            const ranges = art.video.buffered;
            if (ranges?.length) {
                for (let index = 0; index < ranges.length; index += 1) {
                    const low = ranges.start(index);
                    const high = ranges.end(index);
                    if (target >= low && target < high) {
                        art.video.currentTime = Math.min(high - 0.01, Math.max(low, target));
                        return;
                    }
                }
            }
            const totalBytes = Number(this.activeSegment.totalBytes) || 0;
            if (!duration || !totalBytes) {
                art.video.currentTime = target;
                return;
            }
            const restart = {
                seekSeconds: local,
                totalBytes,
                duration,
                resumeAfterSeek: Boolean(art.playing),
                preserveBusinessState: true,
            };
            window.clearTimeout(this.seekTimer);
            this.seekTimer = window.setTimeout(() => {
                if (this.closed || session !== this) return;
                void startContinuous(this.activeSegment.index, restart);
            }, SEEK_RESTART_DELAY_MS);
        }

        syncBufferedUi(player) {
            const duration = this.localDuration();
            if (!duration || !this.activeSegment || !player.video.buffered?.length) {
                player.progressLoaded.style.width = '0%';
                return;
            }
            const end = player.video.buffered.end(player.video.buffered.length - 1);
            const origin = Number(this.activeSegment.localOffset) || 0;
            const localEnd = Math.max(0, Math.min(duration, origin + end - this.activeSegment.start));
            player.progressLoaded.style.width = `${Math.min(100, localEnd / duration * 100)}%`;
        }

        async waitSourceOpen() {
            if (this.mediaSource.readyState === 'open') return;
            await waitForEvent(this.mediaSource, 'sourceopen', ['sourceclose']);
        }

        async waitUpdateEnd() {
            if (!this.sourceBuffer?.updating) return;
            await waitForEvent(this.sourceBuffer, 'updateend', ['error', 'abort']);
        }

        bufferedAhead() {
            if (!art?.video) return 0;
            return Math.max(0, this.bufferedEnd() - (Number(art.video.currentTime) || 0));
        }

        async waitForPlaybackProgress() {
            if (this.closed || session !== this || this.generation !== sessionGeneration) throw new Error('stale session');
            const video = art?.video;
            if (!video) return;
            await new Promise(resolve => {
                let settled = false;
                const finish = () => {
                    if (settled) return;
                    settled = true;
                    window.clearTimeout(timer);
                    video.removeEventListener('timeupdate', finish);
                    resolve();
                };
                const timer = window.setTimeout(finish, 500);
                video.addEventListener('timeupdate', finish, {once: true});
            });
        }

        async waitForAppendCapacity(quotaExceeded = false) {
            const initialTime = Number(art?.video?.currentTime) || 0;
            let waitForAdvance = quotaExceeded;
            while (!this.closed && session === this && this.generation === sessionGeneration) {
                await this.pruneBeforeActive();
                const currentTime = Number(art?.video?.currentTime) || 0;
                if (waitForAdvance && currentTime > initialTime + BOUNDARY_EPSILON) waitForAdvance = false;
                if (!waitForAdvance && this.bufferedAhead() < MAX_BUFFER_AHEAD_SECONDS) return;
                await this.waitForPlaybackProgress();
            }
            throw new Error('stale session');
        }

        appendBufferOnce(chunk) {
            return new Promise((resolve, reject) => {
                const cleanup = () => {
                    this.sourceBuffer.removeEventListener('updateend', onUpdateEnd);
                    this.sourceBuffer.removeEventListener('error', onFailure);
                    this.sourceBuffer.removeEventListener('abort', onFailure);
                };
                const onUpdateEnd = () => { cleanup(); resolve(); };
                const onFailure = event => { cleanup(); reject(event?.error || new Error('appendBuffer failed')); };
                this.sourceBuffer.addEventListener('updateend', onUpdateEnd, {once: true});
                this.sourceBuffer.addEventListener('error', onFailure, {once: true});
                this.sourceBuffer.addEventListener('abort', onFailure, {once: true});
                try {
                    this.sourceBuffer.appendBuffer(chunk);
                } catch (error) {
                    cleanup();
                    reject(error);
                }
            });
        }

        async appendBytes(value) {
            if (this.closed || session !== this || this.generation !== sessionGeneration) throw new Error('stale session');
            const chunk = value.byteOffset === 0 && value.byteLength === value.buffer.byteLength
                ? value.buffer
                : value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength);
            while (!this.closed && session === this && this.generation === sessionGeneration) {
                await this.waitUpdateEnd();
                await this.waitForAppendCapacity();
                try {
                    await this.appendBufferOnce(chunk);
                    return;
                } catch (error) {
                    if (error?.name !== 'QuotaExceededError') throw error;
                    this.quotaWaitCount += 1;
                    await this.waitForAppendCapacity(true);
                }
            }
            throw new Error('stale session');
        }

        markRuntimeSkip(index, reason) {
            const media = currentMediaList?.[index];
            if (!media) return;
            media.continuous_stream_runtime_skip = true;
            media.continuous_stream_skip_reason = reason || '连续流读取失败 · 自动续播跳过';
            legacy.renderPlaylist();
            decoratePlaylist();
            setPlaylistActive(currentIndex);
        }

        nextAppendIndex() {
            if (!this.order.length) return null;
            for (let guard = 0; guard < this.order.length; guard += 1) {
                const index = this.order[this.orderPosition % this.order.length];
                this.orderPosition = (this.orderPosition + 1) % this.order.length;
                if (!currentMediaList[index]?.continuous_stream_runtime_skip) return index;
            }
            return null;
        }

        async appendTrack(index) {
            const media = currentMediaList?.[index];
            if (!media || !streamCompatible(media)) return false;

            let seek = null;
            if (index === this.startIndex && this.initialSeek && !this.initialSeekConsumed) {
                this.initialSeekConsumed = true;
                seek = seekRangePlan(
                    this.initialSeek.seekSeconds,
                    this.initialSeek.totalBytes,
                    this.initialSeek.duration,
                );
            }

            let response;
            try {
                const headers = seek?.rangeStart > 0 ? {Range: `bytes=${seek.rangeStart}-`} : undefined;
                response = await fetch(media.url, {
                    credentials: 'same-origin', headers, signal: this.fetchController.signal,
                });
            } catch (_error) {
                if (this.closed || session !== this) return false;
                this.markRuntimeSkip(index, '连续流读取失败 · 自动续播跳过');
                return false;
            }
            if (!response.ok || !response.body) {
                this.markRuntimeSkip(index, '连续流读取失败 · 自动续播跳过');
                return false;
            }

            const responseLength = Number(response.headers.get('Content-Length') || 0);
            const range = responseContentRange(response.headers);
            const totalBytes = range?.total || seek?.totalBytes || responseLength;
            if (totalBytes > MAX_TRACK_BYTES) {
                this.markRuntimeSkip(index, '文件过大，不进入连续流 · 自动续播跳过');
                response.body.cancel?.().catch?.(() => {});
                return false;
            }
            const contentType = String(response.headers.get('Content-Type') || '').split(';', 1)[0].trim().toLowerCase();
            if (contentType && !['audio/mpeg', 'audio/mp3', 'application/octet-stream'].includes(contentType)) {
                this.markRuntimeSkip(index, '媒体响应格式不兼容连续流 · 自动续播跳过');
                response.body.cancel?.().catch?.(() => {});
                return false;
            }

            const start = this.bufferedEnd();
            const segment = {
                index,
                start,
                end: null,
                duration: null,
                durationHint: 0,
                presentationDuration: 0,
                totalBytes,
                rangeStart: range?.start || seek?.rangeStart || 0,
                localOffset: seek?.localOffset || 0,
                pendingSeekLocal: seek?.seekSeconds,
                pendingSeekGlobal: seek ? start + seek.seekSeconds - seek.localOffset : null,
                partial: false,
            };
            if (seek?.duration) {
                segment.durationHint = seek.duration;
                latchPresentationDuration(segment, seek.duration);
            }
            this.segments.push(segment);
            if (!this.activeSegment) this.activeSegment = segment;
            const reader = response.body.getReader();
            let bytes = 0;
            let pending = [];
            let pendingBytes = 0;
            let pendingSince = 0;
            let nextRead = null;
            let firstAppend = true;

            const flush = async () => {
                if (!pendingBytes) return;
                const merged = concatChunks(pending, pendingBytes);
                pending = [];
                pendingBytes = 0;
                pendingSince = 0;
                if (!presentationDuration(segment)) {
                    const estimate = estimateMp3Duration(merged, totalBytes);
                    if (estimate > 0) {
                        segment.durationHint = estimate;
                        latchPresentationDuration(segment, estimate);
                        if (segment === this.activeSegment || (!this.activeSegment && this.segments[0] === segment)) {
                            art?._syncTime?.();
                        }
                    }
                }
                await this.appendBytes(merged);
                if (Number.isFinite(segment.pendingSeekGlobal) && art?.video?.buffered?.length) {
                    const target = segment.pendingSeekGlobal;
                    for (let index = 0; index < art.video.buffered.length; index += 1) {
                        const low = art.video.buffered.start(index);
                        const high = art.video.buffered.end(index);
                        if (target < low || target >= high) continue;
                        art.video.currentTime = Math.min(high - 0.01, Math.max(low, target));
                        segment.pendingSeekGlobal = null;
                        segment.pendingSeekLocal = null;
                        art._syncTime?.();
                        art._syncBuffered?.();
                        if (this.resumeAfterSeek) {
                            Promise.resolve(art.play()).catch(error => {
                                if (this.closed || session !== this || error?.name === 'AbortError') return;
                                art.notice.show = error?.name === 'NotAllowedError'
                                    ? '浏览器暂停了自动播放，请点击播放继续'
                                    : '播放失败，请重试';
                            });
                        }
                        break;
                    }
                }
                firstAppend = false;
            };

            try {
                while (!this.closed && session === this) {
                    if (!nextRead) nextRead = reader.read();
                    // Retain the same read when a flush deadline wins. Issuing a
                    // second read here would lose or reorder the next bytes.
                    const result = pendingBytes
                        ? await readBeforeFlushDeadline(nextRead, APPEND_MAX_WAIT_MS - (Date.now() - pendingSince))
                        : await nextRead;
                    if (result === null) {
                        await flush();
                        continue;
                    }
                    nextRead = null;
                    const {done, value} = result;
                    if (done) break;
                    bytes += value.byteLength;
                    if (bytes > MAX_TRACK_BYTES) {
                        segment.partial = true;
                        this.markRuntimeSkip(index, '文件过大，连续流仅保留已缓冲部分');
                        await reader.cancel();
                        break;
                    }
                    // Fetch chunk sizes are browser-controlled and may be several
                    // MiB after backpressure. Bound each individual MSE append.
                    let offset = 0;
                    while (offset < value.byteLength) {
                        const threshold = firstAppend ? FIRST_APPEND_BYTES : APPEND_BATCH_BYTES;
                        const length = Math.min(threshold - pendingBytes, value.byteLength - offset);
                        if (!pendingBytes) pendingSince = Date.now();
                        pending.push(value.subarray(offset, offset + length));
                        pendingBytes += length;
                        offset += length;
                        if (pendingBytes >= threshold) await flush();
                    }
                    if (pendingBytes && Date.now() - pendingSince >= APPEND_MAX_WAIT_MS) await flush();
                }
                await flush();
            } catch (error) {
                if (this.closed || session !== this) throw error;
                segment.partial = true;
            } finally {
                // Cancel partially consumed responses on switch or decoder failure.
                void reader.cancel().catch(() => {});
            }

            const end = this.bufferedEnd();
            if (end <= start + 0.01) {
                this.segments = this.segments.filter(item => item !== segment);
                this.markRuntimeSkip(index, '连续流无法解析该文件 · 自动续播跳过');
                return false;
            }
            segment.end = end;
            segment.duration = Math.max(0, end - start);
            latchPresentationDuration(segment, segment.duration);
            if (segment.partial) {
                this.markRuntimeSkip(index, '连续流读取中断 · 将提前进入下一首');
            }
            art?._syncTime?.();
            art?._syncBuffered?.();
            return true;
        }

        futureSegmentCount() {
            const now = Number(art?.video?.currentTime) || 0;
            return this.segments.filter(segment => segment.end === null || segment.end > now + BOUNDARY_EPSILON).length;
        }

        async ensureLookahead() {
            if (this.appendPromise || this.closed || session !== this) return this.appendPromise;
            this.appendPromise = (async () => {
                let failures = 0;
                while (!this.closed && session === this && this.futureSegmentCount() < LOOKAHEAD_TRACKS) {
                    const index = this.nextAppendIndex();
                    if (index === null) break;
                    const appended = await this.appendTrack(index);
                    if (!appended) {
                        failures += 1;
                        if (failures >= Math.max(1, this.order.length)) break;
                    } else {
                        failures = 0;
                    }
                }
                if (!this.segments.length) throw new Error('no continuous audio track could be appended');
            })();
            try {
                await this.appendPromise;
            } finally {
                this.appendPromise = null;
            }
        }

        async pruneBeforeActive() {
            if (!this.activeSegment || !this.sourceBuffer || this.sourceBuffer.updating || !art?.video) return;
            const globalTime = Number(art.video.currentTime) || 0;
            if (globalTime < this.activeSegment.start + PRUNE_AFTER_SECONDS) return;
            const removeEnd = Math.max(0, this.activeSegment.start - BOUNDARY_EPSILON);
            if (removeEnd <= this.lastPrunedBefore + 0.1) return;
            const ranges = this.sourceBuffer.buffered;
            if (!ranges?.length || ranges.start(0) >= removeEnd) return;
            try {
                const done = waitForEvent(this.sourceBuffer, 'updateend', ['error', 'abort']);
                this.sourceBuffer.remove(0, removeEnd);
                await done;
                this.lastPrunedBefore = removeEnd;
                this.segments = this.segments.filter(segment => segment === this.activeSegment
                    || segment.end === null || segment.end > removeEnd + BOUNDARY_EPSILON);
            } catch (_error) {
                // Pruning is an optimization; playback must not depend on it.
            }
        }

        syncTrackFromVideo() {
            if (this.closed || session !== this || !art?.video) return;
            const globalTime = Number(art.video.currentTime) || 0;
            let candidate = this.activeSegment;
            for (const segment of this.segments) {
                if (globalTime + BOUNDARY_EPSILON < segment.start) break;
                if (segment.end === null || globalTime < segment.end - BOUNDARY_EPSILON) {
                    candidate = segment;
                } else if (globalTime >= segment.end - BOUNDARY_EPSILON) {
                    candidate = segment;
                }
            }
            if (candidate && candidate !== this.activeSegment) {
                const previousIndex = this.activeSegment?.index ?? currentIndex;
                this.activeSegment = candidate;
                activateBusinessTrack(candidate.index, skippedBetween(previousIndex, candidate.index));
                void this.ensureLookahead().catch(error => this.fail(error));
            }
            void this.pruneBeforeActive();
        }

        async start() {
            await this.waitSourceOpen();
            if (this.closed || session !== this) return;
            this.mediaSource.duration = Number.POSITIVE_INFINITY;
            this.sourceBuffer = this.mediaSource.addSourceBuffer(MIME);
            this.sourceBuffer.mode = 'sequence';
            void this.ensureLookahead().catch(error => this.fail(error));
        }

        fail(error) {
            if (this.closed || session !== this) return;
            const index = currentIndex;
            this.stop();
            const media = currentMediaList?.[index];
            if (!media) return;
            legacy.initPlayer(media, index);
            window.setTimeout(() => {
                if (art) art.notice.show = '连续流初始化失败，已回退单曲播放';
            }, 0);
            console.warn('FrontierCloud continuous audio fallback', error);
        }

        stop() {
            if (this.closed) return;
            this.closed = true;
            this.fetchController.abort();
            window.clearTimeout(this.seekTimer);
            if (session === this) session = null;
            if (activeObjectUrl === this.objectUrl) activeObjectUrl = null;
            try { URL.revokeObjectURL(this.objectUrl); } catch (_error) { /* already released */ }
        }
    }

    const baseCurrentTime = Object.getOwnPropertyDescriptor(FrontierMediaPlayer.prototype, 'currentTime');
    const baseDuration = Object.getOwnPropertyDescriptor(FrontierMediaPlayer.prototype, 'duration');
    const baseSyncTime = FrontierMediaPlayer.prototype._syncTime;
    const baseSyncBuffered = FrontierMediaPlayer.prototype._syncBuffered;

    Object.defineProperty(FrontierAudioPlayer.prototype, 'currentTime', {
        configurable: true,
        get() {
            return session?.owns(this) ? session.localTime() : baseCurrentTime.get.call(this);
        },
        set(value) {
            if (session?.owns(this)) session.seekLocal(value);
            else baseCurrentTime.set.call(this, value);
        },
    });
    Object.defineProperty(FrontierAudioPlayer.prototype, 'duration', {
        configurable: true,
        get() {
            return session?.owns(this) ? session.localDuration() : baseDuration.get.call(this);
        },
    });
    FrontierAudioPlayer.prototype._syncTime = function continuousSyncTime() {
        if (session?.owns(this)) session.syncTimeUi(this);
        else baseSyncTime.call(this);
    };
    FrontierAudioPlayer.prototype._syncBuffered = function continuousSyncBuffered() {
        if (session?.owns(this)) session.syncBufferedUi(this);
        else baseSyncBuffered.call(this);
    };

    async function startContinuous(index, options = {}) {
        session?.stop();
        discardNextPreload();
        if (activeObjectUrl) {
            try { URL.revokeObjectURL(activeObjectUrl); } catch (_error) { /* stale object URL */ }
            activeObjectUrl = null;
        }

        const next = new ContinuousAudioSession(index, options);
        session = next;
        const media = currentMediaList[index];
        const sequence = ++playerSwitchSequence;
        currentIndex = index;

        if (art) {
            art.url = next.objectUrl;
            art.title = middleEllipsis(media.title, 54);
        } else {
            art = new FrontierAudioPlayer({
                container: '#artplayer',
                url: next.objectUrl,
                title: middleEllipsis(media.title, 54),
                volume: 0.7,
            });
            bindPlayerBusinessEvents();
        }
        activeObjectUrl = next.objectUrl;
        next.player = art;
        if (!options.preserveBusinessState) activateBusinessTrack(index);

        if (!next.initialSeek) {
            Promise.resolve(art.play()).catch(error => {
                if (sequence !== playerSwitchSequence || error?.name === 'AbortError') return;
                art.notice.show = error?.name === 'NotAllowedError'
                    ? '浏览器暂停了自动播放，请点击播放继续'
                    : '播放失败，请重试';
            });
        }
        try {
            await next.start();
        } catch (error) {
            next.fail(error);
        }
    }

    applyMediaCatalog = function applyContinuousAudioCatalog(entries) {
        annotateCatalog(entries);
        legacy.applyMediaCatalog(entries);
        decoratePlaylist();
    };

    initPlayer = function initContinuousAwarePlayer(media, index) {
        if (!streamCompatible(media)) {
            session?.stop();
            legacy.initPlayer(media, index);
            if (browserSupportsContinuousAudio() && media?.type === 'audio') {
                window.setTimeout(() => showNotice('该曲目仅支持单曲播放，自动续播将跳过'), 0);
            }
            return;
        }
        void startContinuous(index);
    };

    checkAndPreloadNext = function continuousTimeupdate(currentTime) {
        if (session?.owns(art)) {
            session.syncTrackFromVideo();
            return;
        }
        legacy.checkAndPreloadNext(currentTime);
    };

    playNext = function playNextContinuousAware() {
        if (browserSupportsContinuousAudio()) {
            const index = nextCompatibleIndex(currentIndex, 1);
            if (index !== null) {
                selectMedia(index);
                return;
            }
        }
        legacy.playNext();
    };

    playPrev = function playPrevContinuousAware() {
        if (browserSupportsContinuousAudio()) {
            const index = nextCompatibleIndex(currentIndex, -1);
            if (index !== null) {
                selectMedia(index);
                return;
            }
        }
        legacy.playPrev();
    };

    window.frontierCloudContinuousAudio = {
        installed: true,
        mime: MIME,
        lookahead_tracks: LOOKAHEAD_TRACKS,
        max_track_bytes: MAX_TRACK_BYTES,
        append_batch_bytes: APPEND_BATCH_BYTES,
        append_max_wait_ms: APPEND_MAX_WAIT_MS,
        first_append_bytes: FIRST_APPEND_BYTES,
        max_buffer_ahead_seconds: MAX_BUFFER_AHEAD_SECONDS,
        supported: browserSupportsContinuousAudio,
        compatible: streamCompatible,
        status: () => ({
            active: Boolean(session?.owns(art)),
            current_index: currentIndex,
            presentation_duration: session?.localDuration() || 0,
            active_segment: session?.activeSegment ? {...session.activeSegment} : null,
            buffered_tracks: session?.segments?.length || 0,
            buffered_ahead_seconds: session?.bufferedAhead() || 0,
            quota_wait_count: session?.quotaWaitCount || 0,
        }),
    };
})();
