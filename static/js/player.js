let art = null;
let currentIndex = 0;
let nextPreload = null;
let activeObjectUrl = null;
let playerSwitchSequence = 0;
let clickTimer = null;
let playbackState = null;
let playbackReporter = null;
let inlineLyricsRequest = null;
let inlineLyricsSequence = 0;
let remoteRetrySequence = -1;
const inlineLyricsCache = new Map();
let activeLyricEntries = [];
let activeLyricIndex = null;
let lyricSlideTimer = null;
let lyricAnimationFrame = null;
let fullscreenLyricIndex = null;
const LYRIC_SLIDE_MS = 480;
const LYRIC_WINDOW_OFFSETS = [-1, 0, 1, 2, 3];
const DIRECT_SEEK_ZONE_START = 0.75;
const PRELOAD_MAX_BYTES = 128 * 1024 * 1024;
const PRELOAD_START_SECONDS = 5;


const PLAYER_ICONS = {
    play: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>',
    pause: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 5h4v14H6zm8 0h4v14h-4z"/></svg>',
    volume: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 9v6h4l5 4V5L7 9H3zm12.5 3a3.5 3.5 0 0 0-1.5-2.87v5.74A3.5 3.5 0 0 0 15.5 12zm0-6.33v2.06a5.5 5.5 0 0 1 0 8.54v2.06a7.5 7.5 0 0 0 0-12.66z"/></svg>',
    muted: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 9v6h4l5 4V5L7 9H3zm13.6 3 2.7-2.7-1.4-1.4-2.7 2.7-2.7-2.7-1.4 1.4 2.7 2.7-2.7 2.7 1.4 1.4 2.7-2.7 2.7 2.7 1.4-1.4z"/></svg>',
    webFullscreen: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h7v2H6v5H4zm9 0h7v7h-2V7h-5zM4 14h2v3h5v2H4zm14 0h2v5h-7v-2h5z"/></svg>',
    fullscreen: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 5h6v2H7v4H5zm8 0h6v6h-2V7h-4zM5 13h2v4h4v2H5zm12 0h2v6h-6v-2h4z"/></svg>',
};

function playerNoticeState(element) {
    let value = '';
    return Object.defineProperty({}, 'show', {
        get: () => value,
        set: next => {
            value = next ? String(next) : '';
            element.textContent = value;
            element.hidden = !value;
        },
    });
}

function playerVisibilityState(element, display = '') {
    let visible = !element.hidden;
    return Object.defineProperty({}, 'show', {
        get: () => visible,
        set: value => {
            visible = Boolean(value);
            element.hidden = !visible;
            if (visible && display) element.style.display = display;
            else if (!visible && display) element.style.removeProperty('display');
        },
    });
}

class FrontierMediaPlayer {
    constructor(options) {
        this.container = typeof options.container === 'string'
            ? document.querySelector(options.container) : options.container;
        if (!this.container) throw new Error('Player container not found');
        this._events = new Map();
        this._url = '';
        this._title = '';
        this._hideTimer = null;
        this._draggingProgress = false;
        this._webFullscreen = false;
        this._playRequested = false;
        this.container.replaceChildren();
        this.container.insertAdjacentHTML('beforeend', `
            <div class="art-video-player">
                <video class="art-video" playsinline webkit-playsinline preload="metadata"></video>
                <div class="art-title" aria-live="polite"></div>
                <button type="button" class="art-mask art-center-play" aria-label="播放">${PLAYER_ICONS.play}</button>
                <div class="art-loading" hidden aria-label="加载中"><span></span></div>
                <div class="art-notice" hidden role="status"></div>
                <div class="art-bottom">
                    <div class="art-progress">
                        <div class="art-control-progress" role="slider" aria-label="播放进度" tabindex="0">
                            <div class="art-control-progress-inner">
                                <div class="art-progress-loaded"></div>
                                <div class="art-progress-played"></div>
                                <span class="art-progress-indicator"></span>
                            </div>
                        </div>
                    </div>
                    <div class="art-controls">
                        <div class="art-controls-left">
                            <button type="button" class="art-control art-control-play" aria-label="播放">${PLAYER_ICONS.play}</button>
                            <span class="art-time"><span class="art-current">0:00</span><span class="art-time-separator"> / </span><span class="art-duration">0:00</span></span>
                            <div class="art-volume">
                                <button type="button" class="art-control art-control-volume" aria-label="静音">${PLAYER_ICONS.volume}</button>
                                <input class="art-volume-range" type="range" min="0" max="1" step="0.01" aria-label="音量">
                            </div>
                        </div>
                        <div class="art-controls-right">
                            <button type="button" class="art-control art-control-web-fullscreen" aria-label="网页全屏">${PLAYER_ICONS.webFullscreen}</button>
                            <button type="button" class="art-control art-control-fullscreen" aria-label="全屏">${PLAYER_ICONS.fullscreen}</button>
                        </div>
                    </div>
                </div>
            </div>
        `);
        this.root = this.container.querySelector('.art-video-player');
        this.video = this.container.querySelector('.art-video');
        this.titleElement = this.container.querySelector('.art-title');
        this.maskElement = this.container.querySelector('.art-mask');
        this.loadingElement = this.container.querySelector('.art-loading');
        this.noticeElement = this.container.querySelector('.art-notice');
        this.bottomElement = this.container.querySelector('.art-bottom');
        this.playButton = this.container.querySelector('.art-control-play');
        this.currentElement = this.container.querySelector('.art-current');
        this.durationElement = this.container.querySelector('.art-duration');
        this.progressControl = this.container.querySelector('.art-control-progress');
        this.progressLoaded = this.container.querySelector('.art-progress-loaded');
        this.progressPlayed = this.container.querySelector('.art-progress-played');
        this.progressIndicator = this.container.querySelector('.art-progress-indicator');
        this.volumeButton = this.container.querySelector('.art-control-volume');
        this.volumeRange = this.container.querySelector('.art-volume-range');
        this.webFullscreenButton = this.container.querySelector('.art-control-web-fullscreen');
        this.fullscreenButton = this.container.querySelector('.art-control-fullscreen');

        this.notice = playerNoticeState(this.noticeElement);
        this.loading = playerVisibilityState(this.loadingElement, 'grid');
        this.mask = playerVisibilityState(this.maskElement, 'grid');
        this.controls = playerVisibilityState(this.bottomElement);

        this.video.volume = Number.isFinite(Number(options.volume)) ? Number(options.volume) : 0.7;
        this.volumeRange.value = String(this.video.volume);
        this._bindNativeEvents();
        this._bindControls();
        this.title = options.title || '';
        this.url = options.url || '';
        this._syncVolume();
        this._syncPlaybackUi();
    }

