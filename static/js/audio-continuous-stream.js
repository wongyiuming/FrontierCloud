'use strict';

(() => {
    if (typeof PLAYER_KIND === 'undefined' || PLAYER_KIND !== 'audio') return;

    const MIME = 'audio/mpeg';
    const LOOKAHEAD_TRACKS = 2;
    const MAX_TRACK_BYTES = 128 * 1024 * 1024;
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

    function mediaPath(media) {
        return String(media?.media_path || '').split('?', 1)[0].toLowerCase();
    }

    function browserSupportsContinuousAudio() {
        return typeof MediaSource === 'function'
            && typeof MediaSource.isTypeSupported === 'function'
            && MediaSource.isTypeSupported(MIME);
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
            media.continuous_stream_compatible = Boolean(supported && streamCompatible(media));
            media.continuous_stream_skip_reason = supported && media?.type === 'audio' && !streamCompatible(media)
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
        for (const [index, media] of (currentMediaList || []).entries()) {
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

    class ContinuousAudioSession {
        constructor(startIndex) {
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
            this.startIndex = startIndex;
            this.lastPrunedBefore = 0;
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
            return Math.max(0, (Number(art.video.currentTime) || 0) - this.activeSegment.start);
        }

        localDuration() {
            if (!this.activeSegment) return 0;
            const end = Number.isFinite(this.activeSegment.end)
                ? this.activeSegment.end
                : Math.max(this.activeSegment.start, this.bufferedEnd());
            return Math.max(0, end - this.activeSegment.start);
        }

        seekLocal(value) {
            if (!art?.video || !this.activeSegment) return;
            const next = Number(value);
            if (!Number.isFinite(next)) return;
            const duration = this.localDuration();
            const local = duration > 0 ? Math.min(duration, Math.max(0, next)) : Math.max(0, next);
            const target = this.activeSegment.start + local;
            const ranges = art.video.buffered;
            if (ranges?.length) {
                const low = ranges.start(0);
                const high = ranges.end(ranges.length - 1);
                art.video.currentTime = Math.min(high - 0.01, Math.max(low, target));
            } else {
                art.video.currentTime = target;
            }
        }

        syncBufferedUi(player) {
            const duration = this.localDuration();
            if (!duration || !this.activeSegment || !player.video.buffered?.length) {
                player.progressLoaded.style.width = '0%';
                return;
            }
            const end = player.video.buffered.end(player.video.buffered.length - 1);
            const localEnd = Math.max(0, Math.min(duration, end - this.activeSegment.start));
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

        async appendBytes(value) {
            if (this.closed || session !== this || this.generation !== sessionGeneration) throw new Error('stale session');
            await this.waitUpdateEnd();
            const chunk = value.byteOffset === 0 && value.byteLength === value.buffer.byteLength
                ? value.buffer
                : value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength);
            const done = waitForEvent(this.sourceBuffer, 'updateend', ['error', 'abort']);
            this.sourceBuffer.appendBuffer(chunk);
            await done;
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

            let response;
            try {
                response = await fetch(media.url, {cache: 'no-store', credentials: 'same-origin'});
            } catch (_error) {
                this.markRuntimeSkip(index, '连续流读取失败 · 自动续播跳过');
                return false;
            }
            if (!response.ok || !response.body) {
                this.markRuntimeSkip(index, '连续流读取失败 · 自动续播跳过');
                return false;
            }

            const declared = Number(response.headers.get('Content-Length') || 0);
            if (declared > MAX_TRACK_BYTES) {
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
            const segment = {index, start, end: null, partial: false};
            this.segments.push(segment);
            const reader = response.body.getReader();
            let bytes = 0;
            try {
                while (!this.closed && session === this) {
                    const {done, value} = await reader.read();
                    if (done) break;
                    bytes += value.byteLength;
                    if (bytes > MAX_TRACK_BYTES) {
                        segment.partial = true;
                        this.markRuntimeSkip(index, '文件过大，连续流仅保留已缓冲部分');
                        await reader.cancel();
                        break;
                    }
                    await this.appendBytes(value);
                }
            } catch (error) {
                if (this.closed || session !== this) throw error;
                segment.partial = true;
            }

            const end = this.bufferedEnd();
            if (end <= start + 0.01) {
                this.segments = this.segments.filter(item => item !== segment);
                this.markRuntimeSkip(index, '连续流无法解析该文件 · 自动续播跳过');
                return false;
            }
            segment.end = end;
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
            await this.ensureLookahead();
            if (!this.activeSegment) this.activeSegment = this.segments[0];
            if (this.activeSegment) activateBusinessTrack(this.activeSegment.index);
            art?._syncTime?.();
            art?._syncBuffered?.();
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
            if (session === this) session = null;
            if (activeObjectUrl === this.objectUrl) activeObjectUrl = null;
            try { URL.revokeObjectURL(this.objectUrl); } catch (_error) { /* already released */ }
        }
    }

    const baseCurrentTime = Object.getOwnPropertyDescriptor(FrontierMediaPlayer.prototype, 'currentTime');
    const baseDuration = Object.getOwnPropertyDescriptor(FrontierMediaPlayer.prototype, 'duration');
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
    FrontierAudioPlayer.prototype._syncBuffered = function continuousSyncBuffered() {
        if (session?.owns(this)) session.syncBufferedUi(this);
        else baseSyncBuffered.call(this);
    };

    async function startContinuous(index) {
        session?.stop();
        discardNextPreload();
        if (activeObjectUrl) {
            try { URL.revokeObjectURL(activeObjectUrl); } catch (_error) { /* stale object URL */ }
            activeObjectUrl = null;
        }

        const next = new ContinuousAudioSession(index);
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
        activateBusinessTrack(index);

        Promise.resolve(art.play()).catch(error => {
            if (sequence !== playerSwitchSequence || error?.name === 'AbortError') return;
            art.notice.show = error?.name === 'NotAllowedError'
                ? '浏览器暂停了自动播放，请点击播放继续'
                : '播放失败，请重试';
        });
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

    renderPlaylist = function renderContinuousAudioPlaylist() {
        legacy.renderPlaylist();
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
        supported: browserSupportsContinuousAudio,
        compatible: streamCompatible,
        status: () => ({
            active: Boolean(session?.owns(art)),
            current_index: currentIndex,
            active_segment: session?.activeSegment ? {...session.activeSegment} : null,
            buffered_tracks: session?.segments?.length || 0,
        }),
    };
})();
