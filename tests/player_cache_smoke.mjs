import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync('static/js/player.js', 'utf8');
const flush = () => new Promise(setImmediate);
function playerContext(fetchImpl) {
    let clock = 10000;
    const requests = [], revoked = [], objects = [];
    const elements = new Map();
    const elementFor = id => {
        if (!elements.has(id)) elements.set(id, {
            id,
            textContent: '',
            style: {setProperty(name, value) { this[name] = value; }},
            classList: {add() {}, remove() {}, toggle() {}, contains(value) { return value === 'hidden'; }},
            clientHeight: 400,
            clientWidth: 500,
            children: [],
            replaceChildren(...children) { this.children = children; },
            appendChild(child) { this.children.push(child); },
            setAttribute() {},
            removeAttribute() {},
        });
        return elements.get(id);
    };
    const context = vm.createContext({
        window: {addEventListener() {}}, navigator: {},
        document: {
            getElementById: id => elementFor(id),
            createElement: () => ({className: '', textContent: '', style: {}, offsetHeight: 100, children: [], appendChild(child) { this.children.push(child); }}),
            querySelector: () => null,
            querySelectorAll: () => [],
        },
        performance: {now: () => clock}, AbortController, Blob, setTimeout, clearTimeout,
        URL: {
            createObjectURL(blob) { objects.push(blob); return `blob:track-${objects.length}`; },
            revokeObjectURL(url) { revoked.push(url); },
        },
        currentMediaList: ['a', 'b', 'c'].map(id => ({media_id: id, type: 'audio', url: `/${id}`, cover: '', play_score: 0})),
        fetch: async (url, options) => { requests.push({url, options}); return fetchImpl(url, options); },
    });
    vm.runInContext(source, context);
    vm.runInContext("art = {duration: 100, currentTime: 5, playing: false, url: '/a', play: async () => {}, notice: {}}", context);
    return {context, elements, requests, revoked, objects, advance: n => {clock += n;}, run: code => vm.runInContext(code, context)};
}

{
    const p = playerContext(async () => new Response(new Uint8Array([1, 2, 3]), {headers: {'Content-Type': 'audio/wav'}}));
    p.run('checkAndPreloadNext(5)');
    await flush();
    assert.equal(p.run('nextPreload.status'), 'ready');
    assert.equal(p.objects[0].size, 3);
    assert.equal(p.requests[0].options.headers, undefined);
    p.run('currentMediaList[2].preference = 7; playNext()');
    await flush();
    assert.equal(p.run('art.url'), 'blob:track-1');
    assert.equal(p.run('currentIndex'), 1);
    assert.equal(p.requests.length, 1);
    p.run('playNext()');
    assert.equal(p.run('art.url'), '/c');
    assert.deepEqual(p.revoked, ['blob:track-1']);
}

for (const failure of ['network', 'status', 'truncated']) {
    let attempts = 0;
    const p = playerContext(async () => {
        if (attempts++ === 0) {
            if (failure === 'network') throw new Error('offline');
            if (failure === 'status') return new Response('failure', {status: 500});
            return new Response(new ReadableStream({start(controller) {controller.error(new Error('body failed'));}}));
        }
        return new Response('complete');
    });
    p.run('checkAndPreloadNext(5)');
    await flush();
    assert.equal(p.run('nextPreload.status'), 'failed');
    p.run('checkAndPreloadNext(6)');
    assert.equal(p.requests.length, 1);
    p.advance(4000);
    p.run('checkAndPreloadNext(10)');
    await flush();
    assert.equal(p.run('nextPreload.status'), 'ready');
    assert.equal(p.requests.length, 2);
}

{
    let release;
    const pending = new Promise(resolve => {release = resolve;});
    const p = playerContext(async () => pending);
    p.run('checkAndPreloadNext(5); playNext()');
    assert.equal(p.run('art.url'), '/b');
    assert.equal(p.requests[0].options.signal.aborted, true);
    release(new Response('late data'));
    await flush();
    assert.equal(p.objects.length, 0);
}

{
    const p = playerContext(async () => new Response('large', {headers: {'Content-Length': String(129 * 1024 * 1024)}}));
    p.run('checkAndPreloadNext(5)');
    await flush();
    assert.equal(p.run('nextPreload.status'), 'skipped');
    assert.equal(p.requests[0].options.signal.aborted, true);
    p.advance(60000);
    p.run('checkAndPreloadNext(65)');
    assert.equal(p.requests.length, 1);
}

{
    let respond;
    const p = playerContext(() => new Promise(resolve => {respond = resolve;}));
    p.context.playbackSessionId = 'session';
    p.run("resetPlaybackAccounting(currentMediaList[0]); playbackState.accumulated=30; reportValidPlayback()");
    p.run('resetPlaybackAccounting(currentMediaList[1])');
    respond(new Response(JSON.stringify({play_score: 1})));
    await flush();
    assert.equal(p.run('playbackState.reported'), false);
    assert.equal(p.run('currentMediaList[0].play_score'), 1);
}

