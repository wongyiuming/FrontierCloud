"""Real elapsed-hour cleanup through a live Nginx Relay, then GUI retirement."""
import hashlib
import io
import json
import secrets
import socket
import ssl
import threading
import time
import wave

from tests import federation_stack as fleet
from tests.migration_media_audit import collect_media_audits


READER_PATH='music/migration-reader/reader.wav'


def prepare_reader(master,source,root,report):
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as output:
        output.setparams((1,2,8000,0,'NONE','not compressed'))
        output.writeframes(b'\x01\x00'*(16*1024**2))
    payload=buffer.getvalue()
    row=master.upload_media(payload,'reader.wav',site_type='direct',target_dir='music/migration-reader')
    assert row['path']==READER_PATH
    master.mode('Relay',relation=source.relation)
    stream=socket.socket();stream.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4096)
    stream.settimeout(3);stream.connect((master.host,443))
    stream=ssl.create_default_context(cafile=str(root/'test-ca/root.crt')).wrap_socket(stream,server_hostname=master.hostname)
    stream.sendall(('GET '+row['url']+' HTTP/1.1\r\nHost: '+master.hostname+'\r\nConnection: close\r\n\r\n').encode())
    header=bytearray()
    while not header.endswith(b'\r\n\r\n'):
        part=stream.recv(1)
        if not part:raise AssertionError('Relay closed before its response headers')
        header.extend(part)
        if len(header)>16384:raise AssertionError('Unbounded Relay response headers')
    assert header.startswith(b'HTTP/1.1 200 '),header.split(b'\r\n',1)[0]
    stop=threading.Event();view=dict(bytes_read=0,eof=False,error='',started_at=time.time())
    stream.settimeout(1)
    def drain():
        while not stop.wait(10):
            try:
                chunk=stream.recv(16384)
                if not chunk:view['eof']=True;return
                view['bytes_read']+=len(chunk)
            except socket.timeout:continue
            except OSError as error:view['error']=type(error).__name__;return
    thread=threading.Thread(target=drain,name='fixture-slow-relay-reader',daemon=True);thread.start()
    report['held_relay_input']=dict(method='GET',path=row['url'],hostname=master.hostname,
        certificate_verified=True,expected_bytes=len(payload),source_sha256=hashlib.sha256(payload).hexdigest(),
        receive_buffer=4096,read_bytes_per_interval=16384,read_interval_seconds=10)
    return stream,stop,thread,view


def close_reader(reader):
    stream,stop,thread,_view=reader;stop.set()
    try:stream.shutdown(socket.SHUT_RDWR)
    except OSError:pass
    stream.close();thread.join(3)
    assert not thread.is_alive()


def observe(master):
    return json.loads(master.web('''import asyncio,json
from sqlalchemy import select
from app.core.db import engine
from app.services import resource_migration_schema as s
async def read():
    async with engine.connect() as conn:
        tasks=[{key:row[key] for key in ('task_id','state','error_code')} for row in (await conn.execute(select(s.tasks))).mappings()]
        units=[{key:row[key] for key in ('unit_id','parent_path','state','committed_at','cleanup_after','publication')}
            for row in (await conn.execute(select(s.units))).mappings()]
        for unit in units:
            unit['cleanup_phase']=unit.pop('publication').get('source_cleanup_reply',{}).get('phase','')
        return dict(tasks=tasks,units=units)
print(json.dumps(asyncio.run(read())))
'''))


