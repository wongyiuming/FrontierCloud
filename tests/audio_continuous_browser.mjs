import fs from 'node:fs';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import http from 'node:http';
import path from 'node:path';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
const require = createRequire(import.meta.url);
const option = name => { const i = process.argv.indexOf(name); return i >= 0 ? process.argv[i+1] : null; };
const {chromium} = require(option('--playwright') || 'playwright');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const fixtures = path.resolve(option('--fixtures') || '');
for (const id of ['07','08']) assert(fs.existsSync(path.join(fixtures,id+'.mp3')), 'Supply private 07.mp3 / 08.mp3 fixtures; never commit real media');
const noPrune = process.argv.includes('--no-prune');
const manual = process.argv.includes('--manual');
const disconnect = process.argv.includes('--disconnect');
let interrupted = false;
let outageUntil = 0;
const baseline = process.argv.includes('--baseline');
const tracks = (manual ? ['08','07'] : ['07','08']).map(id => ({type:'audio',title:id,media_id:id,resource_id:id,media_path:`music/test/${id}.mp3`,url:`/api/v1/media/stream?id=${id}`,cover:'',has_lyrics:false}));
let html = fs.readFileSync(root+'/static/media/audio-player.html','utf8');
const values = {MEDIA_JSON:JSON.stringify(tracks),PLAYBACK_SESSION_ID:'"isolated-audio-probe"',PLAYER_CATALOG_CONFIG_JSON:'null',STUN_URLS_JSON:'[]',WEBRTC_INTERVAL_MS:'60000',PAGE_TITLE:'Isolated audio probe',PLAYER_JS_URL:'/static/js/player.js',PLAYER_CSS_URL:'/static/css/player.css',MEDIA_BROWSER_JS_URL:'/static/js/media-browser.js',NETWORK_OBSERVATION_JS_URL:'/static/js/network-observation.js'};
for (const [key,value] of Object.entries(values)) html = html.replaceAll('{{'+key+'}}',value);
html = html.replace('</body>','<script src="/static/js/player-directory-label.js"></script></body>');
const requests = [];
const server = http.createServer((req,res) => {
    const url = new URL(req.url,'http://localhost');
    if (url.pathname === '/api/v1/media/stream') {
        const id = url.searchParams.get('id');
        if (!['07','08'].includes(id)) {res.writeHead(404);res.end();return;}
        const file = fixtures+'/'+id+'.mp3';
        const size = fs.statSync(file).size;
        const start = Number(/^bytes=(\d+)-$/.exec(req.headers.range || '')?.[1] || 0);
        requests.push({id,start});
        if (disconnect && id === '08' && interrupted && Date.now() < outageUntil) {
            res.writeHead(503); res.end('temporary outage'); return;
        }
        res.writeHead(start ? 206 : 200,{'Content-Type':'audio/mpeg','ETag':`"fixture-${id}"`,'Content-Length':size-start,...(start ? {'Content-Range':`bytes ${start}-${size-1}/${size}`} : {})});
        if (disconnect && id === '08' && !interrupted) {
            interrupted = true;
            outageUntil = Date.now()+8000;
            const broken = fs.createReadStream(file,{start,end:131071,highWaterMark:32768});
            broken.pipe(res,{end:false});
            broken.on('end',()=>setTimeout(()=>res.destroy(),50));
            return;
        }
        fs.createReadStream(file,{start,highWaterMark:32768}).pipe(res);return;
    }
    if (url.pathname.startsWith('/static/')) {
        const file = path.resolve(root,'.'+url.pathname);
        if (!file.startsWith(path.resolve(root)+path.sep) || !fs.existsSync(file)) {res.writeHead(404);res.end();return;}
        res.setHeader('Content-Type',file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'application/octet-stream');
        let content = baseline && /(?:audio-continuous-stream|player-directory-label)\.js$/.test(file)
            ? execFileSync('git', ['show', (option('--baseline-ref') || 'HEAD')+':static/js/'+path.basename(file)], {cwd:root})
            : fs.readFileSync(file);
        if (url.pathname.endsWith('audio-continuous-stream.js')) {
            if (noPrune) content = Buffer.from(content.toString().replace('async pruneBeforeActive() {', 'async pruneBeforeActive() { return;'));
            content = Buffer.from(content.toString().replace('if (this.closed || session !== this) throw error;',"console.error('APPEND_FAILURE', error.name, error.message, art.video.error?.code, art.video.error?.message); if (this.closed || session !== this) throw error;"));
        }
        res.end(content);return;
    }
    if (url.pathname.startsWith('/api/')) {res.writeHead(200,{'Content-Type':'application/json'});res.end('{"entries":[]}');return;}
    res.setHeader('Content-Type','text/html');res.end(html);
});
await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
const browser = await chromium.launch({executablePath:option('--browser') || undefined,headless:true,args:['--autoplay-policy=no-user-gesture-required']});
try {
    const page = await browser.newPage();
    const errors = [];
    page.on('console',message => {if (message.type() === 'error' || message.type() === 'warning') errors.push(message.text());});
    page.on('pageerror',error => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`,{waitUntil:'domcontentloaded'});
    await page.waitForFunction(() => typeof art !== 'undefined' && art?.video?.currentTime > 0.05,{timeout:30000});
    let state;
    const hold = [];
    for (let i=0;i<100;i++) {
        await page.waitForTimeout(250);
        state = await page.evaluate(() => {
            const video = art.video;
            const status = window.frontierCloudContinuousAudio.status();
            if (video.buffered.length && video.buffered.end(video.buffered.length-1) > video.currentTime+4) video.currentTime = video.buffered.end(video.buffered.length-1)-2;
            return {index:currentIndex,time:video.currentTime,state:status,error:video.error?.message,warnings:currentMediaList.map(v=>v.continuous_stream_skip_reason),retry:window.frontierCloudContinuousFetchRetry?.status()};
        });
        if (disconnect && interrupted && Date.now() < outageUntil && state.index === 1) {
            hold.push({index:state.index,end:state.state.active_segment?.end,warnings:state.warnings});
        }
        if (state.index === (manual ? 0 : 1) && (state.warnings.some(Boolean) || state.state.active_segment?.end !== null)) break;
    }
    const result = {mode:{baseline,noPrune,manual,disconnect},state,hold,errors,requests};
    if (baseline && !noPrune && !manual) {
        assert(state.warnings.some(Boolean), 'Baseline must reproduce the truncated-track warning');
        assert(errors.some(v => v.includes('InvalidStateError')), 'Baseline must expose the append/remove conflict');
    } else {
        assert.equal(state.state.active_segment.partial,false);
        assert(Math.abs(state.state.active_segment.duration-242.136)<1, 'Second fixture must contain the complete track');
        assert(state.warnings.every(v=>!v), 'No runtime warning or skip is allowed');
        const unexpected = disconnect
            ? errors.filter(v=>!/^Failed to load resource:.*(?:503|net::ERR_)/.test(v))
            : errors;
        assert.deepEqual(unexpected,[], 'No unexpected browser/pipeline errors allowed');
        if (disconnect) {
            assert(hold.length>0, 'Observe the active track during the outage');
            assert(hold.every(v=>v.index===1 && v.end===null && v.warnings.every(x=>!x)), 'Outage must hold the unfinished track');
            assert(requests.some(v=>v.id==='08' && v.start===131072), 'Resume at the exact delivered offset');
            assert(state.retry.resume_count>0, 'The network failure must actually be recovered');
        }
    }
    fs.writeFileSync(path.join(fixtures, `browser-${baseline ? 'baseline' : 'fixed'}${noPrune ? '-no-prune' : ''}${manual ? '-manual' : ''}${disconnect ? '-disconnect' : ''}.json`),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
} finally {
    await browser.close();
    server.close();
}