{
    const p = playerContext(async () => new Response('unused'));
    p.run("showSynchronizedLyrics([{time:1,text:'one'},{time:2,text:'two'},{time:3,text:'three'},{time:4,text:'four'}], 2.5)");
    const track = p.elements.get('inlineLyricsTrack');
    assert.deepEqual(track.children.slice(0, 4).map(row => row.textContent), ['one', 'two', 'three', 'four']);
    assert.equal(track.children[1].className, 'sync-lyric current');
    p.run('updateSynchronizedLyrics(3.1)');
    assert.equal(track.style.transform, 'translateY(-100px)', 'slide uses the actual departing row height so wrapped lines are not cut');
    assert.equal(track.children[2].className, 'sync-lyric current');
}

{
    const p = playerContext(async () => new Response('unused'));
    let reloads = 0, resumes = 0, restore;
    p.context.reloadsRef = () => { reloads += 1; };
    p.context.resumesRef = () => { resumes += 1; };
    p.context.restoreRef = listener => { restore = listener; };
    p.run(`
        art = {
            e: {}, notice: {}, loading: {}, mask: {}, controls: {}, url: '/b', playRequested: true,
            video: {
                currentTime: 1800, duration: 2400, paused: false,
                load() { reloadsRef(); },
                async play() { resumesRef(); },
                addEventListener(event, listener) { if (event === 'loadedmetadata') restoreRef(listener); },
            },
            on(name, fn) { (this.e[name] ||= []).push(fn); return this; },
            emit(name) { for (const listener of [...(this.e[name] || [])]) listener(); },
            async play() {},
        };
        currentIndex = 1;
        playerSwitchSequence = 7;
        currentMediaList[1].resource_id = 'owner-b-object';
        resetPlaybackAccounting(currentMediaList[1]);
        playbackState.lastTick = performance.now();
        bindPlayerBusinessEvents();
        art.emit('video:error');
    `);
    assert.equal(reloads, 1);
    assert.equal(p.run('art.video.src'), '/b');
    restore();
    assert.equal(p.run('art.video.currentTime'), 1800);
    assert.equal(resumes, 1);
    p.run('art.emit("video:error")');
    assert.equal(reloads, 1);
    assert.equal(p.run('art.loading.show'), false);
    assert.equal(p.run('art.notice.show'), '媒体暂不可用，请稍后重试');
}