    _bindNativeEvents() {
        const forward = {
            play: 'play', pause: 'pause', timeupdate: 'video:timeupdate',
            error: 'video:error', ended: 'video:ended', waiting: 'video:waiting',
            playing: 'video:playing', seeking: 'video:seeking', seeked: 'video:seeked',
            loadedmetadata: 'video:loadedmetadata', canplay: 'video:canplay',
            volumechange: 'video:volumechange', progress: 'video:progress',
        };
        for (const [nativeName, playerName] of Object.entries(forward)) {
            this.video.addEventListener(nativeName, event => this._emit(playerName, event));
        }
        this.video.addEventListener('loadstart', () => { this.loading.show = true; this.controls.show = true; });
        this.video.addEventListener('waiting', () => { this.loading.show = true; this.controls.show = true; });
        this.video.addEventListener('seeking', () => { this.loading.show = true; this.controls.show = true; });
        this.video.addEventListener('canplay', () => { this.loading.show = false; this._syncTime(); });
        this.video.addEventListener('playing', () => { this.loading.show = false; this.notice.show = false; this._syncPlaybackUi(); this._scheduleControlsHide(); });
        this.video.addEventListener('play', () => { this._syncPlaybackUi(); this.controls.show = true; });
        this.video.addEventListener('pause', () => { this._syncPlaybackUi(); this._showControls(); });
        this.video.addEventListener('ended', () => { this._playRequested = false; this._syncPlaybackUi(); this._showControls(); });
        this.video.addEventListener('timeupdate', () => this._syncTime());
        this.video.addEventListener('durationchange', () => this._syncTime());
        this.video.addEventListener('progress', () => this._syncBuffered());
        this.video.addEventListener('volumechange', () => this._syncVolume());
        this.video.addEventListener('error', () => {
            this.loading.show = false;
            this.mask.show = true;
            this.controls.show = true;
            this.notice.show = '播放失败，请重试';
        });
        document.addEventListener('fullscreenchange', () => this._syncFullscreenState());
    }

    _bindControls() {
        const togglePlayback = event => {
            event?.stopPropagation();
            if (this.video.paused || this.video.ended) this.play().catch(error => this._showPlayError(error));
            else this.pause();
        };
        this.playButton.addEventListener('click', togglePlayback);
        this.maskElement.addEventListener('click', togglePlayback);
        this.video.addEventListener('click', togglePlayback);
        this.video.addEventListener('dblclick', event => {
            event.preventDefault();
            event.stopPropagation();
            this.toggleFullscreen();
        });
        this.volumeButton.addEventListener('click', event => {
            event.stopPropagation();
            this.video.muted = !this.video.muted;
        });
        this.volumeRange.addEventListener('input', event => {
            event.stopPropagation();
            this.video.volume = Number(this.volumeRange.value);
            if (this.video.volume > 0) this.video.muted = false;
        });
        this.webFullscreenButton.addEventListener('click', event => {
            event.stopPropagation();
            this.toggleWebFullscreen();
        });
        this.fullscreenButton.addEventListener('click', event => {
            event.stopPropagation();
            this.toggleFullscreen();
        });
        this.progressControl.addEventListener('pointerdown', event => {
            if (event.button !== undefined && event.button !== 0) return;
            this._draggingProgress = true;
            this.progressControl.setPointerCapture?.(event.pointerId);
            this._seekFromClientX(event.clientX);
            event.preventDefault();
        });
        this.progressControl.addEventListener('pointermove', event => {
            if (!this._draggingProgress) return;
            this._seekFromClientX(event.clientX);
            event.preventDefault();
        });
        const finishProgress = event => {
            if (!this._draggingProgress) return;
            this._draggingProgress = false;
            this.progressControl.releasePointerCapture?.(event.pointerId);
            event.preventDefault();
        };
        this.progressControl.addEventListener('pointerup', finishProgress);
        this.progressControl.addEventListener('pointercancel', finishProgress);
        this.progressControl.addEventListener('keydown', event => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            event.preventDefault();
            if (event.key === 'Home') this.currentTime = 0;
            else if (event.key === 'End') this.currentTime = this.duration;
            else this.currentTime = Math.min(this.duration, Math.max(0, this.currentTime + (event.key === 'ArrowRight' ? 5 : -5)));
        });
        this.root.addEventListener('mousemove', () => this._showControls());
        this.root.addEventListener('mouseleave', () => this._scheduleControlsHide(500));
        this.root.addEventListener('touchstart', () => this._showControls(), {passive: true});
    }

    _emit(name, event) {
        for (const handler of [...(this._events.get(name) || [])]) handler(event);
    }

    on(name, handler) {
        if (!this._events.has(name)) this._events.set(name, new Set());
        this._events.get(name).add(handler);
        return this;
    }

    off(name, handler) {
        if (!handler) this._events.delete(name);
        else this._events.get(name)?.delete(handler);
        return this;
    }

    _showPlayError(error) {
        if (error?.name === 'AbortError') return;
        this.notice.show = error?.name === 'NotAllowedError'
            ? '浏览器暂停了自动播放，请点击播放继续' : '播放失败，请重试';
        this.mask.show = true;
        this.controls.show = true;
    }

    _syncPlaybackUi() {
        const playing = !this.video.paused && !this.video.ended;
        this.playButton.innerHTML = playing ? PLAYER_ICONS.pause : PLAYER_ICONS.play;
        this.playButton.setAttribute('aria-label', playing ? '暂停' : '播放');
        this.mask.show = !playing;
    }

    _syncTime() {
        const duration = this.duration;
        const current = Math.min(this.currentTime, duration || this.currentTime);
        this.currentElement.textContent = formatTime(current);
        this.durationElement.textContent = formatTime(duration);
        const ratio = duration > 0 ? Math.min(1, Math.max(0, current / duration)) : 0;
        const percent = `${ratio * 100}%`;
        this.progressPlayed.style.width = percent;
        this.progressIndicator.style.left = percent;
        this.progressControl.setAttribute('aria-valuemin', '0');
        this.progressControl.setAttribute('aria-valuemax', String(duration || 0));
        this.progressControl.setAttribute('aria-valuenow', String(current || 0));
    }

    _syncBuffered() {
        const duration = this.duration;
        if (!duration || !this.video.buffered?.length) {
            this.progressLoaded.style.width = '0%';
            return;
        }
        const end = this.video.buffered.end(this.video.buffered.length - 1);
        this.progressLoaded.style.width = `${Math.min(100, Math.max(0, end / duration * 100))}%`;
    }

    _syncVolume() {
        this.volumeRange.value = String(this.video.muted ? 0 : this.video.volume);
        this.volumeButton.innerHTML = this.video.muted || this.video.volume === 0 ? PLAYER_ICONS.muted : PLAYER_ICONS.volume;
        this.volumeButton.setAttribute('aria-label', this.video.muted ? '取消静音' : '静音');
    }

    _seekFromClientX(clientX) {
        const duration = this.duration;
        if (!duration) return;
        const rect = this.progressControl.getBoundingClientRect();
        const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / Math.max(1, rect.width)));
        this.currentTime = duration * ratio;
    }

    _showControls() {
        clearTimeout(this._hideTimer);
        this.controls.show = true;
        this._scheduleControlsHide();
    }

    _scheduleControlsHide(delay = 2500) {
        clearTimeout(this._hideTimer);
        if (this.video.paused || this.video.ended || this.video.readyState < 2) return;
        this._hideTimer = setTimeout(() => { this.controls.show = false; }, delay);
    }

    async toggleFullscreen() {
        try {
            if (document.fullscreenElement === this.container) await document.exitFullscreen();
            else if (this.container.requestFullscreen) await this.container.requestFullscreen();
            else if (this.video.webkitEnterFullscreen) this.video.webkitEnterFullscreen();
        } catch (_error) {
            this.notice.show = '当前浏览器无法进入全屏';
        }
    }

    toggleWebFullscreen() {
        this._webFullscreen = !this._webFullscreen;
        this.container.classList.toggle('art-fullscreen-web', this._webFullscreen);
        document.body.classList.toggle('player-web-fullscreen', this._webFullscreen);
    }

    _syncFullscreenState() {
        this.container.classList.toggle('art-fullscreen', document.fullscreenElement === this.container);
    }

    play() {
        this._playRequested = true;
        const result = this.video.play();
        return result.catch(error => {
            if (error?.name === 'NotAllowedError') this._playRequested = false;
            throw error;
        });
    }
    pause() { this._playRequested = false; this.video.pause(); }

    get url() { return this._url; }
    set url(value) {
        const url = String(value || '');
        if (url === this._url && this.video.getAttribute('src') === url) return;
        this._url = url;
        this.video.src = url;
        this.video.load();
    }

    get currentTime() { return Number(this.video.currentTime) || 0; }
    set currentTime(value) {
        const next = Number(value);
        if (Number.isFinite(next)) this.video.currentTime = Math.max(0, next);
    }
    get duration() { return Number.isFinite(this.video.duration) ? this.video.duration : 0; }
    get playing() { return !this.video.paused && !this.video.ended; }
    get playRequested() { return this._playRequested; }
    get title() { return this._title; }
    set title(value) {
        this._title = String(value || '');
        if (this.titleElement) this.titleElement.textContent = this._title;
    }
}

