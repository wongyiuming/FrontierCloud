import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const script = fs.readFileSync(new URL('../static/js/network-observation.js', import.meta.url), 'utf8');
const diagnosticsScript = fs.readFileSync(new URL('../static/js/playback-continuity-diagnostics.js', import.meta.url), 'utf8');
const continuityDesign = fs.readFileSync(new URL('../docs/audio-continuous-stream.md', import.meta.url), 'utf8');
const continuityWiki = fs.readFileSync(new URL('../docs/wiki/Playback-Continuity.md', import.meta.url), 'utf8');
const wikiSidebar = fs.readFileSync(new URL('../docs/wiki/_Sidebar.md', import.meta.url), 'utf8');
new vm.Script(diagnosticsScript);
assert.doesNotMatch(script, /playback-continuity-handoff|PlaybackContinuityHandoff|loadPlaybackContinuityCore/);

for (const document of [continuityDesign, continuityWiki]) {
    assert.match(document, /audio\/mpeg/);
    assert.match(document, /sequence/i);
    assert.match(document, /MP3/i);
    assert.match(document, /video/i);
    assert.match(document, /affinity|placement-affinity/i);
    assert.doesNotMatch(document, /T-200 ms warm-standby.*permanent/i);
}
assert.match(continuityDesign, /no T-200 ms timer/i);
assert.match(continuityDesign, /no second standby media element/i);
assert.match(wikiSidebar, /\[Playback Continuity\]\(Playback-Continuity\)/);

const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const urls = Array.from({length: 6}, (_, i) => `stun:storage-${i}.fixture.test:${3478+i}`);
function harness(embedded, sharedLocks, stallReport = false, hidden = false) {
    const peers = [], reports = [], timers = new Map(), events = new Map();
    let timerId = 0, live = 0, maximumLive = 0;
    const window = {dispatchEvent() {}, addEventListener(name, fn) {events.set(name, fn);}};
    if (!embedded) Object.assign(window, {frontierCloudStunUrls: [...urls, urls[0]], frontierCloudWebrtcIntervalMs: 45000});
    const context = {
        window, navigator: sharedLocks ? {locks: sharedLocks} : {},
        document: {hidden, addEventListener(name, fn) {events.set(name, fn);}, getElementById: () => embedded ? {textContent: JSON.stringify({stun_urls: [...urls, urls[0]], interval_ms: 45000})} : null},
        CustomEvent: class {},
        AbortController,
        fetch(_url, options) {reports.push(JSON.parse(options.body)); return stallReport ? new Promise(() => {}) : Promise.resolve({});},
        RTCPeerConnection: class {
            constructor(config) {this.config = config; this.listeners = new Map(); this.closed = false; peers.push(this); live++; maximumLive = Math.max(maximumLive, live);}
            createDataChannel() {}
            addEventListener(name, fn) {this.listeners.set(name, fn);}
            createOffer() {return Promise.resolve({});}
            setLocalDescription() {assert.equal(this.closed, false); return Promise.resolve();}
            close() {if (!this.closed) {this.closed = true; live--;}}
            candidate(address) {this.listeners.get('icecandidate')({candidate: {type: 'srflx', address}});}
            complete() {this.listeners.get('icecandidate')({candidate: null});}
        },
        setTimeout(fn, ms) {const id = ++timerId; timers.set(id, {fn, ms}); return id;},
        clearTimeout(id) {timers.delete(id);},
    };
    function fire(ms) {const item = [...timers].find(([,value]) => value.ms === ms); assert.ok(item, `missing ${ms} timer`); timers.delete(item[0]); item[1].fn();}
    vm.runInNewContext(script, context);
    return {context, peers, reports, timers, events, fire, maximum: () => maximumLive};
}