{
    const p = playerContext(async () => new Response('unused'));
    const scheduled = [];
    p.context.window.FrontierMusicMetadata = {metadata(entry) { scheduled.push(entry); return Promise.resolve({}); }};
    p.context.window.FrontierMediaCrypto = {prepareCatalog: async () => {}, resetFault() {}};
    p.run(`currentMediaList = Array.from({length: 500}, (_, i) => ({media_id: String(i), type: 'audio', url: '/track-' + i, cover: '', title: String(i), encryption: {version: 1}}));
        cryptoPreparedCatalogs.add(currentMediaList); applyMediaCatalog(currentMediaList);`);
    assert.equal(scheduled.length, 1, 'opening a large encrypted album reads only the selected track');
    assert.equal(scheduled[0].media_id, '0');
    p.run('selectMedia(400)');
    assert.equal(scheduled.length, 2, 'selecting a distant track must not queue intervening tracks');
    assert.equal(scheduled[1].media_id, '400');
    p.run('selectMedia(0)');
    assert.equal(scheduled.length, 2, 'already requested metadata is reused');
}
{
    const p = playerContext(async () => new Response('unused'));
    let reads = 0;
    p.context.window.FrontierMusicMetadata = {metadata() {
        reads += 1; return Promise.resolve({title: 'S3 original title'});
    }};
    p.run('currentMediaList[0].browser_metadata=true; loadSelectedMetadata(currentMediaList[0]);');
    await flush();
    assert.equal(reads, 1, 'plain S3 tracks require browser metadata without a crypto descriptor');
    assert.equal(p.run('currentMediaList[0].title'), 'S3 original title');
}
{
    const p = playerContext(async () => new Response('unused'));
    let attempts = 0;
    p.context.window.FrontierMusicMetadata = {metadata() {
        return ++attempts === 1 ? Promise.reject(new Error('transient Range failure')) : Promise.resolve({title: 'Recovered'});
    }};
    p.run('currentMediaList[0].encryption = {version: 1}; loadSelectedMetadata(currentMediaList[0]); loadSelectedMetadata(currentMediaList[0]);');
    assert.equal(attempts, 1, 'an in-flight selected-track read remains deduplicated');
    await flush();
    p.run('loadSelectedMetadata(currentMediaList[0])');
    await flush();
    assert.equal(attempts, 2, 'a failed selected-track read must be retryable');
    assert.equal(p.run('currentMediaList[0].title'), 'Recovered');
}
{
    const p = playerContext(async () => new Response('unused'));
    let observer;
    const scheduled = [], releases = [];
    const rows = Array.from({length: 500}, (_, index) => ({dataset: {index: String(index)}}));
    p.context.window.IntersectionObserver = class {
        constructor(callback) { observer = callback; }
        observe() {}
        disconnect() {}
    };
    p.context.window.FrontierMusicMetadata = {metadata(entry, options) {
        scheduled.push({id: entry.media_id, priority: options.priority});
        return new Promise(resolve => releases.push(() => resolve({cover: 'blob:own-' + entry.media_id})));
    }};
    p.elements.set('mediaList', {querySelectorAll: () => rows});
    p.run(`currentMediaList = Array.from({length:500},(_,i)=>({media_id:String(i),media_path:'music/'+i+'.mp3',encryption:{version:1}}));
        currentMediaList[499].encryption=undefined;currentMediaList[499].browser_metadata=true;renderPlaylist()`);
    observer(rows.slice(0, 3).map(target => ({target, isIntersecting: true})));
    await new Promise(resolve => setTimeout(resolve, 10));
    assert.deepEqual(scheduled, [{id: '0', priority: false}]);
    p.run('loadSelectedMetadata(currentMediaList[400])');
    assert.deepEqual(scheduled[1], {id: '400', priority: true}, 'selection must have a slot while visible artwork is pending');
    observer(rows.slice(0, 3).map(target => ({target, isIntersecting: false})));
    observer([{target: rows[499], isIntersecting: true}]);
    releases[0](); releases[1]();
    await new Promise(resolve => setTimeout(resolve, 10));
    assert.deepEqual(scheduled.map(item => item.id), ['0', '400', '499'], 'scrolling must discard queued offscreen rows');
    releases[2]();
    await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(p.run('currentMediaList[499].cover'), 'blob:own-499');
    assert.equal(scheduled.length, 3, 'the other 497 tracks remain unread');
}
{
    const p = playerContext(async () => new Response('unused'));
    let timers = 0;
    p.context.setTimeout = (fn, delay) => { timers++; return setTimeout(fn, delay); };
    p.run('currentMediaList[0].encryption={version:1}; visibleMetadataTracks.add(currentMediaList[0]); schedulePlaylistMetadata()');
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.equal(timers, 1, 'a missing metadata script must not spin a zero-delay timer loop');
    let reads = 0;
    p.context.window.FrontierMusicMetadata = {metadata() { reads++; return Promise.resolve({}); }};
    p.run('schedulePlaylistMetadata()');
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.equal(reads, 1, 'restoring the helper permits a later visible-row read');
}
{
    const p = playerContext(async () => new Response('unused'));
    p.context.PLAYER_KIND = 'audio';
    p.context.window.IntersectionObserver = class {observe() {} disconnect() {}};
    p.context.window.FrontierMusicMetadata = {thumbnail() { throw new Error('unsupported renderer must not be queued'); }, canThumbnail: () => false};
    p.run("currentMediaList[0].cover='/api/v1/media/cover?legacy'; document.getElementById('mediaList').querySelectorAll=()=>[]; renderPlaylist()");
    assert.match(p.elements.get('mediaList').innerHTML, /src="data:image\/svg\+xml/, 'unsupported thumbnail browsers use a network-free placeholder');
    assert.doesNotMatch(p.elements.get('mediaList').innerHTML, /src="\/api\/v1\/media\/cover/, 'no original fallback download');
}
{
    const p = playerContext(async () => {throw new Error('thumbnail must not fetch media');});
    p.context.PLAYER_KIND = 'audio';
    p.context.window.requestIdleCallback = callback => setImmediate(() => callback({timeRemaining: () => 20}));
    p.context.window.cancelIdleCallback = clearImmediate;
    p.run("art.video={paused:false,seeking:false,readyState:3,currentTime:5,duration:100,buffered:{length:1,start:()=>0,end:()=>10}}; currentMediaList[0].cover='/api/v1/media/cover?low'; visibleArtworkTracks.add(currentMediaList[0])");
    let calls = 0, signal;
    p.context.image = {src: 'placeholder'};
    p.context.window.FrontierMusicMetadata = {canThumbnail: () => true, thumbnail(_source, options) {
        calls++; signal = options.signal;
        return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(new Error('cancelled'))));
    }};
    await p.run('paintPlayerThumbnail(currentMediaList[0],image)');
    assert.equal(calls, 0, 'five seconds of buffer is reserved for playback, not pictures');
    p.run('art.video.buffered.end=()=>30; void paintPlayerThumbnail(currentMediaList[0],image)');
    assert.equal(calls, 1);
    p.run('art.video.buffered.end=()=>10; pumpPlayerArtwork()'); await flush();
    assert.equal(signal.aborted, true); assert.equal(p.context.image.src, 'placeholder');
    assert.equal(p.run('art.url'), '/a'); assert.equal(p.run('art.currentTime'), 5);
    p.context.window.FrontierMusicMetadata.thumbnail = async () => {throw new Error('cache miss');};
    p.run('art.video.buffered.end=()=>30');
    await p.run('paintPlayerThumbnail(currentMediaList[0],image)');
    assert.equal(p.context.image.src, 'placeholder', 'cache miss does not load an original');
    assert.equal(p.requests.length, 0);
    p.run('document.hidden=true');
    assert.equal(p.run('playerArtworkReady()'), false, 'background pages do no optional artwork work');
}
console.log('player-cache-smoke-ok');
