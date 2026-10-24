import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';

const bytes=8*1024*1024, calls=[], transfers=[], progress=[], phases=[];
let failConfirmation=true,failComplete=false,interruptAt=null;
const listeners=new Map(),navigator={onLine:true};
const dispatch=name=>{for(const work of [...(listeners.get(name)||[])])work({type:name});};
const interrupt=name=>{navigator.onLine=false;dispatch(name);navigator.onLine=true;};
const document={getElementById:()=>null,createElement:()=>({setAttribute(){},append(){}})};
const context={window:{addEventListener(name,work){if(!listeners.has(name))listeners.set(name,new Set());listeners.get(name).add(work);},
    removeEventListener:(name,work)=>listeners.get(name)?.delete(work)},navigator,document,
    crypto:{subtle:{digest:async(...args)=>{const value=await webcrypto.subtle.digest(...args);
        if(interruptAt==='digest'){interruptAt=null;interrupt('offline');}return value;}}},Blob,Uint8Array,btoa,setTimeout,clearTimeout,setInterval,clearInterval,
    requestHeaders:()=>({'Content-Type':'application/json','X-CSRF-Token':'private-csrf'}),
    api:async(url,options={})=>{
        calls.push({url,...options});
        if(url.endsWith('/initialize')){if(interruptAt==='initialize'){interruptAt=null;interrupt('offline');}return {state:'uploading'};}
        if(/\/parts\/\d+$/.test(url)){
            const {checksum_sha256}=JSON.parse(options.body);assert.equal(checksum_sha256.length,44);
            if(interruptAt==='grant'){interruptAt=null;interrupt('offline');}
            return {url:'https://bucket.test/opaque-signed-part',headers:{'content-length':String(bytes),'content-type':'application/octet-stream','x-amz-checksum-sha256':checksum_sha256}};
        }
        if(url.endsWith('/parts/1/confirm')){
            if(interruptAt==='confirm'){interruptAt=null;interrupt('offline');throw new Error('lost acknowledgement');}
            if(failConfirmation){failConfirmation=false;throw new Error('lost acknowledgement');}
        }
        if(url.endsWith('/complete')){
            if(interruptAt==='complete'){interruptAt=null;interrupt('offline');}
            if(failComplete)throw new Error('completion uncertain');return {state:'published',path:'vido/folder/movie.mp4'};
        }
        return {};
    },
    XMLHttpRequest:class {
        constructor(){this.upload={};this.headers={};}
        open(method,url){assert.equal(method,'PUT');assert.ok(url.startsWith('https://bucket.test/'));this.url=url;}
        setRequestHeader(name,value){assert.notEqual(name.toLowerCase(),'content-length');this.headers[name]=value;}
        send(blob){
            assert.equal(this.withCredentials,false);assert.equal(this.headers['X-CSRF-Token'],undefined);
            assert.ok(blob.size<=bytes);transfers.push({xhr:this,blob,checksum:this.headers['x-amz-checksum-sha256']});
            queueMicrotask(()=>{if(interruptAt==='offline'||interruptAt==='pagehide'){const event=interruptAt;interruptAt=null;interrupt(event);}if(this.aborted)return;this.upload.onprogress({lengthComputable:true,loaded:blob.size,total:blob.size});this.status=200;this.onload();});
        }
        abort(){this.aborted=true;this.onabort();}
    }};
vm.createContext(context);
const admin=fs.readFileSync(new URL('../static/js/admin.js',import.meta.url),'utf8');
vm.runInContext(admin.slice(0,admin.indexOf('let clusterUpload =')),context);
vm.runInContext(fs.readFileSync(new URL('../static/js/s3-admin.js',import.meta.url),'utf8'),context);
const file=new Blob([new Uint8Array(bytes),new Uint8Array([1,2,3])]);
const result=await context.window.FrontierS3.transfer({upload_id:'a'.repeat(32),part_bytes:bytes},file,
    value=>progress.push(value),value=>phases.push(value));