// Background starts and interrupted visibility rounds never persist partial observations.
const background = harness(true, undefined, false, true);
await flush();
assert.equal(background.peers.length, 0);
assert.equal(background.reports.length, 0);
background.context.document.hidden = false;
background.events.get('visibilitychange')();
background.fire(45000); await flush();
assert.equal(background.peers.length, 1);
background.context.document.hidden = true;
background.events.get('visibilitychange')(); await flush();
assert.ok(background.peers[0].closed);
assert.equal(background.reports.length, 0, 'hidden sweep cannot persist a partial or false observation');
assert.equal(background.timers.size, 0);
background.context.document.hidden = false;
background.events.get('visibilitychange')();
background.fire(45000); await flush();
assert.equal(background.peers.length, 2);
background.events.get('pagehide')(); await flush();

for (const embedded of [true, false]) {
    const h = harness(embedded);
    await flush();
    for (let i = 0; i < urls.length; i++) {
        assert.equal(h.peers.length, i+1, 'exactly one sequential ICE connection starts per node');
        const peer = h.peers[i];
        assert.equal(peer.config.iceServers.length, 1);
        assert.equal(peer.config.iceServers[0].urls, urls[i]);
        assert.equal(h.timers.size, 1, 'no interval/round timer while a peer is active');
        if (i === 2) h.fire(3000); else {peer.candidate(`198.51.100.${i+1}`); peer.complete();}
        await flush();
        assert.ok(peer.closed, 'peer closes before the next node starts');
        assert.equal(h.peers.length, i+1);
        h.fire(250); await flush();
    }
    assert.equal(h.maximum(), 1, 'no concurrent node probing');
    assert.equal(h.reports.length, 1, 'one report for the complete sweep');
    assert.equal(h.reports[0].addresses.length, 5);
    assert.equal(h.reports[0].failure, null, 'one failed node does not erase other observations');
    assert.equal(h.timers.size, 1);
    h.fire(45000); await flush();
    assert.equal(h.peers.length, 7, 'next sweep starts only after previous sweep and cooldown');
    h.events.get('pagehide')(); await flush();
    assert.ok(h.peers[6].closed, 'navigation cancels active peer');
    assert.equal(h.timers.size, 0, 'navigation leaves no probes scheduled');
}
// The origin-wide lease has no queue: another tab never starts an overlapping round.
let held = false;
const locks = {async request(_name, options, fn) {
    assert.equal(options.ifAvailable, true);
    if (held) return fn(null);
    held = true; try {return await fn({name: 'lease'});} finally {held = false;}
}};
const first = harness(true, locks), second = harness(true, locks);
await flush();
assert.equal(first.peers.length, 1); assert.equal(second.peers.length, 0);
second.fire(45000); await flush(); assert.equal(second.peers.length, 0, 'repeat tick cannot bypass origin lease');
first.events.get('pagehide')(); await flush(); second.fire(45000); await flush();
assert.equal(second.peers.length, 1, 'lease releases after navigation'); second.events.get('pagehide')(); await flush();

// A persisted page restores serial probing without duplicating timers or origin leases.
const restored = harness(true, locks);
await flush();
restored.events.get('pagehide')();
restored.events.get('pageshow')({persisted: true});
await flush();
assert.equal(restored.timers.size, 1);
restored.fire(45000); await flush();
assert.equal(restored.peers.length, 2);
assert.equal(restored.maximum(), 1);
restored.events.get('pagehide')(); await flush();
assert.equal(restored.timers.size, 0);
assert.equal(held, false);

// Even a stalled fetch that ignores abort cannot retain the origin lease.
for (const navigate of [false, true]) {
    const stalled = harness(true, locks, true);
    await flush();
    for (let i = 0; i < urls.length; i++) {
        stalled.peers[i].complete(); await flush(); stalled.fire(250); await flush();
    }
    assert.equal(held, true);
    if (navigate) stalled.events.get('pagehide')(); else stalled.fire(2000);
    await flush();
    assert.equal(held, false, 'bounded report releases the origin lease');
    assert.equal(stalled.timers.size, navigate ? 0 : 1);
    if (!navigate) stalled.events.get('pagehide')();
}

await import('./audio_continuous_fetch_retry_smoke.mjs');
await import('./audio_continuous_stream_smoke.mjs');
console.log('network-observation-smoke-ok');
