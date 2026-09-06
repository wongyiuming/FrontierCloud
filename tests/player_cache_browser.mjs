// Optional real Chromium check: set PLAYWRIGHT_MODULE to an installed playwright package.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const playerSource = process.env.PLAYER_SOURCE_URL
    ? await (await fetch(process.env.PLAYER_SOURCE_URL)).text()
    : fs.readFileSync('static/js/player.js', 'utf8');
const sampleRate = 16000, seconds = 20;
const wave = Buffer.alloc(44 + sampleRate * seconds * 2);
wave.write('RIFF', 0); wave.writeUInt32LE(wave.length - 8, 4); wave.write('WAVEfmt ', 8);
wave.writeUInt32LE(16, 16); wave.writeUInt16LE(1, 20); wave.writeUInt16LE(1, 22);
wave.writeUInt32LE(sampleRate, 24); wave.writeUInt32LE(sampleRate * 2, 28);
wave.writeUInt16LE(2, 32); wave.writeUInt16LE(16, 34); wave.write('data', 36);
wave.writeUInt32LE(wave.length - 44, 40);
for (let i = 0; i < sampleRate * seconds; i++) wave.writeInt16LE(Math.round(2500 * Math.sin(i * 440 * 2 * Math.PI / sampleRate)), 44 + i * 2);
let nextRequests = 0;
const server = http.createServer((req, res) => {
    if (req.url === '/player.js') {res.setHeader('Content-Type', 'application/javascript'); res.end(playerSource); return;}
    if (req.url === '/a.wav' || req.url === '/b.wav') {
        if (req.url === '/b.wav' && ++nextRequests === 1) {res.writeHead(503); res.end('retry'); return;}
        res.setHeader('Content-Type', 'audio/wav'); res.setHeader('Content-Length', wave.length);
        res.setHeader('Cache-Control', 'no-store'); res.end(wave); return;
    }
    res.setHeader('Content-Type', 'text/html');
    res.end('<div id="artplayer" style="width:640px;height:360px"></div><div id="audioCover"></div><div id="audioDisk"></div><div id="audioBlurBg"></div>');
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const browser = await chromium.launch({channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome', headless: true, args: ['--autoplay-policy=no-user-gesture-required']});
try {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.addScriptTag({url: 'https://cdnjs.cloudflare.com/ajax/libs/artplayer/5.1.1/artplayer.js'});
    await page.addScriptTag({url: '/player.js'});
    await page.evaluate(() => {
        window.currentMediaList = ['a', 'b'].map(id => ({media_id: id, title: id, type: 'audio', url: `/${id}.wav`, cover: '', media_path: id}));
        window.playbackSessionId = 'browser-smoke';
        initPlayer(currentMediaList[0], 0);
    });
    await page.waitForFunction(() => art.currentTime > 0.1);
    await page.evaluate(() => {art.currentTime = 6;});
    await page.waitForFunction(() => nextPreload?.status === 'failed');
    await page.waitForFunction(() => nextPreload?.status === 'ready', {timeout: 15000});
    assert.equal(nextRequests, 2, 'one failure followed by one successful retry');
    await context.setOffline(true);
    const started = Date.now();
    await page.evaluate(() => playNext());
    await page.waitForFunction(() => currentIndex === 1 && art.currentTime > 0.1 && !art.video.paused, {timeout: 5000});
    const state = await page.evaluate(() => ({url: art.url, muted: art.video.muted, currentTime: art.currentTime}));
    assert.ok(state.url.startsWith('blob:'));
    assert.equal(state.muted, false);
    assert.equal(nextRequests, 2, 'offline switch must not fetch the next track again');
    console.log(JSON.stringify({result: 'player-cache-browser-ok', retryRequests: nextRequests, offlineSwitchMs: Date.now() - started, ...state}));
} finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
}
