"""Real socket/Chromium Web Crypto interoperability with the published wire format.

The fixture creates ciphertext only in test code. Production handlers derive
small envelopes and deliver opaque Range bytes. Full Compose business/UI tests
separately cover upload, node authentication and the Admin login flow.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import socket
import sys
from pathlib import Path

import uvicorn
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.services import media_crypto as crypto
from tests import test_media_crypto_http as fixture


BROWSER_CODE = """async () => {
  const common = window.FrontierCryptoCommon;
  const pair = await crypto.subtle.generateKey({name:'ECDH',namedCurve:'P-256'},false,['deriveBits']);
  const publicKey = await crypto.subtle.exportKey('raw',pair.publicKey);
  const post = async (path,body) => {
    const response = await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),credentials:'same-origin'});
    if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`);
    if (!response.headers.get('cache-control').includes('no-store')) throw new Error('Key response is cacheable');
    return response.json();
  };
  const grant = await post('/api/v1/media/crypto/session',{public_key:common.encode(publicKey)});
  const server = await crypto.subtle.importKey('raw',common.decode(grant.public_key),{name:'ECDH',namedCurve:'P-256'},false,[]);
  const shared = await crypto.subtle.deriveBits({name:'ECDH',public:server},pair.privateKey,256);
  const hkdf = await crypto.subtle.importKey('raw',shared,'HKDF',false,['deriveKey']);
  const wrap = await crypto.subtle.deriveKey({name:'HKDF',hash:'SHA-256',salt:common.decode(grant.salt),info:new TextEncoder().encode('frontiercloud:browser-wrap:v1:'+grant.session_id)},hkdf,{name:'AES-GCM',length:256},false,['decrypt']);
  const object = await post('/api/v1/media/crypto/key',{session_id:grant.session_id,file_path:'lyrics/artist/browser.lrc'});
  const keyRaw = await crypto.subtle.decrypt({name:'AES-GCM',iv:common.decode(object.key_envelope.iv),additionalData:new TextEncoder().encode('frontiercloud:key-envelope:v1:'+grant.session_id+':'+object.encryption.file_id)},wrap,common.decode(object.key_envelope.wrapped_key));
  const key = await crypto.subtle.importKey('raw',keyRaw,'AES-GCM',false,['decrypt']);
  const decoded=[]; const statuses=[];
  for(let i=0;i<2;i++) {
    const range=common.cipherRange(object.encryption,i);
    const response=await fetch(object.source_url,{headers:{Range:`bytes=${range.start}-${range.end}`}});
    if(response.status!==206) throw new Error('Cipher Range failed');
    statuses.push(response.status);
    const ciphertext=await response.arrayBuffer();
    const plaintext=await crypto.subtle.decrypt(common.parameters(object.encryption,i),key,ciphertext);
    decoded.push(new Uint8Array(plaintext));
    const corrupt=new Uint8Array(ciphertext);corrupt[corrupt.length-1]^=1;
    let rejected=false;
    try { await crypto.subtle.decrypt(common.parameters(object.encryption,i),key,corrupt); } catch { rejected=true; }
    if(!rejected) throw new Error('Corrupted ciphertext authenticated');
  }
  const full=new Uint8Array(object.encryption.plaintext_size);full.set(decoded[0]);full.set(decoded[1],common.CHUNK_SIZE);
  const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',full)),byte=>byte.toString(16).padStart(2,'0')).join('');
  const selected=full.slice(common.CHUNK_SIZE-5,common.CHUNK_SIZE+29);
  const selectedDigest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',selected)),byte=>byte.toString(16).padStart(2,'0')).join('');
  const before=object.key_envelope.expires_at;
  const second=await post('/api/v1/media/crypto/key',{session_id:grant.session_id,file_path:'lyrics/artist/browser.lrc'});
  if(second.key_envelope.expires_at!==before) throw new Error('File request extended the grant');
  await post('/api/v1/media/crypto/revoke',{});
  const revoked=await fetch('/api/v1/media/crypto/key',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:grant.session_id,file_path:'lyrics/artist/browser.lrc'})});
  if(revoked.status!==401) throw new Error('Revoked binding accepted');
  return {digest,selectedDigest,statuses,revokedStatus:revoked.status,chunks:decoded.length,expiresIn:grant.expires_in};
}"""


async def main(output: Path, ready: Path | None = None):
    instance = fixture.MediaCryptoHTTPTests()
    await instance.asyncSetUp()
    server_task = None
    try:
        track_id, _, _ = await instance.register('music/artist/browser.mp3',encrypted=False)
        plain = bytes((index % 251 for index in range(crypto.CHUNK_SIZE + 57)))
        descriptor = crypto.Metadata.new(len(plain))
        key = crypto.manager.file_key(descriptor)
        cipher = b''.join(AESGCM(key).encrypt(descriptor.chunk_nonce(index),plain[index*crypto.CHUNK_SIZE:(index+1)*crypto.CHUNK_SIZE],descriptor.chunk_aad(index)) for index in range(2))
        path = 'lyrics/artist/browser.lrc'
        (instance.media/path).write_bytes(cipher)
        from app.services import media_objects
        async with instance.database.begin() as conn:
            identifier = await media_objects.ensure_object(conn,path,'lyric')
            await crypto.put_encryption(conn,identifier,descriptor)
            await conn.execute(text('INSERT INTO media_lyric_links(media_id,media_path,lyric_id,lyric_path) VALUES(:id,:track,:lyric_id,:lyric)'),
                {'id':track_id,'track':'music/artist/browser.mp3','lyric_id':identifier,'lyric':path})
        app = instance.client._transport.app
        app.mount('/static',StaticFiles(directory=str(Path(__file__).resolve().parents[1]/'static')))
        @app.get('/')
        async def page():
            return HTMLResponse('<!doctype html><html><body><script src="/static/js/compiled/media-crypto-common.js"></script></body></html>')
        listener = socket.socket()
        listener.bind(('127.0.0.1',0));listener.listen(32);listener.setblocking(False)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',lifespan='off'))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        while not server.started:
            if server_task.done():
                await server_task
            await asyncio.sleep(.01)
        if ready:
            ready.write_text(json.dumps({'url':f'http://127.0.0.1:{port}/','browser_code':BROWSER_CODE})+'\n',encoding='utf-8')
            for _ in range(1800):
                if output.exists():
                    break
                await asyncio.sleep(.1)
            else:
                raise TimeoutError('Browser fixture timed out')
            result=json.loads(output.read_text(encoding='utf-8'))
        else:
            from playwright.async_api import async_playwright
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True,args=['--no-sandbox'])
                context = await browser.new_context()
                page = await context.new_page()
                errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                await page.goto(f'http://127.0.0.1:{port}/',wait_until='networkidle')
                result = await page.evaluate(BROWSER_CODE)
                assert not errors,errors
                await browser.close()
        assert result['digest']==hashlib.sha256(plain).hexdigest(),result
        assert result['selectedDigest']==hashlib.sha256(plain[crypto.CHUNK_SIZE-5:crypto.CHUNK_SIZE+29]).hexdigest(),result
        result['compiled_common_sha256']=hashlib.sha256((Path(__file__).resolve().parents[1]/'static/js/compiled/media-crypto-common.js').read_bytes()).hexdigest()
        result['actual_socket']=True
        output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'browser_crypto':'passed','chunks':2,'range_status':206,'corruption':'rejected','revocation':401}))
    finally:
        if server_task is not None:
            server.should_exit=True
            await server_task
        await instance.asyncTearDown()


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--ready',type=Path)
    arguments=parser.parse_args()
    asyncio.run(main(arguments.output,arguments.ready))
