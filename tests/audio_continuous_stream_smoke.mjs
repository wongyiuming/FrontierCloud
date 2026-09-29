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
assert.match(source, /duration:\s*null, durationHint:\s*0/);
assert.match(source, /estimateMp3Duration\(merged, declared\)/);
assert.match(source, /if \(!hasWarning && !existing\) return/);

class BasePlayer {
    get currentTime() { return 0; }
    set currentTime(_value) {}
    get duration() { return 0; }
    _syncBuffered() {}
}
class AudioPlayer extends BasePlayer {}

let legacyApplyCalls = 0;
let selectedIndex = null;
let querySelectorCalls = 0;
const windowObject = {
    setTimeout() { return 1; },
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
};
vm.createContext(context);
vm.runInContext(source, context);

const api = windowObject.frontierCloudContinuousAudio;
assert.equal(api.installed, true);
assert.equal(api.mime, 'audio/mpeg');
assert.equal(api.lookahead_tracks, 2);
assert.equal(api.append_batch_bytes, 512 * 1024);
assert.equal(api.first_append_bytes, 64 * 1024);
assert.equal(api.supported(), true);
assert.equal(api.compatible({type: 'audio', media_path: 'music/a/track.mp3'}), true);
assert.equal(api.compatible({type: 'audio', media_path: 'music/a/track.flac'}), false);
assert.equal(api.compatible({type: 'video', media_path: 'vido/a/track.mp3'}), false);

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
