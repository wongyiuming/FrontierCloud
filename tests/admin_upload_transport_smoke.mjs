import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync('static/js/admin.js','utf8');
const pick=name=>{
    const start=source.indexOf(`function ${name}(`);
    return start<0?'':source.slice(start,source.indexOf('\n}',start)+2);
};
let clock=0,sequence=0;
const timers=new Map(),listeners=new Map(),instances=[];
class Xhr {
    constructor(){this.upload={};this.headers={};this.status=200;this.response={ok:true};instances.push(this);}
    open(method,url){this.method=method;this.url=url;}
    setRequestHeader(name,value){this.headers[name]=value;}
    send(body){this.body=body;}
    abort(){this.aborted=true;this.onabort?.();}
}
const context=vm.createContext({XMLHttpRequest:Xhr,uploadLimits:{upload_inactivity_timeout:1},
    csrf:()=> 'fixture-csrf',parseXhrData:xhr=>xhr.response,formatErrorDetail:value=>String(value),
    location:{href:''},navigator:{onLine:true},
    setTimeout:(work,delay)=>{const id=++sequence;timers.set(id,{work,at:clock+delay});return id;},
    clearTimeout:id=>timers.delete(id),
    window:{addEventListener:(name,work)=>{if(!listeners.has(name))listeners.set(name,new Set());listeners.get(name).add(work);},removeEventListener:(name,work)=>listeners.get(name)?.delete(work)}});
vm.runInContext(source.slice(0,source.indexOf('let clusterUpload ='))+'\n'+['uploadRequest','uploadRaw','uploadOne'].map(pick).join('\n'),context);
const baseListeners=()=>[...listeners.values()].reduce((n,set)=>n+set.size,0);
const persistentListeners=baseListeners();
const dispatch=name=>{for(const work of [...(listeners.get(name)||[])])work({type:name});};
const outcome=promise=>Promise.race([promise.then(()=> 'resolved',error=>'rejected:'+error.message),
    new Promise(resolve=>setTimeout(()=>resolve('pending'),30))]);
const tick=delay=>{clock+=delay;for(const [id,value] of [...timers])if(value.at<=clock){timers.delete(id);value.work();}};

let promise=context.uploadRaw('/raw',{},()=>{});let xhr=instances.at(-1);xhr.abort();
assert.match(await outcome(promise),/^rejected:.*取消/,'Aborted raw upload must settle');
assert.equal(timers.size,0);assert.equal(baseListeners(),persistentListeners);
xhr.upload.onprogress({loaded:999,total:1000,lengthComputable:true});
assert.equal(timers.size,0,'Late events cannot recreate a finished upload timer');

promise=context.uploadRaw('/direct',{},()=>{},true);xhr=instances.at(-1);
assert.equal(xhr.headers['X-CSRF-Token'],undefined);
assert.equal(xhr.headers['Content-Type'],'application/octet-stream');
dispatch('offline');
vm.runInContext('if(typeof uploadInterrupted!=="undefined")uploadInterrupted=false;',context);
assert.match(await outcome(promise),/^rejected:.*中断/,'Offline upload must stop without waiting for the server');
assert.equal(xhr.aborted,true);assert.equal(timers.size,0);assert.equal(baseListeners(),persistentListeners);

promise=context.uploadRaw('/slow',{},()=>{});xhr=instances.at(-1);
for(let i=1;i<=5;i++){tick(600);xhr.upload.onprogress({loaded:i,total:100,lengthComputable:true});}
assert.equal(xhr.aborted,undefined,'Continuous progress must not hit a whole-upload deadline');
tick(1001);
assert.match(await outcome(promise),/^rejected:.*超时/);
assert.equal(xhr.aborted,true);assert.equal(timers.size,0);assert.equal(baseListeners(),persistentListeners);

promise=context.uploadOne({},()=>{});xhr=instances.at(-1);
assert.equal(xhr.headers['X-CSRF-Token'],'fixture-csrf');
assert.equal(xhr.headers['Content-Type'],undefined,'Multipart boundary belongs to the browser');
xhr.onload();assert.equal(await outcome(promise),'resolved');
assert.equal(timers.size,0);assert.equal(baseListeners(),persistentListeners);