class FrontierAudioPlayer extends FrontierMediaPlayer {
    constructor(options) {
        super(options);
        this.root.classList.add('frontier-audio-player');
        this.controls.show = true;
    }

    _scheduleControlsHide() {
        clearTimeout(this._hideTimer);
        this.controls.show = true;
    }
}

class FrontierVideoPlayer extends FrontierMediaPlayer {
    constructor(options) {
        super(options);
        this.root.classList.add('frontier-video-player');
    }
}

function nextMediaIndex() {
    return (currentIndex + 1) % currentMediaList.length;
}

function discardNextPreload() {
    if (!nextPreload) return;
    nextPreload.controller?.abort();
    if (nextPreload.objectUrl) URL.revokeObjectURL(nextPreload.objectUrl);
    nextPreload = null;
}

async function preloadMedia(entry) {
    const timeout = setTimeout(() => entry.controller.abort(), 120000);
    try {
        const response = await fetch(entry.url, {signal: entry.controller.signal});
        if (!response.ok || response.status === 206) throw new Error('Incomplete preload response');
        if (Number(response.headers.get('Content-Length')) > PRELOAD_MAX_BYTES) {
            entry.tooLarge = true;
            throw new Error('Preload memory limit exceeded');
        }
        const reader = response.body.getReader();
        const chunks = [];
        let bytes = 0;
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            bytes += value.byteLength;
            if (bytes > PRELOAD_MAX_BYTES) {
                entry.tooLarge = true;
                throw new Error('Preload memory limit exceeded');
            }
            chunks.push(value);
        }
        if (!bytes) throw new Error('Empty preload response');
        if (nextPreload !== entry) return;
        entry.objectUrl = URL.createObjectURL(new Blob(chunks, {
            type: response.headers.get('Content-Type') || 'application/octet-stream',
        }));
        entry.status = 'ready';
    } catch (_error) {
        entry.controller.abort();
        if (nextPreload !== entry) return;
        entry.status = entry.tooLarge ? 'skipped' : 'failed';
        entry.retryAt = performance.now() + Math.min(30000, 3000 * (2 ** entry.attempt));
    } finally {
        clearTimeout(timeout);
    }
}

// Escape HTML globally to prevent DOM-based XSS.
function escapeHTML(str) {
    if (typeof str !== 'string') return str;
    return str.replace(/[&<>'\"]/g, tag => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        "'": '&#39;',
        '"': '&quot;'
    }[tag] || tag));
}

function middleEllipsis(value, maximum = 40) {
    const characters = Array.from(String(value || ''));
    if (characters.length <= maximum) return characters.join('');
    const head = Math.ceil((maximum - 3) / 2);
    const tail = Math.floor((maximum - 3) / 2);
    return `${characters.slice(0, head).join('')}...${characters.slice(-tail).join('')}`;
}

