import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../static/js/playback-continuity-handoff.js', import.meta.url), 'utf8');
new vm.Script(source);

class FakeMedia {
    constructor({rejectPlay = false} = {}) {
        this.rejectPlay = rejectPlay;
        this.listeners = new Map();
        this.readyState = 4;
        this.networkState = 1;
        this.currentTime = 0;
        this.duration = 120;
        this.paused = true;
        this.ended = false;
        this.volume = 0.7;
        this.muted = false;
        this.playbackRate = 1;
        this.playCalls = 0;
        this.pauseCalls = 0;
        this.style = {};
        this.src = '';
    }

    addEventListener(name, listener) {
        if (!this.listeners.has(name)) this.listeners.set(name, []);
        this.listeners.get(name).push(listener);
    }

    emit(name) {
        for (const listener of this.listeners.get(name) || []) listener({type: name});
    }

    async play() {
        this.playCalls += 1;
        if (this.rejectPlay) {
            const error = new Error('blocked');
            error.name = 'NotAllowedError';
            throw error;
        }
        this.paused = false;
    }

    pause() {
        this.pauseCalls += 1;
        this.paused = true;
    }

    load() {}
    setAttribute() {}
    remove() { this.removed = true; }
}

async function flush() {
    await Promise.resolve();
    await new Promise(resolve => setImmediate(resolve));
}

function scenario({rejectStandby = false} = {}) {
    const timers = [];
    const intervals = [];
    const createdMedia = [];
    const oldVideo = new FakeMedia();
    oldVideo.currentTime = 9.81;
    oldVideo.duration = 10;
    oldVideo.paused = false;

    const windowListeners = new Map();
    const windowObject = {
        setTimeout(fn, ms) { const entry = {fn, ms, cancelled: false}; timers.push(entry); return entry; },
        clearTimeout(entry) { if (entry) entry.cancelled = true; },
        setInterval(fn, ms) { const entry = {fn, ms, cancelled: false}; intervals.push(entry); return entry; },
        clearInterval(entry) { if (entry) entry.cancelled = true; },
        addEventListener(name, listener) {
            if (!windowListeners.has(name)) windowListeners.set(name, []);
            windowListeners.get(name).push(listener);
        },
        dispatchEvent() {},
    };

    const context = vm.createContext({
        window: windowObject,
        document: {
            visibilityState: 'hidden',
            hidden: true,
            createElement(name) {
                assert.equal(name, 'audio');
                const media = new FakeMedia({rejectPlay: rejectStandby});
                createdMedia.push(media);
                return media;
            },
            body: {appendChild() {}},
            documentElement: {appendChild() {}},
        },
        navigator: {
            userAgent: 'Tesla Chromium regression fixture',
            platform: 'Linux x86_64',
            language: 'zh-CN',
            sendBeacon() { return true; },
        },
        performance: {now: () => 10000},
        Blob,
        URL: {revokeObjectURL() {}},
        CustomEvent: class { constructor(name, init) { this.type = name; this.detail = init?.detail; } },
        fetch: async () => ({ok: true}),
        oldVideo,
    });
    windowObject.window = windowObject;

    vm.runInContext(`
        const PLAYER_KIND = 'audio';
        let currentIndex = 0;
        let playerSwitchSequence = 1;
        let activeObjectUrl = null;
        let nextPreload = {url: '/b', status: 'ready', objectUrl: 'blob:b'};
        let currentMediaList = [
            {media_id: 'a', type: 'audio', url: '/a'},
            {media_id: 'b', type: 'audio', url: '/b'},
        ];
        const artEvents = new Map();
        let art = {
            video: oldVideo,
            root: {appendChild() {}},
            get currentTime() { return this.video.currentTime; },
            get duration() { return this.video.duration; },
            on(name, listener) {
                if (!artEvents.has(name)) artEvents.set(name, new Set());
                artEvents.get(name).add(listener);
                return this;
            },
            off(name, listener) { artEvents.get(name)?.delete(listener); return this; },
            emit(name) { for (const listener of [...(artEvents.get(name) || [])]) listener(); },
        };
        function nextMediaIndex() { return (currentIndex + 1) % currentMediaList.length; }
        function discardNextPreload() { nextPreload = null; }
        function checkAndPreloadNext() {}
        function updateMediaSession() {}
        function selectMedia(index) {
            currentIndex = index;
            playerSwitchSequence += 1;
            if (nextPreload?.objectUrl) {
                activeObjectUrl = nextPreload.objectUrl;
                nextPreload.objectUrl = null;
            }
            discardNextPreload();
            art.video.src = activeObjectUrl || currentMediaList[index].url;
            art.video.currentTime = 0;
            art.video.duration = 120;
            art.video.paused = false;
        }
        function playNext() { selectMedia(nextMediaIndex()); }
        function playPrev() { selectMedia((currentIndex - 1 + currentMediaList.length) % currentMediaList.length); }
    `, context);

    vm.runInContext(source, context);
    return {
        context,
        oldVideo,
        createdMedia,
        timers,
        intervals,
        run: code => vm.runInContext(code, context),
        runPoll() { for (const entry of intervals) if (!entry.cancelled) entry.fn(); },
        runImmediateTimers() {
            for (const entry of timers) {
                if (!entry.cancelled && entry.ms <= 1) {
                    entry.cancelled = true;
                    entry.fn();
                }
            }
        },
    };
}

{
    const s = scenario();
    assert.equal(s.run('window.frontierCloudPlaybackContinuityHandoff.handoffSeconds'), 0.2);
    s.runPoll();
    s.runImmediateTimers();
    await flush();

    assert.equal(s.createdMedia.length, 1, 'one standby deck must be warmed');
    assert.equal(s.createdMedia[0].playCalls, 1, 'standby must start before the old deck is released');
    assert.equal(s.oldVideo.pauseCalls, 1, 'old deck must pause only after standby play succeeds');
    assert.equal(s.run('currentIndex'), 1, 'business state must advance to the warmed next track');
    assert.equal(s.oldVideo.muted, true, 'main deck stays muted while it catches the bridge');
    assert.equal(s.run('window.frontierCloudPlaybackContinuityHandoff.status().bridge_active'), true);

    s.run("art.emit('video:playing')");
    assert.equal(s.oldVideo.muted, false, 'main deck restores the user mute state after takeover');
    assert.equal(s.createdMedia[0].pauseCalls >= 1, true, 'standby stops after main-deck takeover');
    assert.equal(s.run('window.frontierCloudPlaybackContinuityHandoff.status().bridge_active'), false);

    const before = s.run('currentIndex');
    assert.equal(s.run("playNext('media-session-next')"), false, 'duplicate system media control must be suppressed after handoff');
    assert.equal(s.run('currentIndex'), before);
}

{
    const s = scenario({rejectStandby: true});
    s.runPoll();
    s.runImmediateTimers();
    await flush();

    assert.equal(s.createdMedia[0].playCalls, 1);
    assert.equal(s.oldVideo.pauseCalls, 0, 'failed standby play must never cut the old track early');
    assert.equal(s.run('currentIndex'), 0, 'failed early handoff must leave business state unchanged');
    s.run("playNext('legacy-next')");
    assert.equal(s.run('currentIndex'), 1, 'normal ended-style fallback remains available');
}

console.log('playback-handoff-smoke-ok');
