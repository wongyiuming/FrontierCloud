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
    window:{addEventListener:(name,work)=>listeners.set(name,work),removeEventListener:(name,work)=>{
        if(listeners.get(name)===work)listeners.delete(name);
    }}});
vm.runInContext(['uploadRequest','uploadRaw','uploadOne'].map(pick).join('\n'),context);
const outcome=promise=>Promise.race([promise.then(()=> 'resolved',error=>'rejected:'+error.message),
    new Promise(resolve=>setTimeout(()=>resolve('pending'),30))]);
const tick=delay=>{clock+=delay;for(const [id,value] of [...timers])if(value.at<=clock){timers.delete(id);value.work();}};

let promise=context.uploadRaw('/raw',{},()=>{});let xhr=instances.at(-1);xhr.abort();
assert.match(await outcome(promise),/^rejected:.*取消/,'Aborted raw upload must settle');
assert.equal(timers.size,0);assert.equal(listeners.size,0);
xhr.upload.onprogress({loaded:999,total:1000,lengthComputable:true});
assert.equal(timers.size,0,'Late events cannot recreate a finished upload timer');

promise=context.uploadRaw('/direct',{},()=>{},true);xhr=instances.at(-1);
assert.equal(xhr.headers['X-CSRF-Token'],undefined);
assert.equal(xhr.headers['Content-Type'],'application/octet-stream');
listeners.get('offline')?.();
assert.match(await outcome(promise),/^rejected:.*中断/,'Offline upload must stop without waiting for the server');
assert.equal(xhr.aborted,true);assert.equal(timers.size,0);assert.equal(listeners.size,0);

promise=context.uploadRaw('/slow',{},()=>{});xhr=instances.at(-1);
for(let i=1;i<=5;i++){tick(600);xhr.upload.onprogress({loaded:i,total:100,lengthComputable:true});}
assert.equal(xhr.aborted,undefined,'Continuous progress must not hit a whole-upload deadline');
tick(1001);
assert.match(await outcome(promise),/^rejected:.*超时/);
assert.equal(xhr.aborted,true);assert.equal(timers.size,0);assert.equal(listeners.size,0);

promise=context.uploadOne({},()=>{});xhr=instances.at(-1);
assert.equal(xhr.headers['X-CSRF-Token'],'fixture-csrf');
assert.equal(xhr.headers['Content-Type'],undefined,'Multipart boundary belongs to the browser');
xhr.onload();assert.equal(await outcome(promise),'resolved');
assert.equal(timers.size,0);assert.equal(listeners.size,0);

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