function updateMediaSession(media) {
    if ('mediaSession' in navigator) {
        navigator.mediaSession.metadata = new MediaMetadata({
            title: media.title || '未知曲目',
            artist: media.artist || '前沿娱乐',
            album: typeof PAGE_TITLE !== 'undefined' ? PAGE_TITLE : '前沿娱乐',
            artwork: [
                { src: media.cover, sizes: '512x512', type: 'image/png' }
            ]
        });

        navigator.mediaSession.setActionHandler('play', () => { if (art) art.play(); });
        navigator.mediaSession.setActionHandler('pause', () => { if (art) art.pause(); });
        navigator.mediaSession.setActionHandler('previoustrack', () => { playPrev(); });
        navigator.mediaSession.setActionHandler('nexttrack', () => { playNext(); });
    }
}

function checkAndPreloadNext(currentTime) {
    if (!currentMediaList || currentMediaList.length <= 1) return;
    const duration = Number(art?.duration || 0);
    const start = duration > 0 ? Math.min(PRELOAD_START_SECONDS, duration / 4) : PRELOAD_START_SECONDS;
    if (currentTime < start) return;
    const next = currentMediaList[nextMediaIndex()];
    // Keep speculative memory bounded; video continues to use normal streaming.
    if (next.type !== 'audio') return;
    let attempt = 0;
    if (nextPreload?.url === next.url) {
        if (nextPreload.status !== 'failed' || performance.now() < nextPreload.retryAt) return;
        attempt = Math.min(nextPreload.attempt + 1, 4);
    }
    discardNextPreload();
    nextPreload = {url: next.url, status: 'loading', controller: new AbortController(), attempt};
    void preloadMedia(nextPreload);
}

function playNext() {
    if (!currentMediaList || currentMediaList.length === 0) return;
    const nextIndex = nextMediaIndex();
    selectMedia(nextIndex);
}

function playPrev() {
    if (!currentMediaList || currentMediaList.length === 0) return;
    const prevIndex = (currentIndex - 1 + currentMediaList.length) % currentMediaList.length;
    selectMedia(prevIndex);
}

function playbackThreshold(duration) {
    return Math.max(5, Math.min(30, duration * 0.5));
}

function resetPlaybackAccounting(media) {
    playbackState = {
        mediaId: media.media_id,
        accumulated: 0,
        lastTick: null,
        reporting: false,
        reported: false,
    };
}

function accountPlaybackTime() {
    if (!art || !playbackState) return;
    const now = performance.now();
    const playing = art.playing === true || (art.video && !art.video.paused);
    if (playing && playbackState.lastTick !== null) {
        const elapsed = (now - playbackState.lastTick) / 1000;
        if (elapsed > 0 && elapsed <= 2.5) playbackState.accumulated += elapsed;
    }
    playbackState.lastTick = playing ? now : null;
}

async function reportValidPlayback() {
    accountPlaybackTime();
    if (!art || !playbackState || playbackState.reporting || playbackState.reported) return;
    const media = currentMediaList[currentIndex];
    const duration = Number(art.duration || 0);
    if (!media || !duration || playbackState.mediaId !== media.media_id) return;
    if (playbackState.accumulated + 0.05 < playbackThreshold(duration)) return;

    const reportingState = playbackState;
    reportingState.reporting = true;
    try {
        const response = await fetch('/api/v1/media/playback', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                media_path: media.media_path,
                resource_id: media.resource_id || null,
                playback_session_id: playbackSessionId,
                played_seconds: reportingState.accumulated,
                duration,
            }),
        });
        if (!response.ok) return;
        const data = await response.json();
        reportingState.reported = true;
        media.play_score = data.play_score;
    } catch (_error) {
        // Playback remains available while transient accounting failures retry.
    } finally {
        reportingState.reporting = false;
    }
}

function lyricIndexAt(entries, currentTime) {
    let low = 0;
    let high = entries.length - 1;
    let active = -1;
    while (low <= high) {
        const middle = Math.floor((low + high) / 2);
        if (entries[middle].time <= currentTime + 0.03) {
            active = middle;
            low = middle + 1;
        } else {
            high = middle - 1;
        }
    }
    return active;
}

function sizeSynchronizedLyrics() {
    const panel = document.getElementById('inlineLyrics');
    const track = document.getElementById('inlineLyricsTrack');
    if (!panel || !track || activeLyricEntries.length === 0) return;
    const visible = Array.from(track.children).map(row => row.textContent || '');
    const longest = Math.max(1, ...visible.map(line => Array.from(line).length));
    const heightSize = Math.max(18, panel.clientHeight / 4.9);
    const widthSize = Math.max(18, (panel.clientWidth - 40) / Math.max(6, longest * 1.02));
    panel.style.setProperty('--sync-lyric-font-size', `${Math.max(18, Math.min(58, heightSize, widthSize))}px`);
}

function lyricRowClass(offset) {
    if (offset <= -2) return 'sync-lyric departing';
    if (offset === -1) return 'sync-lyric previous';
    if (offset === 0) return 'sync-lyric current';
    if (offset === 1) return 'sync-lyric next';
    if (offset === 2) return 'sync-lyric next far';
    return 'sync-lyric trailing';
}

function clearLyricSlide() {
    if (lyricSlideTimer !== null) clearTimeout(lyricSlideTimer);
    lyricSlideTimer = null;
}

function setActiveLyricTime(panel, index) {
    const cue = activeLyricEntries[index];
    if (cue) panel.setAttribute('data-active-time', String(cue.time));
    else panel.removeAttribute('data-active-time');
}

function sizeFullscreenLyrics() {
    const overlay = document.getElementById('fullscreenLyrics');
    if (!overlay || overlay.classList.contains('hidden') || activeLyricEntries.length === 0) return;
    const rowsPerColumn = Math.max(1, Math.ceil(activeLyricEntries.length / 3));
    const longest = Math.max(1, ...activeLyricEntries.map(entry => Array.from(entry.text || '').length));
    const heightSize = Math.max(10, (overlay.clientHeight - 80) / (rowsPerColumn * 1.22));
    const widthSize = Math.max(10, (overlay.clientWidth / 3 - 56) / Math.max(4, longest * 1.02));
    overlay.style.setProperty('--fullscreen-lyric-font-size', `${Math.max(10, Math.min(48, heightSize, widthSize))}px`);
}