const integrity=fs.readFileSync('static/js/admin-upload-integrity.js','utf8');
const start=integrity.indexOf('async function cancelClusterReservation(');
vm.runInContext(integrity.slice(start,integrity.indexOf('\n}',start)+2),context);
context.AbortController=AbortController;context.requestHeaders=()=>({});
let signal;
context.api=(_path,options)=>new Promise((_resolve,reject)=>{
    signal=options.signal;signal.addEventListener('abort',()=>reject(new Error('Unreachable control endpoint')),{once:true});
});
promise=context.cancelClusterReservation({upload_id:'owned-fixture',transport:'Direct'});
tick(10001);assert.equal(await outcome(promise),'resolved');assert.equal(signal.aborted,true);assert.equal(timers.size,0);
context.api=async()=>({});
context.window.FrontierS3={refresh:options=>new Promise((_resolve,reject)=>{
    signal=options.signal;signal.addEventListener('abort',()=>reject(new Error('Unreachable status endpoint')),{once:true});
})};
promise=context.cancelClusterReservation({upload_id:'owned-s3-fixture',transport:'S3'});
await new Promise(resolve=>setImmediate(resolve));
tick(10001);assert.equal(await outcome(promise),'resolved');assert.equal(signal.aborted,true);assert.equal(timers.size,0);
console.log('admin-upload-transport-smoke-ok: abort, offline, inactivity, active transfer, credential boundaries and bounded cancellation/status');