assert.equal(result.state,'published');assert.equal(transfers.length,3,'lost metadata acknowledgement retries only the same bounded part');
assert.equal(transfers[0].checksum,transfers[1].checksum);
assert.deepEqual(Array.from(new Uint8Array(await transfers[2].blob.arrayBuffer())),[1,2,3]);
assert.ok(calls.every(call=>!call.body||JSON.stringify(JSON.parse(call.body)).length<150),'Master requests carry metadata only');
assert.ok(progress.slice(0,-1).every(value=>value<=.99),'100% waits for durable publication');
assert.equal(progress.at(-1),1);assert.ok(phases.includes('确认中'));assert.ok(phases.includes('校验中'));
failComplete=true;progress.length=0;
await assert.rejects(context.window.FrontierS3.transfer({upload_id:'b'.repeat(32),part_bytes:bytes},new Blob(['small']),value=>progress.push(value)),/completion uncertain/);
assert.ok(progress.every(value=>value<1),'an unconfirmed completion never displays success');

// A real offline/pagehide event must abort the active direct PUT and latch the
// batch even if connectivity returns before its rejection reaches retry logic.
failComplete=false;
for(const mode of ['offline','pagehide','grant','initialize','digest','confirm']){
    vm.runInContext('if(typeof uploadInterrupted!=="undefined")uploadInterrupted=false;',context);
    const callStart=calls.length,transferStart=transfers.length;
    const interruptedProgress=[];
    interruptAt=mode;
    await assert.rejects(context.window.FrontierS3.transfer({upload_id:'d'.repeat(32),part_bytes:bytes},file,value=>interruptedProgress.push(value)),/中断|取消/);
    const attempted=calls.slice(callStart);
    assert.equal(attempted.filter(call=>/\/parts\/\d+$/.test(call.url)).length,['initialize','digest'].includes(mode)?0:1,'never issue another grant/retry after interruption');
    assert.equal(attempted.filter(call=>call.url.endsWith('/confirm')).length,mode==='confirm'?1:0);
    assert.equal(attempted.some(call=>call.url.endsWith('/complete')),false);
    assert.equal(transfers.length-transferStart,['offline','pagehide','confirm'].includes(mode)?1:0,'a late grant must not start a direct PUT');
    if(['offline','pagehide'].includes(mode)){
        assert.equal(transfers.at(-1).xhr.aborted,true);
        const count=interruptedProgress.length;
        transfers.at(-1).xhr.upload.onprogress({lengthComputable:true,loaded:bytes,total:bytes});
        assert.equal(interruptedProgress.length,count,'late progress cannot revive a settled PUT');
    }
    assert.equal(vm.runInContext('activeUploadRequests.size',context),0,'S3 settlement unregisters every direct PUT');
}
vm.runInContext('uploadInterrupted=false;',context);
interruptAt='complete';
assert.equal((await context.window.FrontierS3.transfer({upload_id:'e'.repeat(32),part_bytes:bytes},new Blob(['complete']),()=>{})).state,'published',
    'a durable publication remains successful if interruption arrived during complete');
const afterPublished=calls.length;
await assert.rejects(context.window.FrontierS3.transfer({upload_id:'f'.repeat(32),part_bytes:bytes},file,()=>{}),/中断/);
assert.equal(calls.length,afterPublished,'a published current file does not permit the old batch to resume');

// Transfer cleanup and the subsequent cancellation both refresh this panel.
// Resolve their real API calls separately, as they can finish out of order.
class Element {
    constructor(){this.children=[];this.textContent='';}
    append(...children){this.children.push(...children);}
    replaceChildren(...children){this.children=[...children];}
    setAttribute(){}
}
const content=new Element(),panel={querySelector:()=>content},requests=[];
let controlled=false;
const panelContext={window:{},document:{getElementById:id=>id==='nodesPanel'?panel:null,createElement:()=>new Element()},
    requestHeaders:()=>({}),setTimeout,clearTimeout,setInterval,clearInterval,
    api:(url,options={})=>{
        if(!controlled)return Promise.resolve(url.endsWith('/members')?{members:[]}:{uploads:[]});
        return new Promise((resolve,reject)=>{
            const abort=()=>reject(new Error('status request aborted'));
            if(options.signal?.aborted){abort();return;}
            options.signal?.addEventListener('abort',abort,{once:true});
            const finish=work=>value=>{options.signal?.removeEventListener('abort',abort);work(value);};
            requests.push({url,options,resolve:finish(resolve),reject:finish(reject)});
        });
    }};
