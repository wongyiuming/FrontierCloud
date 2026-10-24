"""Actual verified HTTPS inventory; disposable Linux container only."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import resource
import ssl
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI
import httpx
from sqlalchemy import insert, text
from sqlalchemy.engine import URL
import uvicorn

from app.api import internal_nodes, internal_resource_inventory as api
from app.api import internal_resource_observation as observation_api
from app.core import db
from app.core.sqlite_compat import create_sqlite_engine
from app.services.business_traffic import BusinessTrafficMiddleware, Monitor
from app.services.federation import protocol as p, schema as s
from app.services.federation.state import State
from app.services.federation.transport import Transport
from app.services.resource_inventory import Inventory
from app.services.resource_inventory_client import InventoryClient
from app.services.resource_observation import ContentObserver
from app.services.resource_observation_client import ObservationClient
from app.services import media_crypto as crypto, recording_crypto
from tests.certificate_fixtures import TestCA


async def main(*, with_content=True):
    started, cpu = time.monotonic(), time.process_time()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        files = root/'files'
        files.mkdir()
        for index in range(1074):
            (files/f'{index:04d}.bin').write_bytes(b'original')
        ca = TestCA()
        certificate, key = ca.pair()
        (root/'chain.pem').write_bytes(certificate)
        (root/'key.pem').write_bytes(key)
        os.chmod(root/'key.pem', 0o600)
        (root/'root.pem').write_bytes(ca.root.public_bytes(serialization.Encoding.PEM))
        other = TestCA()
        (root/'other.pem').write_bytes(other.root.public_bytes(serialization.Encoding.PEM))
        database = create_sqlite_engine(URL.create('sqlite+aiosqlite', database=str(root/'node.db')))
        with patch.object(db, 'engine', database), patch.object(db, 'SECRET_DIR', root/'secrets'):
            await db.init_db()
        store = State(database, Fernet.generate_key())
        await store.initialize()
        await store.promote('Follower', 'https://node.4399.com:18743', 'fixture')
        identifier, credential = 'b'*32, p.encode(os.urandom(32))
        async with database.begin() as conn:
            await conn.execute(insert(s.relationships).values(relationship_id=identifier, peer_id='c'*32,
                peer_endpoint='https://master.4399.com', peer_key=p.public_key(p.new_key()),
                credential=store.seal(credential), direction='upstream', mode='Relay', state='active',
                status='online', last_heartbeat=1, rtt_ms=1, failures=0, recoveries=0,
                peer_version=p.APP_VERSION, protocol=p.PROTOCOL_VERSION, summary={}, created_at=1))
        media, recordings = root/'media', root/'recordings'
        source_cases = []
        if with_content:
            plain = b'ID3' + bytes(8*1024*1024)
            clear = b'private audio' * 6000
            descriptor = crypto.Metadata.new(len(clear))
            cipher = AESGCM(bytes(32)).encrypt(descriptor.chunk_nonce(0), clear, descriptor.chunk_aad(0))
            for object_id, name, data, encrypted in [('e'*64, 'plain.mp3', plain, None),
                                                     ('f'*64, 'encrypted.mp3', cipher, descriptor)]:
                relative = 'music/历史/' + name
                path = media/relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                async with database.begin() as conn:
                    await conn.execute(text('INSERT INTO media_objects VALUES (:id,:kind,:path,:locator,:created,:updated)'),
                        dict(id=object_id, kind='audio', path=relative, locator=hashlib.sha256(relative.encode()).hexdigest(),
                             created='2026-01-02 03:04:05.123456', updated='2026-02-03 04:05:06.654321'))
                    if encrypted is not None:
                        await crypto.put_encryption(conn, object_id, encrypted)
                source_cases.append(dict(resource_kind='media', object_id=object_id, user_id='', path=path,
                                         bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
            user, recording_id = '1'*32, '2'*32
            descriptor = crypto.Metadata.new(4)
            snapshot = {'encryption': descriptor.to_dict(), 'ciphertext': crypto._encode64(
                AESGCM(bytes(32)).encrypt(descriptor.chunk_nonce(0), b'line', descriptor.chunk_aad(0)))}
            footer = p.canonical(dict(version=1, title='recording', lyrics=[], encrypted_lyrics=snapshot))
            data = b'RIFF'+bytes(512*1024)+footer+len(footer).to_bytes(8, 'big')+recording_crypto.TRAILER_MAGIC
            path = recordings/identifier/user/(recording_id+'.bin')
            path.parent.mkdir(parents=True)
            path.write_bytes(data)
            source_cases.append(dict(resource_kind='recording', object_id=recording_id, user_id=user, path=path,
                                     bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
        monitor = Monitor()
        inventory = Inventory(root_provider=lambda: {'media': files}, traffic=monitor)
        observer = ContentObserver(store, root_provider=lambda: {'media': media, 'recordings': recordings}, traffic=monitor)
        private = store.unseal(store.node['private_key'])
        relation = dict(relationship_id=identifier, peer_id=store.node['node_id'],
            peer_endpoint='https://node.4399.com:18743', peer_key=p.public_key(private),
            credential=credential, direction='downstream', state='active')
        master = SimpleNamespace(node={'node_id': 'c'*32, 'role': 'Master'}, unseal=lambda value: value)
        transport = Transport()
        application = FastAPI()
        application.include_router(api.router)
        application.include_router(observation_api.router)
        application.add_middleware(BusinessTrafficMiddleware, monitor=monitor)
        server = uvicorn.Server(uvicorn.Config(application, host='127.0.0.1', port=18743,
            ssl_certfile=str(root/'chain.pem'), ssl_keyfile=str(root/'key.pem'),
            lifespan='off', log_level='error', access_log=False, workers=1))
        received = set()
        async def consume(value):
            assert value['path'] not in received
            received.add(value['path'])
        with (patch.object(internal_nodes, 'state', store), patch.object(api, 'state', store),
              patch.object(api, 'inventory', inventory), patch.object(internal_nodes.settings, 'TLS_ENABLED', True),
              patch.object(observation_api, 'observer', observer),
              patch.dict(os.environ, {'SSL_CERT_FILE': str(root/'root.pem')})):
            serving = asyncio.create_task(server.serve())
            reaper = asyncio.create_task(inventory.reap())
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if serving.done():
                            await serving
                            raise AssertionError('TLS server stopped before readiness')
                        await asyncio.sleep(.01)
                # Trust and hostname verification must fail for the wrong roots
                # and hostname; no ignored-certificate setting is used.
                rejected = []
                for url, roots in [('https://node.4399.com:18743', root/'other.pem'),
                                   ('https://wrong.4399.com:18743', root/'root.pem')]:
                    async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(roots)),
                                                  trust_env=False, timeout=2) as untrusted:
                        try:
                            await untrusted.post(url+'/internal/v1/migration/inventory/start', json={})
                            raise AssertionError('invalid TLS accepted')
                        except httpx.ConnectError as exc:
                            assert 'CERTIFICATE_VERIFY_FAILED' in str(exc), str(exc)
                            rejected.append('certificate_verification_failed')
                proof = await InventoryClient(master, relation, transport).read('media', consume)
                assert proof['count'] == 1074 and proof['bytes'] == 1074*8, proof
                assert len(received) == 1074 and inventory.session is None
                assert all(path.read_bytes() == b'original' for path in files.iterdir())
                observed = []
                with (patch.object(crypto, 'open_premaster', side_effect=AssertionError('source media key opened')),
                      patch.object(crypto.Manager, 'file_key', side_effect=AssertionError('source media key derived'))):
                    content_client = ObservationClient(master, relation, transport)
                    for case in source_cases:
                        receipt = await content_client.read(case['resource_kind'], case['object_id'], case['user_id'])
                        result = receipt['result']
                        assert result['sha256'] == case['sha256'] and result['bytes'] == case['bytes'], result
                        assert hashlib.sha256(case['path'].read_bytes()).hexdigest() == case['sha256']
                        assert 'nonce_prefix' not in json.dumps(receipt) and 'ciphertext' not in json.dumps(receipt)
                        assert observer.current is None
                        observed.append(dict(resource_kind=case['resource_kind'], bytes=result['bytes'],
                                             sha256=result['sha256'], source_unchanged=True))
                result = dict(format='frontiercloud-python-native-inventory-evidence', version=1,
                    tls='verified private test CA and DNS SAN; wrong root and hostname refused',
                    transport='actual product HTTPS control client, HMAC nonce and node signature',
                    objects=1074, bytes=proof['bytes'], transcript_sha256=proof['transcript_sha256'],
                    source_files_unchanged=True, observation_closed=True, tls_negative_cases=len(rejected),
                    content_observations=observed, source_media_keys_opened=False,
                    wall_seconds=round(time.monotonic()-started, 3),
                    cpu_seconds=round(time.process_time()-cpu, 4),
                    peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    scope='native metadata/content observation only; no admission, transfer or authority cutover proven')
                print(json.dumps(result, indent=2), flush=True)
            finally:
                await transport.close()
                await observer.stop()
                reaper.cancel()
                try:
                    await reaper
                except asyncio.CancelledError:
                    pass
                server.should_exit = True
                await asyncio.wait_for(serving, 5)
                await database.dispose()


if __name__ == '__main__':
    asyncio.run(main())