function syncFullscreenLyrics(index) {
    const overlay = document.getElementById('fullscreenLyrics');
    if (!overlay || overlay.classList.contains('hidden') || index === fullscreenLyricIndex) return;
    overlay.querySelector('.fullscreen-lyric-line.current')?.classList.remove('current');
    if (index >= 0) overlay.querySelector(`[data-lyric-index="${index}"]`)?.classList.add('current');
    fullscreenLyricIndex = index;
}

function renderFullscreenLyrics() {
    const columns = [0, 1, 2].map(index => document.getElementById(`fullscreenLyricsColumn${index}`));
    if (columns.some(column => !column)) return;
    columns.forEach(column => column.replaceChildren());
    const rowsPerColumn = Math.max(1, Math.ceil(activeLyricEntries.length / columns.length));
    activeLyricEntries.forEach((entry, index) => {
        const row = document.createElement('p');
        row.className = 'fullscreen-lyric-line';
        row.dataset.lyricIndex = String(index);
        row.textContent = entry.text || '\u00a0';
        columns[Math.min(columns.length - 1, Math.floor(index / rowsPerColumn))].appendChild(row);
    });
    fullscreenLyricIndex = null;
    syncFullscreenLyrics(activeLyricIndex ?? -1);
    sizeFullscreenLyrics();
}

function closeFullscreenLyrics() {
    const overlay = document.getElementById('fullscreenLyrics');
    if (!overlay) return;
    overlay.classList.add('hidden');
    overlay.setAttribute('aria-hidden', 'true');
    fullscreenLyricIndex = null;
}

function openFullscreenLyrics() {
    const overlay = document.getElementById('fullscreenLyrics');
    if (!overlay || activeLyricEntries.length === 0) return;
    renderFullscreenLyrics();
    overlay.classList.remove('hidden');
    overlay.setAttribute('aria-hidden', 'false');
    syncFullscreenLyrics(activeLyricIndex ?? -1);
    sizeFullscreenLyrics();
}

function renderLyricWindow(index) {
    const panel = document.getElementById('inlineLyrics');
    const track = document.getElementById('inlineLyricsTrack');
    if (!panel || !track) return;
    clearLyricSlide();
    track.classList.remove('sliding');
    track.style.transform = 'translateY(0)';
    const rows = LYRIC_WINDOW_OFFSETS.map(offset => {
        const row = document.createElement('p');
        const entry = activeLyricEntries[index + offset];
        row.className = lyricRowClass(offset);
        row.textContent = entry?.text || (offset === 0 ? '♪' : '\u00a0');
        return row;
    });
    track.replaceChildren(...rows);
    activeLyricIndex = index;
    setActiveLyricTime(panel, index);
    sizeSynchronizedLyrics();
}

function animateLyricForward(nextIndex) {
    const panel = document.getElementById('inlineLyrics');
    const track = document.getElementById('inlineLyricsTrack');
    if (!panel || !track || track.children.length !== LYRIC_WINDOW_OFFSETS.length) {
        renderLyricWindow(nextIndex);
        return;
    }
    clearLyricSlide();
    Array.from(track.children).forEach((row, rowIndex) => {
        row.className = lyricRowClass(LYRIC_WINDOW_OFFSETS[rowIndex] - 1);
    });
    activeLyricIndex = nextIndex;
    setActiveLyricTime(panel, nextIndex);
    void track.offsetWidth;
    track.classList.add('sliding');
    track.style.transform = 'translateY(-20%)';
    lyricSlideTimer = setTimeout(() => renderLyricWindow(nextIndex), LYRIC_SLIDE_MS + 30);
}

function updateSynchronizedLyrics(currentTime, force = false) {
    const panel = document.getElementById('inlineLyrics');
    const track = document.getElementById('inlineLyricsTrack');
    if (!panel || !track) return;
    panel.classList.toggle('hidden', activeLyricEntries.length === 0);
    if (activeLyricEntries.length === 0) {
        clearLyricSlide();
        activeLyricIndex = null;
        panel.removeAttribute('data-active-time');
        track.classList.remove('sliding');
        track.style.transform = 'translateY(0)';
        track.replaceChildren();
        return;
    }
    const nextIndex = lyricIndexAt(activeLyricEntries, Number(currentTime) || 0);
    syncFullscreenLyrics(nextIndex);
    if (!force && nextIndex === activeLyricIndex) return;
    if (lyricSlideTimer !== null) renderLyricWindow(activeLyricIndex);
    if (!force && nextIndex === activeLyricIndex + 1) animateLyricForward(nextIndex);
    else renderLyricWindow(nextIndex);
}

function showSynchronizedLyrics(entries, currentTime = 0) {
    activeLyricEntries = entries;
    activeLyricIndex = null;
    if (entries.length === 0) closeFullscreenLyrics();
    else if (!document.getElementById('fullscreenLyrics')?.classList.contains('hidden')) renderFullscreenLyrics();
    updateSynchronizedLyrics(currentTime, true);
}

function stopLyricClock() {
    if (lyricAnimationFrame !== null) cancelAnimationFrame(lyricAnimationFrame);
    lyricAnimationFrame = null;
}

function startLyricClock() {
    if (typeof PLAYER_KIND === 'undefined' || PLAYER_KIND !== 'audio' || lyricAnimationFrame !== null) return;
    const tick = () => {
        if (!art || !art.playing) {
            lyricAnimationFrame = null;
            return;
        }
        updateSynchronizedLyrics(art.currentTime);
        lyricAnimationFrame = requestAnimationFrame(tick);
    };
    lyricAnimationFrame = requestAnimationFrame(tick);
}