// Run the real batch coordinator and transport. An offline→online transition
// during cancellation must not quietly turn the remaining selection into a new task.
const batchStart=integrity.indexOf('runUploadTask = async function runUploadTaskWithReservationCleanup(');
const batchCode=integrity.slice(batchStart,integrity.indexOf('\n};',batchStart)+3);
async function batchCase({site='primary',encrypted=false,stage='send'}={}) {
    const events=new Map(),nodes=new Map(),records={encrypt:0,cancel:0,reserve:0,send:0,clean:0,results:[],alerts:[]};
    let interrupt=true;
    const emit=name=>{for(const work of [...(events.get(name)||[])])work({type:name});};
    const node=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',innerHTML:'',classList:{remove(){}}});return nodes.get(id);};
    const navigator={onLine:true};
    const disconnect=()=>{navigator.onLine=false;emit('offline');navigator.onLine=true;};
    const env={window:{addEventListener(name,work){if(!events.has(name))events.set(name,new Set());events.get(name).add(work);},
        removeEventListener:(name,work)=>events.get(name)?.delete(work)},navigator,
        uploadLimits:{max_upload_task_files:5,max_upload_file_size:1000,max_lyric_file_size:1000,upload_inactivity_timeout:300},
        uploadSiteType:{value:site},uploadSiteLabels:{primary:'主节点',direct:'直连',relay:'中转',s3:'S3'},
        $:node,alert:value=>records.alerts.push(value),updateMediaUploadGate(){},setProgress(){},formatSize:String,
        addUploadResult:(_name,state)=>records.results.push(state),renderTree:async()=>{},loadLyricCatalog:async()=>{},
        requestHeaders:()=>({}),csrf:()=> 'test-csrf',parseXhrData:xhr=>xhr.response,formatErrorDetail:String,
        location:{href:''},setTimeout,clearTimeout,AbortController,FormData:class{append(){}},
        XMLHttpRequest:class {
            constructor(){this.upload={};this.response={path:'music/album/first.wav'};this.status=200;}
            open(){}setRequestHeader(){}
            send(){records.send++;queueMicrotask(()=>{
                if(interrupt&&stage==='send'){interrupt=false;disconnect();}
                if(!this.aborted)this.onload();
                if(interrupt&&stage==='published'){interrupt=false;disconnect();}
            });}
            abort(){this.aborted=true;this.onabort?.();}
        },
        api:async(path,options={})=>{
            if(options.method==='DELETE'){records.cancel++;return {};}
            if(path.endsWith('/session')||path.endsWith('/s3/uploads')){
                records.reserve++;if(interrupt&&stage==='reservation'){interrupt=false;disconnect();}
                return {upload_id:'owned-'+records.reserve,transport:site==='s3'?'S3':site==='direct'?'Direct':site==='primary'?'Local':'Relay',upload_url:'/raw',path:'music/album/first.wav'};
            }
            return {path:'music/album/first.wav'};
        }};
    env.window.FrontierMediaCrypto={encryptUploadFile:async file=>{records.encrypt++;
        if(interrupt&&stage==='encryption'){interrupt=false;disconnect();}
        return {file,encryption:{opaque:true},cleanup:async()=>{records.clean++;}};}};
    env.window.FrontierS3={refresh:async()=>{},transfer:async()=>{records.send++;if(interrupt){interrupt=false;disconnect();
        if(stage!=='published')throw new Error('网络连接中断');}return {path:'music/album/first.wav'};}};
    const ctx=vm.createContext(env);
    vm.runInContext(source.slice(0,source.indexOf('let clusterUpload ='))+'\n'+['uploadRequest','uploadRaw','uploadOne'].map(pick).join('\n')+'\n'+
        'let clusterUpload=true;currentPath="music/album";function setUploadControlsDisabled(value){uploadRunning=value;}\n'+
        integrity.slice(start,integrity.indexOf('\n}',start)+2)+'\n'+batchCode,ctx);
    const files=[{name:'first.wav',size:10},{name:'second.wav',size:10}];
    const run=()=>ctx.runUploadTask(files,null,false,encrypted?'encrypted':'plain');
    if(stage==='already-offline')navigator.onLine=false;
    await run();
    if(stage==='already-offline'){
        assert.equal(records.encrypt,0);assert.equal(records.reserve,0);assert.equal(records.send,0);
        assert.match(records.alerts.join(' '),/网络/);
    }else{
        assert.equal(records.results.length,1,'the remaining file must not be processed');
        assert.equal(records.send,['send','published'].includes(stage)?1:0,'late encryption/grants must never start a transfer');
        assert.equal(records.reserve,stage==='encryption'?0:1,'an interrupted batch must not reserve its next file');
        const published=stage==='published'&&site!=='direct';
        assert.equal(records.cancel,stage==='encryption'||published?0:1,'only an unpublished current reservation is cancelled');
        assert.equal(records.encrypt,encrypted?1:0,'do not encrypt the next file after interruption');
        assert.equal(records.clean,encrypted?1:0,'temporary ciphertext is cleaned after interruption');
        assert.equal(node('uploadSummary').textContent,`已中断：成功 ${published?1:0}，失败 ${published?0:1}，未上传 1`);
    }
    assert.equal(vm.runInContext('typeof activeUploadRequests==="undefined"?0:activeUploadRequests.size',ctx),0,'settled requests leave no tracker entries');
    // Only a new explicit online selection creates a new task.
    navigator.onLine=true;interrupt=false;
    const previous=records.send;
    await ctx.runUploadTask([files[0]],null,false,'plain');
    assert.equal(records.send,previous+1,'a new online selection can upload normally');
    assert.equal(node('uploadSummary').textContent,'完成：成功 1，失败 0');
}
for(const site of ['primary','direct','relay','s3'])await batchCase({site});
for(const site of ['primary','direct','relay','s3'])await batchCase({site,stage:'published'});
for(const site of ['direct','s3'])await batchCase({site,encrypted:true});
await batchCase({encrypted:true,stage:'encryption'});
await batchCase({encrypted:true,stage:'reservation'});
await batchCase({site:'s3',encrypted:true,stage:'reservation'});
await batchCase({encrypted:true,stage:'already-offline'});

promise=context.uploadRaw('/first',{},()=>{});const firstActive=instances.at(-1);
const second=context.uploadRaw('/second',{},()=>{});const secondActive=instances.at(-1);
dispatch('pagehide');
assert.match(await outcome(promise),/^rejected:/);assert.match(await outcome(second),/^rejected:/);
assert.equal(firstActive.aborted,true);assert.equal(secondActive.aborted,true);
assert.equal(vm.runInContext('activeUploadRequests.size',context),0,'pagehide settles and unregisters every active upload');
console.log('admin-upload-batch-smoke-ok: interrupted queues, rapid reconnect, late grants, ciphertext cleanup, explicit new selection and pagehide');
