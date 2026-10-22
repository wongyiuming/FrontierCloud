import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';

const bytes=8*1024*1024, calls=[], transfers=[], progress=[], phases=[];
let failConfirmation=true,failComplete=false;
const document={getElementById:()=>null,createElement:()=>({setAttribute(){},append(){}})};
const context={window:{},document,crypto:webcrypto,Blob,Uint8Array,btoa,setTimeout,clearTimeout,setInterval,clearInterval,
    requestHeaders:()=>({'Content-Type':'application/json','X-CSRF-Token':'private-csrf'}),
    api:async(url,options={})=>{
        calls.push({url,...options});
        if(url.endsWith('/initialize'))return {state:'uploading'};
        if(/\/parts\/\d+$/.test(url)){
            const {checksum_sha256}=JSON.parse(options.body);assert.equal(checksum_sha256.length,44);
            return {url:'https://bucket.test/opaque-signed-part',headers:{'content-length':String(bytes),'content-type':'application/octet-stream','x-amz-checksum-sha256':checksum_sha256}};
        }
        if(url.endsWith('/parts/1/confirm')&&failConfirmation){failConfirmation=false;throw new Error('lost acknowledgement');}
        if(url.endsWith('/complete')){if(failComplete)throw new Error('completion uncertain');return {state:'published',path:'vido/folder/movie.mp4'};}
        return {};
    },
    XMLHttpRequest:class {
        constructor(){this.upload={};this.headers={};}
        open(method,url){assert.equal(method,'PUT');assert.ok(url.startsWith('https://bucket.test/'));this.url=url;}
        setRequestHeader(name,value){assert.notEqual(name.toLowerCase(),'content-length');this.headers[name]=value;}
        send(blob){
            assert.equal(this.withCredentials,false);assert.equal(this.headers['X-CSRF-Token'],undefined);
            assert.ok(blob.size<=bytes);transfers.push({blob,checksum:this.headers['x-amz-checksum-sha256']});
            queueMicrotask(()=>{this.upload.onprogress({lengthComputable:true,loaded:blob.size,total:blob.size});this.status=200;this.onload();});
        }
        abort(){this.onabort();}
    }};
vm.runInNewContext(fs.readFileSync(new URL('../static/js/s3-admin.js',import.meta.url),'utf8'),context);
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
console.log('s3-upload-smoke-ok: bounded direct parts, retry checksum, credential isolation, confirmed progress');
