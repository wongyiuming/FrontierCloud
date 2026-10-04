import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../static/js/audio-continuous-stream.js', import.meta.url), 'utf8');
new vm.Script(source);
assert.doesNotMatch(source, /HANDOFF_SECONDS|pre-end-200ms|standby\.play|bridge_active/);
assert.doesNotMatch(source, /endOfStream\s*\(/);
assert.doesNotMatch(source, /Math\.max\(this\.activeSegment\.start, this\.bufferedEnd\(\)\)/);
assert.doesNotMatch(source, /await this\.appendBytes\(value\)/);
assert.doesNotMatch(source, /cache:\s*['"]no-store['"]/);
assert.match(source, /const MIME = 'audio\/mpeg'/);
assert.match(source, /sourceBuffer\.mode = 'sequence'/);
assert.match(source, /const LOOKAHEAD_TRACKS = 2/);
assert.match(source, /const FIRST_APPEND_BYTES = 64 \* 1024/);
assert.match(source, /const APPEND_BATCH_BYTES = 512 \* 1024/);
assert.match(source, /const MAX_BUFFER_AHEAD_SECONDS = 30/);
assert.match(source, /const SEEK_RANGE_ALIGNMENT_BYTES = 64 \* 1024/);
assert.match(source, /Range: `bytes=\$\{seek\.rangeStart\}-`/);
assert.match(source, /void startContinuous\(this\.activeSegment\.index, restart\)/);
assert.match(source, /pendingSeekGlobal/);
assert.match(source, /error\?\.name !== 'QuotaExceededError'/);
assert.match(source, /await this\.waitForAppendCapacity\(true\)/);
assert.match(source, /await this\.appendBufferOnce\(chunk\)/);
assert.match(source, /presentationDuration:\s*0/);
assert.match(source, /latchPresentationDuration\(segment, estimate\)/);
assert.match(source, /latchPresentationDuration\(segment, segment\.duration\)/);
assert.match(source, /FrontierAudioPlayer\.prototype\._syncTime = function continuousSyncTime/);
assert.match(source, /session\.syncTimeUi\(this\)/);
assert.match(source, /if \(!hasWarning && !existing\) return/);

class BasePlayer {
    get currentTime() { return 0; }
    set currentTime(_value) {}
    get duration() { return 0; }
    _syncTime() {}
    _syncBuffered() {}
}
class AudioPlayer extends BasePlayer {}

let legacyApplyCalls = 0;
let selectedIndex = null;
let querySelectorCalls = 0;
const windowObject = {
    setTimeout() { return 1; },
    clearTimeout() {},
};
const context = {
    PLAYER_KIND: 'audio',
    initPlayer() {},
    playNext() {},
    playPrev() {},
    checkAndPreloadNext() {},
    applyMediaCatalog() { legacyApplyCalls += 1; },
    renderPlaylist() {},
    selectMedia(index) { selectedIndex = index; },
    FrontierMediaPlayer: BasePlayer,
    FrontierAudioPlayer: AudioPlayer,
    MediaSource: class {
        static isTypeSupported(value) { return value === 'audio/mpeg'; }
    },
    URL: {
        createObjectURL() { return 'blob:fixture'; },
        revokeObjectURL() {},
    },
    window: windowObject,
    document: {
        querySelectorAll() { return []; },
        querySelector() { querySelectorCalls += 1; return null; },
        createElement() { return {className: '', textContent: ''}; },
        getElementById() { return null; },
    },
    console,
    setTimeout() { return 1; },
    clearTimeout() {},
    Uint8Array,
    AbortController,
};
vm.createContext(context);
const instrumented = source.replace(
    'window.frontierCloudContinuousAudio = {',
    'window.__estimateMp3Duration = estimateMp3Duration;\n'
        + '    window.__presentationDuration = presentationDuration;\n'
        + '    window.__latchPresentationDuration = latchPresentationDuration;\n'
        + '    window.__seekRangePlan = seekRangePlan;\n'
        + '    window.__ContinuousAudioSession = ContinuousAudioSession;\n'
        + '    window.__setContinuousSessionForTest = value => { session = value; sessionGeneration = value.generation; };\n'
        + '    window.frontierCloudContinuousAudio = {',
);
vm.runInContext(instrumented, context);

const api = windowObject.frontierCloudContinuousAudio;
assert.equal(api.installed, true);
assert.equal(api.mime, 'audio/mpeg');
assert.equal(api.lookahead_tracks, 2);
assert.equal(api.append_batch_bytes, 512 * 1024);
assert.equal(api.append_max_wait_ms, 1000);
assert.equal(api.first_append_bytes, 64 * 1024);
assert.equal(api.max_buffer_ahead_seconds, 30);
assert.equal(api.supported(), true);
assert.equal(api.compatible({type: 'audio', media_path: 'music/a/track.mp3'}), true);
assert.equal(api.compatible({type: 'audio', media_path: 'music/a/track.flac'}), false);
assert.equal(api.compatible({type: 'video', media_path: 'vido/a/track.mp3'}), false);

const estimated = vm.runInContext(`(() => {
    const cbrHeader = new Uint8Array(64 * 1024);
    cbrHeader.set([0xff, 0xfb, 0x90, 0x00], 0);
    return window.__estimateMp3Duration(cbrHeader, 1_600_000);
})()`, context);
assert.ok(Math.abs(estimated - 100) < 0.01, `expected stable 100 second estimate, got ${estimated}`);

const latchResult = vm.runInContext(`(() => {
    const segment = {presentationDuration: 0};
    const first = window.__latchPresentationDuration(segment, 100);
    const second = window.__latchPresentationDuration(segment, 120);
    return JSON.stringify([first, second, segment.presentationDuration, window.__presentationDuration(segment)]);
})()`, context);
assert.equal(latchResult, '[100,100,100,100]', 'presentation duration must be write-once even if later MSE duration grows');

const seekPlan = JSON.parse(vm.runInContext(
    'JSON.stringify(window.__seekRangePlan(75, 10 * 1024 * 1024, 100))',
    context,
));
assert.equal(seekPlan.seekSeconds, 75);
assert.equal(seekPlan.rangeStart, 7_798_784);
assert.ok(seekPlan.localOffset < 75, 'Range must begin before the requested time so the decoder can resynchronize');
assert.ok(75 - seekPlan.localOffset < 2, 'the prefetched decode lead should remain tightly bounded');

const quotaRetryResult = await vm.runInContext(`(async () => {
    const listeners = new Map();
    let appendCalls = 0;
    const sourceBuffer = {
        updating: false,
        addEventListener(name, callback) { listeners.set(name, callback); },
        removeEventListener(name, callback) {
            if (listeners.get(name) === callback) listeners.delete(name);
        },
        appendBuffer() {
            appendCalls += 1;
            if (appendCalls === 1) {
                const error = new Error('full');
                error.name = 'QuotaExceededError';
                throw error;
            }
            Promise.resolve().then(() => listeners.get('updateend')?.());
        },
    };
    const candidate = Object.create(window.__ContinuousAudioSession.prototype);
    Object.assign(candidate, {closed: false, generation: 7, sourceBuffer, quotaWaitCount: 0});
    const capacityCalls = [];
    candidate.waitUpdateEnd = async () => {};
    candidate.waitForAppendCapacity = async forced => { capacityCalls.push(Boolean(forced)); };
    window.__setContinuousSessionForTest(candidate);
    await candidate.appendBytes(new Uint8Array([1, 2, 3]));
    return JSON.stringify({appendCalls, capacityCalls, quotaWaitCount: candidate.quotaWaitCount});
})()`, context);
assert.equal(
    quotaRetryResult,
    '{"appendCalls":2,"capacityCalls":[false,true,false],"quotaWaitCount":1}',
    'quota pressure must retry the same append after silent capacity backpressure',
);

// A slow Direct transfer must deliver partial batches while its next read is
// pending, without consuming that read twice or changing byte order.
context.currentMediaList = [{type: 'audio', media_path: 'music/direct/song.mp3', url: '/fixture'}];
context.art = {_syncTime() {}, _syncBuffered() {}, video: {buffered: {length: 0}}};
function transferCandidate(reader, total) {
    const candidate = Object.create(windowObject.__ContinuousAudioSession.prototype);
    let appended = 0;
    const batches = [];
    Object.assign(candidate, {
        closed: false, generation: 9, startIndex: 0, segments: [], activeSegment: null,
        fetchController: new AbortController(), initialSeek: null,
        bufferedEnd: () => appended / 32768,
        appendBytes: async bytes => { batches.push(bytes); appended += bytes.byteLength; },
    });
    context.fetch = async () => ({
        ok: true,
        headers: {get(name) { return name === 'Content-Length' ? String(total) : 'audio/mpeg'; }},
        body: {getReader() { return reader; }},
    });
    windowObject.__setContinuousSessionForTest(candidate);
    return {candidate, batches};
}
const slowChunks = [new Uint8Array(65536).fill(1), new Uint8Array(32768).fill(2), new Uint8Array(65536).fill(3)];
let slowReads = 0;
let finishRead;
let readerCancelled = false;
const slowReader = {
    read() {
        slowReads += 1;
        if (slowReads < 3) return Promise.resolve({done: false, value: slowChunks[slowReads - 1]});
        if (slowReads === 3) return new Promise(resolve => { finishRead = resolve; });
        return Promise.resolve({done: true});
    },
    cancel() { readerCancelled = true; return Promise.resolve(); },
};
const timers = new Map();
let timerSequence = 0;
windowObject.setTimeout = (callback, delay) => { timers.set(++timerSequence, {callback, delay}); return timerSequence; };
windowObject.clearTimeout = id => timers.delete(id);
const slow = transferCandidate(slowReader, slowChunks.reduce((sum, chunk) => sum + chunk.length, 0));
const transferring = slow.candidate.appendTrack(0);
await new Promise(setImmediate);
assert.deepEqual(slow.batches.map(bytes => bytes.length), [65536]);
assert.equal(slowReads, 3);
assert.equal(timers.size, 1, 'one deadline must guard the retained network read');
const deadline = [...timers.values()][0];
assert.ok(deadline.delay <= 1000);
deadline.callback();
await new Promise(setImmediate);
assert.deepEqual(slow.batches.map(bytes => bytes.length), [65536, 32768], 'received audio must flush before another network chunk arrives');
assert.equal(slowReads, 3, 'a deadline must not issue an additional concurrent reader.read');
finishRead({done: false, value: slowChunks[2]});
await transferring;
assert.deepEqual(slow.batches.flatMap(bytes => Array.from(bytes)), slowChunks.flatMap(bytes => Array.from(bytes)));
assert.equal(readerCancelled, true);
assert.equal(timers.size, 0, 'all flush timers must be released');

// Chromium may hand over a multi-MiB chunk after consumer backpressure. Every
// append must still honor the startup/regular caps and preserve exact bytes.
const largeChunk = Uint8Array.from({length: 2 * 1024 * 1024 + 37}, (_, index) => index % 251);
let largeRead = 0;
const large = transferCandidate({
    read: async () => largeRead++ === 0 ? {done: false, value: largeChunk} : {done: true},
    cancel: async () => {},
}, largeChunk.length);
await large.candidate.appendTrack(0);
assert.equal(large.batches[0].length, 65536);
assert.ok(large.batches.every(bytes => bytes.length <= 512 * 1024), 'MSE append size must not depend on native fetch chunk size');
assert.deepEqual(Buffer.concat(large.batches), Buffer.from(largeChunk));

const stopController = new AbortController();
const stopping = Object.create(windowObject.__ContinuousAudioSession.prototype);
Object.assign(stopping, {closed: false, fetchController: stopController, objectUrl: 'blob:old'});
context.activeObjectUrl = 'blob:old';
windowObject.__setContinuousSessionForTest(stopping);
stopping.stop();
assert.equal(stopController.signal.aborted, true, 'switching tracks must abort the old Direct download');
assert.equal(context.activeObjectUrl, null);

const mp3Only = [
    {type: 'audio', media_path: 'music/a/one.mp3'},
    {type: 'audio', media_path: 'music/a/two.mp3'},
];
context.currentMediaList = mp3Only;
context.currentIndex = 0;
context.applyMediaCatalog(mp3Only);
assert.equal(legacyApplyCalls, 1);
assert.equal(querySelectorCalls, 1, 'all-MP3 catalog should only perform the single warning-existence lookup');

const entries = [
    {type: 'audio', media_path: 'music/a/legacy.flac'},
    {type: 'audio', media_path: 'music/a/stream.mp3'},
    {type: 'audio', media_path: 'music/a/legacy.m4a'},
];
context.currentMediaList = entries;
context.currentIndex = 0;
context.applyMediaCatalog(entries);
assert.equal(legacyApplyCalls, 2);
assert.match(entries[0].continuous_stream_skip_reason, /自动续播跳过/);
assert.equal(entries[1].continuous_stream_compatible, true);
assert.match(entries[2].continuous_stream_skip_reason, /自动续播跳过/);

context.playNext();
assert.equal(selectedIndex, 1, 'auto-next must skip incompatible audio and select the next MP3');

const videoWindow = {};
vm.runInNewContext(source, {PLAYER_KIND: 'video', window: videoWindow});
assert.equal(videoWindow.frontierCloudContinuousAudio, undefined, 'video pages must not install audio continuity');

console.log('audio-continuous-stream-smoke-ok');
