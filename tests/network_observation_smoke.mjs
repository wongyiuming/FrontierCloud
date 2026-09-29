import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const script = fs.readFileSync(new URL('../static/js/network-observation.js', import.meta.url), 'utf8');
const diagnosticsScript = fs.readFileSync(new URL('../static/js/playback-continuity-diagnostics.js', import.meta.url), 'utf8');
new vm.Script(diagnosticsScript);
assert.doesNotMatch(script, /playback-continuity-handoff|PlaybackContinuityHandoff|loadPlaybackContinuityCore/);

for (const embedded of [true, false]) {
    const configurations = [];
    let interval;
    const urls = ['stun:ca-fixture.test:3478'];
    const context = {
        window: embedded ? {} : {frontierCloudStunUrls: urls, frontierCloudWebrtcIntervalMs: 45000},
        document: {getElementById: () => embedded ? {textContent: JSON.stringify({stun_urls: urls, interval_ms: 45000})} : null},
        RTCPeerConnection: class {
            constructor(config) { configurations.push(config); }
            createDataChannel() {}
            addEventListener() {}
            createOffer() { return Promise.resolve({}); }
            setLocalDescription() {}
        },
        setTimeout() { return 1; },
        setInterval(_callback, milliseconds) { interval = milliseconds; },
        clearTimeout() {},
    };
    vm.runInNewContext(script, context);
    assert.equal(configurations.length, 1, 'STUN must start under both JSON and legacy page configuration');
    assert.equal(configurations[0].iceServers[0].urls, urls[0]);
    assert.equal(interval, 45000);
}

await import('./audio_continuous_stream_smoke.mjs');
console.log('network-observation-smoke-ok');