async function loadInlineLyrics(media) {
    const sequence = ++inlineLyricsSequence;
    inlineLyricsRequest?.abort();
    inlineLyricsRequest = null;
    if (!media.has_lyrics) {
        showSynchronizedLyrics([]);
        return;
    }
    const lyricIdentity = media.resource_id || media.media_id || media.media_path;
    const resourceQuery = media.resource_id ? `&resource_id=${encodeURIComponent(media.resource_id)}` : '';
    const cached = inlineLyricsCache.get(lyricIdentity);
    if (cached) {
        showSynchronizedLyrics(cached, art?.currentTime || 0);
        return;
    }
    const controller = new AbortController();
    inlineLyricsRequest = controller;
    try {
        const response = await fetch(`/api/v1/media/lyrics/content?track=${encodeURIComponent(media.media_path)}${resourceQuery}`, {
            cache: 'no-store',
            signal: controller.signal,
        });
        if (!response.ok) throw new Error('Lyrics unavailable');
        const data = await response.json();
        const entries = Array.isArray(data.entries) ? data.entries
            .map(entry => ({time: Number(entry?.time), text: String(entry?.text || '')}))
            .filter(entry => Number.isFinite(entry.time) && entry.time >= 0 && entry.text)
            .sort((left, right) => left.time - right.time) : [];
        inlineLyricsCache.set(lyricIdentity, entries);
        if (sequence === inlineLyricsSequence) showSynchronizedLyrics(entries, art?.currentTime || 0);
    } catch (error) {
        if (error.name !== 'AbortError' && sequence === inlineLyricsSequence) showSynchronizedLyrics([]);
    } finally {
        if (inlineLyricsRequest === controller) inlineLyricsRequest = null;
    }
}

function bindPlayerBusinessEvents() {
    art.on('play', () => {
        remoteRetrySequence = -1;
        if (playbackState) playbackState.lastTick = performance.now();
        if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
        startLyricClock();
    });

    art.on('pause', () => {
        accountPlaybackTime();
        if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
        stopLyricClock();
    });

    art.on('video:timeupdate', () => {
        checkAndPreloadNext(art.currentTime);
        if (typeof PLAYER_KIND !== 'undefined' && PLAYER_KIND === 'audio') {
            updateSynchronizedLyrics(art.currentTime);
            startLyricClock();
        }
        reportValidPlayback();
    });

    art.on('video:error', async () => {
        const media = currentMediaList[currentIndex];
        const sequence = playerSwitchSequence;
        const video = art.video;
        if (!media?.resource_id || activeObjectUrl || !video) return;
        if (remoteRetrySequence === sequence) {
            art.loading.show = false;
            art.mask.show = true;
            art.controls.show = true;
            art.notice.show = '媒体暂不可用，请稍后重试';
            return;
        }
        remoteRetrySequence = sequence;
        const position = video.currentTime;
        const resume = art.playRequested;
        video.src = media.url;
        video.load();
        const restore = () => {
            if (playerSwitchSequence !== sequence) return;
            video.currentTime = Math.min(position, Math.max(0, video.duration - 0.1));
            if (resume) video.play().catch(() => { art.notice.show = '请点击播放继续'; });
        };
        video.addEventListener('loadedmetadata', restore, {once: true});
        art.notice.show = '正在重新连接媒体';
    });

    art.on('video:ended', () => {
        stopLyricClock();
        playNext();
    });
}

function initPlayer(media, index) {
    accountPlaybackTime();
    currentIndex = index;
    const sequence = ++playerSwitchSequence;
    const previousObjectUrl = activeObjectUrl;
    activeObjectUrl = nextPreload?.url === media.url && nextPreload.status === 'ready'
        ? nextPreload.objectUrl : null;
    if (activeObjectUrl) nextPreload.objectUrl = null;
    discardNextPreload();
    const playbackUrl = activeObjectUrl || media.url;
    resetPlaybackAccounting(media);
    const lyricsLink = document.getElementById('lyricsLink');

    showSynchronizedLyrics([]);
    void loadInlineLyrics(media);

    if (lyricsLink) {
        lyricsLink.classList.toggle('unavailable', !media.has_lyrics);
        lyricsLink.disabled = !media.has_lyrics;
        lyricsLink.setAttribute('aria-label', media.has_lyrics ? `全屏显示 ${media.title} 的歌词` : `${media.title} 暂无歌词`);
    }

    if (art) {
        if (art.url !== playbackUrl) art.url = playbackUrl;
        else art.currentTime = 0;
        if (previousObjectUrl) URL.revokeObjectURL(previousObjectUrl);
        art.title = middleEllipsis(media.title, 54);
        Promise.resolve(art.play()).then(() => {
            if (sequence !== playerSwitchSequence) return;
            updateMediaSession(media);
            startLyricClock();
        }).catch(error => {
            if (sequence !== playerSwitchSequence || error.name === 'AbortError') return;
            art.notice.show = error.name === 'NotAllowedError'
                ? '浏览器暂停了自动播放，请点击播放继续' : '播放失败，请重试';
        });
        return;
    }

    const PlayerClass = typeof PLAYER_KIND !== 'undefined' && PLAYER_KIND === 'audio'
        ? FrontierAudioPlayer
        : FrontierVideoPlayer;
    art = new PlayerClass({
        container: '#artplayer',
        url: playbackUrl,
        title: middleEllipsis(media.title, 54),
        volume: 0.7,
    });
    bindPlayerBusinessEvents();
    updateMediaSession(media);
    Promise.resolve(art.play()).catch(error => {
        if (sequence !== playerSwitchSequence || error.name === 'AbortError') return;
        art.notice.show = error.name === 'NotAllowedError'
            ? '浏览器暂停了自动播放，请点击播放继续' : '播放失败，请重试';
    });
}