def finish(page,master,source,target,root,reader,report,check):
    began=time.monotonic();initial=observe(master)
    assert len(initial['tasks'])==1 and len(initial['units'])==3,initial
    deadline=max(row['cleanup_after'] for row in initial['units'])
    check('gui_terminal_cleanup_keeps_the_real_hour',all(row['cleanup_after']-row['committed_at']==3600
        for row in initial['units']) and time.time()<deadline)
    view=reader[3];samples=[];last_report=0
    while time.time()<deadline+180:
        page.wait_for_timeout(15000)
        assert not view['eof'] and not view['error'],view
        current=observe(master)
        held=next(row for row in current['units'] if row['parent_path']=='music/migration-reader')
        samples.append(dict(observed_at=time.time(),task_state=current['tasks'][0]['state'],
            held_state=held['state'],cleanup_phase=held['cleanup_phase'],bytes_read=view['bytes_read']))
        if time.monotonic()-last_report>60:
            print(json.dumps(dict(actual_hour_pending=True,remaining_seconds=max(0,round(deadline-time.time())),
                cleanup_phase=held['cleanup_phase'])),flush=True);last_report=time.monotonic()
        if time.time()>=deadline and any(row['cleanup_phase']=='waiting_reader' for row in current['units']):break
        assert current['tasks'][0]['state']!='failed',current
    else:raise AssertionError('Real Nginx reader wait was not observed after the original hour')
    check('actual_nginx_relay_reader_blocks_source_cleanup_after_capability_expiry',
        time.time()>view['started_at']+310 and (source.data/'media'/READER_PATH).is_file()
        and 0<view['bytes_read']<report['held_relay_input']['expected_bytes'])
    live_leases=int(source.web('''import asyncio,time
from sqlalchemy import select,func
from app.core.db import engine
from app.services import resource_migration_schema as s
async def read():
    async with engine.connect() as conn:
        return await conn.scalar(select(func.count()).select_from(s.read_leases).where(
            s.read_leases.c.purpose=='playback',s.read_leases.c.expires_at>int(time.time())))
print(asyncio.run(read()))
'''))
    check('actual_nginx_reader_is_protected_without_a_live_durable_playback_lease',live_leases==0)
    with (source.data/'media'/READER_PATH).open('rb') as original:
        actual=hashlib.file_digest(original,'sha256').hexdigest()
    check('held_source_bytes_remain_unchanged_at_actual_cleanup_boundary',actual==report['held_relay_input']['source_sha256'])
    report['held_relay_reader']=dict(view)
    report['held_relay_reader'].update(live_durable_playback_leases=live_leases,
        waiting_units=[dict(unit_id=row['unit_id'],parent_path=row['parent_path']) for row in current['units'] if row['cleanup_phase']=='waiting_reader'])
    close_reader(reader)
    report['held_relay_reader']['closed_at']=time.time()
    until=time.monotonic()+180
    while time.monotonic()<until:
        page.wait_for_timeout(3000);current=observe(master)
        if current['tasks'][0]['state']=='completed':break
        assert current['tasks'][0]['state']!='failed',current
    else:raise AssertionError('Actual GUI task did not complete after closing its Relay reader')
    check('actual_gui_migration_finishes_after_reader_close',all(row['state']=='cleaned' for row in current['units'])
        and not (source.data/'media'/READER_PATH).exists() and (target.data/'media'/READER_PATH).is_file())
    heading=page.locator('#nodesPanel .module-heading')
    if heading.get_attribute('aria-expanded')!='true':heading.click()
    from tests.node_retirement_browser import run as retire
    retire(page,master,source,root,report,check)
    check('gui_retirement_accepts_real_completed_migration_history',True)
    report.update(whole_task_terminal=True,actual_hour=True,task_phase='completed',
        migration_then_retirement=True,terminal_elapsed_seconds=round(time.monotonic()-began,3),
        original_cleanup_after=deadline,terminal_observations=samples)