vm.runInNewContext(fs.readFileSync(new URL('../static/js/s3-admin.js',import.meta.url),'utf8'),panelContext);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
await settle();controlled=true;
const pending=content.children[0].children[2];
const cancelled={upload_id:'c'.repeat(32),path:'music/failed-album/interrupted.wav',state:'aborting',confirmed_bytes:bytes,expected_bytes:bytes*2};
const firstRefresh=panelContext.window.FrontierS3.refresh();
await settle();
const cancelledRefresh=panelContext.window.FrontierS3.refresh();
await settle();
// Drain in request order. The old renderer cleared the shared list for both
// member replies, then appended the same cancellation for both upload replies.
while(requests.length){
    const request=requests.shift();
    request.resolve(request.url.endsWith('/members')?{members:[]}:{uploads:[cancelled]});
    await settle();
}
await Promise.all([firstRefresh,cancelledRefresh]);
assert.equal(pending.children.length,1,'overlapping cancellation refreshes must render one pending task');
assert.match(pending.children[0].children[0].textContent,/待回收/);

const staleRefresh=panelContext.window.FrontierS3.refresh();
await settle();
const oldMembers=requests.shift();
const latestRefresh=panelContext.window.FrontierS3.refresh();
await settle();
assert.equal(requests.length,1,'a stalled refresh must not block a later cancellation refresh');
requests.shift().resolve({members:[]});
await settle();
assert.equal(pending.children.length,1,'keep the previous complete status until both new replies arrive');
requests.shift().resolve({uploads:[cancelled]});
await latestRefresh;
oldMembers.resolve({members:[]});
await settle();
requests.shift().resolve({uploads:[{...cancelled,state:'uploading'}]});
await staleRefresh;
assert.equal(pending.children.length,1);
assert.match(pending.children[0].children[0].textContent,/待回收/,'a late pre-cancellation snapshot must not overwrite newer state');

const failedRefresh=panelContext.window.FrontierS3.refresh();
const rejected=assert.rejects(failedRefresh,/temporary control failure/);
await settle();requests.shift().reject(new Error('temporary control failure'));
await rejected;
const controller=new AbortController(),recoveredRefresh=panelContext.window.FrontierS3.refresh({signal:controller.signal});
await settle();
while(requests.length){
    const request=requests.shift();
    assert.equal(request.options.signal,controller.signal,'queued refresh preserves the caller cancellation signal');
    request.resolve(request.url.endsWith('/members')?{members:[]}:{uploads:[cancelled]});
    await settle();
}
await recoveredRefresh;
assert.equal(pending.children.length,1,'an earlier failed refresh must not poison later cancellation status');

const abortedController=new AbortController(),abortedRefresh=panelContext.window.FrontierS3.refresh({signal:abortedController.signal});
const aborted=assert.rejects(abortedRefresh,/status request aborted/);
await settle();requests.shift().resolve({members:[]});
await settle();requests.shift(); // Its upload response never arrives.
abortedController.abort();await aborted;
assert.equal(pending.children.length,1,'aborting an incomplete snapshot must not blank existing status');
const emptyRefresh=panelContext.window.FrontierS3.refresh();
await settle();requests.shift().resolve({members:[]});
await settle();requests.shift().resolve({uploads:[]});
await emptyRefresh;
assert.equal(pending.children.length,0,'the latest successful empty response clears recovered pending tasks');
console.log('s3-upload-smoke-ok: bounded direct parts, retry checksum, credential isolation, confirmed progress, atomic latest cancellation status');
