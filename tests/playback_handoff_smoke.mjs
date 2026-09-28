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

function scenario({rejectStandby = false, currentTime = 9.81, playbackRate = 1} = {}) {
    const timers = [];
    const intervals = [];
    const createdMedia = [];
    const oldVideo = new FakeMedia();
    oldVideo.currentTime = currentTime;
    oldVideo.duration = 10;
    oldVideo.paused = false;
    oldVideo.playbackRate = playbackRate;

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
            play() { return this.video.play(); },
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
        runTimersAt(ms) {
            for (const entry of [...timers]) {
                if (!entry.cancelled && Math.abs(entry.ms - ms) < 1) {
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
    s.oldVideo.currentTime = 0.2;
    s.runTimersAt(500);
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

{
    const s = scenario({currentTime: 9.5, playbackRate: 2});
    s.runPoll();
    const transition = s.timers.find(entry => !entry.cancelled && Math.abs(entry.ms - 50) < 1);
    assert.ok(transition, 'T-200ms must use wall time when playback speed is not 1x');
}

{
    const s = scenario();
    s.runPoll();
    s.runImmediateTimers();
    await flush();

    const standby = s.createdMedia[0];
    assert.equal(s.run('window.frontierCloudPlaybackContinuityHandoff.status().bridge_active'), true);
    s.runTimersAt(8000);
    await flush();
    assert.equal(s.oldVideo.playCalls, 1, 'a missed main takeover must retry main playback');
    assert.equal(standby.paused, false, 'main recovery must keep the audible bridge alive');
    assert.equal(s.run('window.frontierCloudPlaybackContinuityHandoff.status().bridge_active'), true);

    s.run("art.emit('video:playing')");
    s.oldVideo.currentTime = 0.2;
    s.runTimersAt(500);
    assert.equal(standby.paused, true, 'the bridge stops only after main playback is confirmed');
    assert.equal(s.run('window.frontierCloudPlaybackContinuityHandoff.status().bridge_active'), false);
}

{
    const s = scenario();
    s.runPoll();
    s.runImmediateTimers();
    await flush();

    const standby = s.createdMedia[0];
    s.run("art.emit('video:playing')");
    s.oldVideo.currentTime = 0.015;
    s.runTimersAt(500);
    assert.equal(standby.paused, false, 'a playing event without clock progress must keep the bridge audible');
    assert.equal(s.oldVideo.muted, true, 'a stalled main deck must stay muted');
    assert.equal(s.run('window.frontierCloudPlaybackContinuityHandoff.status().bridge_active'), true);

    s.run("art.emit('video:playing')");
    s.oldVideo.currentTime = 0.215;
    s.runTimersAt(500);
    assert.equal(standby.paused, true, 'the bridge retires after measured main clock progress');
    assert.equal(s.oldVideo.muted, false);
    assert.equal(s.run('window.frontierCloudPlaybackContinuityHandoff.status().bridge_active'), false);
}

{
    const s = scenario();
    s.runPoll();
    s.runImmediateTimers();
    await flush();

    const standby = s.createdMedia[0];
    s.run('selectMedia(0)');
    s.run("art.emit('video:playing')");
    assert.equal(standby.paused, true, 'a newer selection must retire the old bridge');
    assert.equal(s.oldVideo.muted, false, 'a stale handoff must not leave the newer selection muted');
    assert.equal(s.run('window.frontierCloudPlaybackContinuityHandoff.status().bridge_active'), false);
}

console.log('playback-handoff-smoke-ok');