def read_after_retirement(browser,master,source,target,business,encrypted,report,check):
    """Fresh public capabilities and independent browser WebCrypto, after retirement."""
    target_id=target.nodes()['node_id']
    check('post_retirement_source_originals_are_all_absent',all(not (source.data/'media'/row['path']).exists() for row in business))
    relationships=[row for row in master.nodes()['relationships'] if row['peer_id']==target_id]
    check('post_retirement_target_relationship_is_active_relay',len(relationships)==1 and
        relationships[0]['state']=='active' and relationships[0]['mode']=='Relay')
    since=int(time.time())
    requests=[dict(path=row['path'],resource_id=row['resource_id'],url=row['url'],trace_id=secrets.token_hex(16)) for row in business]
    cipher_trace=secrets.token_hex(16)
    for row in requests:row['traceparent']='00-'+row['trace_id']+'-'+secrets.token_hex(8)+'-01'
    arguments=dict(items=requests,encrypted={key:encrypted[key] for key in ('path','resource_id','file_id')},
        cipher_traceparent='00-'+cipher_trace+'-'+secrets.token_hex(8)+'-01')
    context=browser.new_context()  # No old cookies, HTTP cache, keys or Service Worker.
    try:
        page=context.new_page()
        page.goto(master.endpoint+'/api/v1/media/music/category?path=music%2Fmigration-b',wait_until='domcontentloaded')
        fleet.wait_page(page,"typeof FrontierCryptoCommon!=='undefined' && typeof art!=='undefined' && art")
        page.evaluate('() => art.pause()')
        check('post_retirement_browser_context_starts_without_workers',not context.service_workers)
        result=page.evaluate("""async ({items,encrypted,cipher_traceparent}) => {
            if(navigator.serviceWorker.controller || (await navigator.serviceWorker.getRegistrations()).length)
                throw new Error('Post-retirement probe must not reuse a Service Worker');
            const common=FrontierCryptoCommon,inputs=[],ranges=[];
            const digest=async bytes=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),n=>n.toString(16).padStart(2,'0')).join('');
            const hidden=response=>['X-Media-Resource-ID','X-Media-Owner-ID','X-Media-Object-ID','X-Media-Parent-Request-ID']
                .every(name=>!response.headers.has(name));
            for(const item of items){
                const url=new URL(item.url,location.href);
                if(url.origin!==location.origin || url.pathname!=='/api/v1/media/stream'
                    || url.searchParams.get('resource_id')!==item.resource_id || url.searchParams.has('token'))
                    throw new Error('Original logical business URL changed');
                const response=await fetch(url,{headers:{Range:'bytes=3-9',Traceparent:item.traceparent},credentials:'same-origin',cache:'no-store'});
                const bytes=await response.arrayBuffer();
                inputs.push({method:'GET',path:url.pathname+url.search,range:'bytes=3-9',cache:'no-store',trace_id:item.trace_id});
                ranges.push({resource_id:item.resource_id,status:response.status,bytes:bytes.byteLength,
                    content_range:response.headers.get('content-range'),sha256:await digest(bytes),node_headers_hidden:hidden(response)});
            }
            const pair=await crypto.subtle.generateKey({name:'ECDH',namedCurve:'P-256'},false,['deriveBits']);
            const publicKey=await crypto.subtle.exportKey('raw',pair.publicKey);
            const post=async(path,body)=>{
                const serialized=JSON.stringify(body);
                const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',cache:'no-store',body:serialized});
                inputs.push({method:'POST',path,status:response.status,body_sha256:await digest(new TextEncoder().encode(serialized)),
                    ...(body.resource_id?{resource_id:body.resource_id,file_path:body.file_path}:{})});
                if(!response.ok || !response.headers.get('cache-control')?.includes('no-store'))throw new Error('Fresh public key authorization failed');
                return response.json();
            };
            const session=await post('/api/v1/media/crypto/session',{public_key:common.encode(publicKey)});
            const server=await crypto.subtle.importKey('raw',common.decode(session.public_key),{name:'ECDH',namedCurve:'P-256'},false,[]);
            const shared=await crypto.subtle.deriveBits({name:'ECDH',public:server},pair.privateKey,256);
            const hkdf=await crypto.subtle.importKey('raw',shared,'HKDF',false,['deriveKey']);
            new Uint8Array(shared).fill(0);
            const wrap=await crypto.subtle.deriveKey({name:'HKDF',hash:'SHA-256',salt:common.decode(session.salt),
                info:new TextEncoder().encode('frontiercloud:browser-wrap:v1:'+session.session_id)},hkdf,{name:'AES-GCM',length:256},false,['decrypt']);
            const object=await post('/api/v1/media/crypto/key',{session_id:session.session_id,file_path:encrypted.path,resource_id:encrypted.resource_id});
            common.validate(object.encryption);
            if(object.encryption.file_id!==encrypted.file_id || object.encryption.plaintext_size>common.CHUNK_SIZE)
                throw new Error('Original small encrypted fixture identity changed');
            const rawKey=await crypto.subtle.decrypt({name:'AES-GCM',iv:common.decode(object.key_envelope.iv),
                additionalData:new TextEncoder().encode('frontiercloud:key-envelope:v1:'+session.session_id+':'+object.encryption.file_id)},wrap,common.decode(object.key_envelope.wrapped_key));
            let key;
            try{key=await crypto.subtle.importKey('raw',rawKey,'AES-GCM',false,['decrypt']);}
            finally{new Uint8Array(rawKey).fill(0);}
            const url=new URL(object.source_url,location.href),range=common.cipherRange(object.encryption,0);
            if(url.origin!==location.origin || url.pathname!=='/api/v1/media/stream' || url.searchParams.get('resource_id')!==encrypted.resource_id
                || url.searchParams.get('file_path')!==encrypted.path || url.searchParams.has('token'))throw new Error('Key grant lost original logical identity');
            const response=await fetch(url,{headers:{Range:`bytes=${range.start}-${range.end}`,Traceparent:cipher_traceparent},credentials:'same-origin',cache:'no-store'});
            const ciphertext=await response.arrayBuffer();
            inputs.push({method:'GET',path:url.pathname+url.search,range:`bytes=${range.start}-${range.end}`,cache:'no-store',trace_id:cipher_traceparent.split('-')[1]});
            if(response.status!==206 || response.headers.get('content-range')!==`bytes ${range.start}-${range.end}/${object.encryption.ciphertext_size}`
                || ciphertext.byteLength!==range.size)throw new Error('Fresh complete ciphertext Range failed');
            const plain=await crypto.subtle.decrypt(common.parameters(object.encryption,0),key,ciphertext);
            const decoded={status:response.status,ciphertext_bytes:ciphertext.byteLength,ciphertext_sha256:await digest(ciphertext),
                plaintext_bytes:plain.byteLength,plaintext_sha256:await digest(plain),range:`bytes=${range.start}-${range.end}`,
                content_range:response.headers.get('content-range'),node_headers_hidden:hidden(response)};
            new Uint8Array(plain).fill(0);
            await post('/api/v1/media/crypto/revoke',{});
            return {ranges,decoded,inputs,service_workers:(await navigator.serviceWorker.getRegistrations()).length};
        }""",arguments)
        check('post_retirement_original_logical_ids_have_fresh_business_ranges',len(result['ranges'])==len(business) and all(
            row['resource_id']==expected['resource_id'] and row['status']==206 and row['bytes']==7
            and row['content_range']=='bytes 3-9/'+str(expected['size']) and row['sha256']==expected['range_sha256'] and row['node_headers_hidden']
            for row,expected in zip(result['ranges'],business)))
        decoded=result['decoded']
        check('post_retirement_fresh_browser_decrypts_original_target_ciphertext',decoded['ciphertext_sha256']==encrypted['ciphertext_sha256']
            and decoded['plaintext_sha256']==encrypted['plaintext_sha256'] and decoded['plaintext_bytes']==encrypted['plaintext_size']
            and decoded['node_headers_hidden'] and result['service_workers']==0)
    finally:context.close()
    expected={request['trace_id']:dict(resource_id=request['resource_id'],object_id=reference['object_id'],range='bytes=3-9')
        for request,reference in zip(requests,business)}
    expected[cipher_trace]=dict(resource_id=encrypted['resource_id'],object_id=next(row['object_id'] for row in business if row['resource_id']==encrypted['resource_id']),
        range=decoded['range'])
    # Native Web logs prove authenticated authorization/lineage, not sent bytes.
    # Fresh public browser Range/hash/WebCrypto checks above prove the payload.
    audits=collect_media_audits(fleet.command,master.container('web'),target.container('web'),since,expected,target_id)
    check('post_retirement_fresh_signed_capabilities_are_served_by_target',
        {row['trace_id'] for row in audits['target_audit']}==set(expected))
    report['post_retirement_reads']=dict(inputs=result['inputs'],ranges=result['ranges'],decrypted=decoded,**audits,
        target_audit_proves='native_authenticated_capability_lineage',
        payload_proof='fresh_public_browser_ranges_hashes_and_webcrypto',
        target_node_id=target_id,new_browser_context=True,public_browser_authorization=True,service_workers_used=False,
        old_ciphertext_cache_used=False,reference_payload_sent_to_browser=False,keys_exported_to_python=False)