function formatTime(seconds) {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${s < 10 ? '0' : ''}${s}`;
}

function isGestureControl(target) {
    return Boolean(target && target.closest(
        '.sidebar, .art-bottom, .art-controls, .art-progress, .art-control-progress, '
        + '.art-setting, .art-contextmenu, button, a, input, select, textarea, [role="button"]'
    ));
}

function verticalPlayerRatio(event, playerSection) {
    const rect = playerSection.getBoundingClientRect();
    return Math.min(1, Math.max(0, (event.clientY - rect.top) / Math.max(1, rect.height)));
}

function seekToHorizontalPosition(clientX, playerSection) {
    if (!art) return;
    const duration = Number(art.duration || 0);
    if (!duration) return;
    const rect = playerSection.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / Math.max(1, rect.width)));
    art.currentTime = duration * ratio;
}

function showGestureHud(message, timeout = 0) {
    const gestureHud = document.getElementById('gestureHud');
    if (!gestureHud) return;
    gestureHud.innerText = message;
    gestureHud.style.display = 'block';
    if (timeout) {
        window.setTimeout(() => {
            if (gestureHud.innerText === message) gestureHud.style.display = 'none';
        }, timeout);
    }
}

function initAudioGestureControl() {
    const playerSection = document.getElementById('playerSection');
    const gestureHud = document.getElementById('gestureHud');
    let pointerId = null;
    let pointerStartX = 0;
    let pointerStartY = 0;
    let initialTime = 0;
    let targetTime = 0;
    let isDragging = false;
    let suppressClickUntil = 0;

    playerSection.addEventListener('pointerdown', event => {
        if (isGestureControl(event.target) || !art || verticalPlayerRatio(event, playerSection) >= DIRECT_SEEK_ZONE_START) return;
        if (event.pointerType === 'mouse' && event.button !== 0) return;

        pointerId = event.pointerId;
        pointerStartX = event.clientX;
        pointerStartY = event.clientY;
        initialTime = art.currentTime;
        targetTime = initialTime;
        isDragging = false;
    });

    playerSection.addEventListener('pointermove', event => {
        if (event.pointerId !== pointerId || !art) return;

        const deltaX = event.clientX - pointerStartX;
        const deltaY = event.clientY - pointerStartY;

        if (!isDragging && Math.abs(deltaX) > 10 && Math.abs(deltaX) > Math.abs(deltaY)) {
            isDragging = true;
            playerSection.setPointerCapture?.(pointerId);
        }

        if (isDragging) {
            event.preventDefault();

            const duration = art.duration || 1;
            const sensitivity = 0.2;
            const seekOffset = deltaX * sensitivity;

            targetTime = Math.min(Math.max(0, initialTime + seekOffset), duration);

            const sign = seekOffset >= 0 ? '+' : '';
            showGestureHud(`${sign}${Math.round(seekOffset)}s (${formatTime(targetTime)} / ${formatTime(duration)})`);
        }
    });

    const finishPointer = event => {
        if (event.pointerId !== pointerId) return;
        if (isDragging) {
            if (art) art.currentTime = targetTime;
            gestureHud.style.display = 'none';
            isDragging = false;
            suppressClickUntil = Date.now() + 500;
            if (event.cancelable) event.preventDefault();
        }
        pointerId = null;
    };

    playerSection.addEventListener('pointerup', finishPointer);
    playerSection.addEventListener('pointercancel', event => {
        if (event.pointerId !== pointerId) return;
        if (isDragging) {
            gestureHud.style.display = 'none';
            isDragging = false;
        }
        pointerId = null;
    });

    playerSection.addEventListener('dblclick', event => {
        if (isGestureControl(event.target)) return;

        event.stopPropagation();
        event.preventDefault();
    }, true);

    playerSection.addEventListener('click', event => {
        if (Date.now() < suppressClickUntil) return;

        const isDirectSeekZone = verticalPlayerRatio(event, playerSection) >= DIRECT_SEEK_ZONE_START;
        if (!isDirectSeekZone && isGestureControl(event.target)) return;

        event.stopPropagation();
        event.preventDefault();

        if (isDirectSeekZone) {
            if (clickTimer) clearTimeout(clickTimer);
            clickTimer = null;
            seekToHorizontalPosition(event.clientX, playerSection);
            showGestureHud(`跳转到 ${formatTime(art.currentTime)} / ${formatTime(art.duration || 0)}`, 850);
            return;
        }

        if (clickTimer) {
            clearTimeout(clickTimer);
            clickTimer = null;
            playPrev();
        } else {
            clickTimer = setTimeout(() => {
                clickTimer = null;
                playNext();
            }, 250);
        }
    }, true);
}

function initVideoGestureControl() {
    const playerSection = document.getElementById('playerSection');
    let pointerId = null;
    let pointerStartX = 0;
    let pointerStartY = 0;
    let initialTime = 0;
    let targetTime = 0;
    let isDragging = false;
    let suppressClickUntil = 0;

    playerSection.addEventListener('pointerdown', event => {
        if (isGestureControl(event.target) || !art || verticalPlayerRatio(event, playerSection) >= DIRECT_SEEK_ZONE_START) return;
        if (event.pointerType === 'mouse' && event.button !== 0) return;

        pointerId = event.pointerId;
        pointerStartX = event.clientX;
        pointerStartY = event.clientY;
        initialTime = art.currentTime;
        targetTime = initialTime;
        isDragging = false;
    });

    playerSection.addEventListener('pointermove', event => {
        if (event.pointerId !== pointerId || !art) return;

        const deltaX = event.clientX - pointerStartX;
        const deltaY = event.clientY - pointerStartY;
        if (!isDragging && Math.abs(deltaX) > 10 && Math.abs(deltaX) > Math.abs(deltaY)) {
            isDragging = true;
            playerSection.setPointerCapture?.(pointerId);
        }
        if (!isDragging) return;

        event.preventDefault();
        const duration = art.duration || 1;
        const seekOffset = deltaX * 0.2;
        targetTime = Math.min(Math.max(0, initialTime + seekOffset), duration);
    });

    const finishPointer = event => {
        if (event.pointerId !== pointerId) return;
        if (isDragging) {
            if (art) art.currentTime = targetTime;
            isDragging = false;
            suppressClickUntil = Date.now() + 500;
            if (event.cancelable) event.preventDefault();
        }
        pointerId = null;
    };

    playerSection.addEventListener('pointerup', finishPointer);
    playerSection.addEventListener('pointercancel', event => {
        if (event.pointerId !== pointerId) return;
        isDragging = false;
        pointerId = null;
    });

    playerSection.addEventListener('click', event => {
        if (Date.now() < suppressClickUntil) {
            event.stopPropagation();
            event.preventDefault();
            return;
        }

        const isDirectSeekZone = verticalPlayerRatio(event, playerSection) >= DIRECT_SEEK_ZONE_START;
        if (!isDirectSeekZone) return;

        event.stopPropagation();
        event.preventDefault();
        seekToHorizontalPosition(event.clientX, playerSection);
    }, true);
}

function initGestureControl() {
    if (typeof PLAYER_KIND !== 'undefined' && PLAYER_KIND === 'audio') {
        initAudioGestureControl();
    } else {
        initVideoGestureControl();
    }
}

function karaokeUrl(media) {
    const query = new URLSearchParams({media: media.karaoke_id});
    return `/karaoke/?${query}`;
}

function openKaraoke() {
    const media = currentMediaList?.[currentIndex];
    if (!media?.karaoke_id) return;
    accountPlaybackTime();
    void reportValidPlayback();
    window.location.assign(karaokeUrl(media));
}

function initKaraokeGesture() {
    const surface = document.getElementById('playerSection');
    if (!surface) return;
    let timer = null;
    let starts = [];
    let recognized = false;
    const cancel = () => {
        if (timer) clearTimeout(timer);
        timer = null;
        starts = [];
    };
    surface.addEventListener('touchstart', event => {
        if (event.touches.length !== 3) { cancel(); return; }
        starts = Array.from(event.touches, touch => ({id: touch.identifier, x: touch.clientX, y: touch.clientY}));
        recognized = false;
        timer = setTimeout(() => {
            timer = null;
            recognized = true;
            showGestureHud('进入 K歌', 650);
            openKaraoke();
        }, 1500);
    }, {passive: true});
    surface.addEventListener('touchmove', event => {
        if (!timer || event.touches.length !== 3) { cancel(); return; }
        const moved = starts.some(start => {
            const touch = Array.from(event.touches).find(item => item.identifier === start.id);
            return !touch || Math.hypot(touch.clientX - start.x, touch.clientY - start.y) > 18;
        });
        if (moved) cancel();
    }, {passive: true});
    const finish = event => {
        if (!recognized) cancel();
        if (recognized && event.cancelable) event.preventDefault();
        recognized = false;
    };
    surface.addEventListener('touchend', finish, {passive: false});
    surface.addEventListener('touchcancel', finish, {passive: false});
}

function renderPlaylist() {
    const listContainer = document.getElementById('mediaList');
    if (!listContainer) return;
    const matches = currentMediaList
        .map((item, index) => ({item, index}));
    listContainer.innerHTML = matches.map(({item, index}) => `
        <li class="media-item ${index === currentIndex ? 'active' : ''}" data-index="${index}" data-media-id="${escapeHTML(item.media_id)}" onclick="selectMedia(${index})">
            <img src="${escapeHTML(item.cover)}" alt="cover">
            <div class="media-info">
                <div class="media-title" title="${escapeHTML(item.title)}">${escapeHTML(middleEllipsis(item.title, 40))}</div>
                <div class="media-artist">${escapeHTML(item.artist)}</div>
            </div>
        </li>
    `).join('');
}

function mediaIdentity(item) {
    return item?.resource_id || item?.media_id || item?.media_path || '';
}

function applyMediaCatalog(entries) {
    if (!Array.isArray(entries)) return;
    const selectedIdentity = mediaIdentity(currentMediaList?.[currentIndex]);
    currentMediaList = entries;
    if (!entries.length) {
        document.getElementById('mediaList').innerHTML = '<li style="padding:20px;color:#666;text-align:center;">该分类下暂无媒体数据</li>';
        return;
    }
    const matched = selectedIdentity
        ? entries.findIndex(item => mediaIdentity(item) === selectedIdentity)
        : -1;
    currentIndex = matched >= 0 ? matched : 0;
    renderPlaylist();
    if (!art || (selectedIdentity && matched < 0)) {
        initPlayer(currentMediaList[currentIndex], currentIndex);
    }
}

async function loadPlayerCatalog() {
    const config = window.frontierCloudPlayerCatalogConfig;
    if (!config?.url || !window.FrontierCatalogCache) return;
    const cached = window.FrontierCatalogCache.read(config.cacheKey);
    if (cached) applyMediaCatalog(cached.entries);
    try {
        const fresh = await window.FrontierCatalogCache.fetch(config.url, config.cacheKey);
        applyMediaCatalog(fresh.entries);
    } catch (error) {
        if (!currentMediaList.length) {
            document.getElementById('mediaList').innerHTML = `<li style="padding:20px;color:#ff9f9f;text-align:center;">${escapeHTML(error.message)}</li>`;
        }
    }
}

window.addEventListener('DOMContentLoaded', () => {
    const listContainer = document.getElementById('mediaList');
    listContainer.innerHTML = '<li style="padding:20px;color:#888;text-align:center;">正在加载媒体目录…</li>';
    document.getElementById('lyricsLink')?.addEventListener('click', openFullscreenLyrics);
    document.getElementById('karaokeLink')?.addEventListener('click', openKaraoke);
    document.getElementById('fullscreenLyrics')?.addEventListener('click', closeFullscreenLyrics);
    initGestureControl();
    initKaraokeGesture();
    playbackReporter = setInterval(reportValidPlayback, 1000);
    if (typeof currentMediaList !== 'undefined' && currentMediaList.length) {
        applyMediaCatalog(currentMediaList);
    }
    void loadPlayerCatalog();
});

window.addEventListener('pagehide', event => {
    if (playbackReporter) clearInterval(playbackReporter);
    stopLyricClock();
    accountPlaybackTime();
    discardNextPreload();
    if (!event.persisted && activeObjectUrl) {
        URL.revokeObjectURL(activeObjectUrl);
        activeObjectUrl = null;
    }
});

window.addEventListener('pageshow', event => {
    if (event.persisted && art) {
        playbackReporter = setInterval(reportValidPlayback, 1000);
        if (art.playing) startLyricClock();
    }
});

window.addEventListener('resize', () => {
    sizeSynchronizedLyrics();
    sizeFullscreenLyrics();
});

function selectMedia(index) {
    const targetElement = document.querySelector(`.media-item[data-index="${index}"]`);
    const items = document.querySelectorAll('.media-item');
    items.forEach(item => item.classList.remove('active'));
    if (targetElement) {
        targetElement.classList.add('active');
        targetElement.scrollIntoView({ block: 'nearest', behavior: 'auto' });
    }

    initPlayer(currentMediaList[index], index);
}

window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !document.getElementById('fullscreenLyrics')?.classList.contains('hidden')) {
        e.preventDefault();
        closeFullscreenLyrics();
    } else if (e.key === 'ArrowRight' || e.key === 'MediaTrackNext' || e.code === 'MediaTrackNext') {
        e.preventDefault();
        playNext();
    } else if (e.key === 'ArrowLeft' || e.key === 'MediaTrackPrevious' || e.code === 'MediaTrackPrevious') {
        e.preventDefault();
        playPrev();
    }
});
