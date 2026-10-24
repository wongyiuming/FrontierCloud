"""One-shot operator readiness/identity probe; never imported by Web startup."""
from __future__ import annotations

import hashlib
import http.client
import json
import secrets
import socket
import ssl
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import create_async_engine

from app.services import offline_upgrade as offline
from app.services.federation import protocol


def fetch(endpoint, path, *, body=True):
    parsed = urlsplit(endpoint)
    if parsed.scheme == 'https':
        connection = http.client.HTTPSConnection(parsed.hostname,parsed.port or 443,timeout=5,
            context=ssl.create_default_context())
    else:
        connection = http.client.HTTPConnection(parsed.hostname,parsed.port or 80,timeout=5)
    try:
        connection.request('GET',path,headers={'Accept':'application/json','Cache-Control':'no-cache'})
        leaf = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest() if parsed.scheme == 'https' else None
        response = connection.getresponse()
        content = response.read(65537) if body else b''
        if len(content) > 65536:
            offline.fail('probe_response_too_large','A readiness/identity response exceeded 64 KiB')
        return response.status,content,leaf
    except ssl.SSLCertVerificationError as error:
        kind = {10:'probe_certificate_expired',62:'probe_certificate_hostname',64:'probe_certificate_hostname',
            18:'probe_certificate_untrusted',19:'probe_certificate_untrusted',20:'probe_certificate_untrusted',
            21:'probe_certificate_untrusted'}.get(error.verify_code,'probe_certificate_invalid')
        offline.fail(kind,'HTTPS certificate verification failed (verify_code='+str(error.verify_code)+')')
    except ssl.SSLError:
        offline.fail('probe_tls_failed','The configured endpoint did not complete a verified TLS handshake')
    except socket.gaierror:
        offline.fail('probe_dns_failed','The configured endpoint could not be resolved')
    except (TimeoutError,socket.timeout):
        offline.fail('probe_timeout','The configured endpoint timed out')
    except ConnectionRefusedError:
        offline.fail('probe_connection_refused','The configured endpoint refused the connection')
    except (OSError,http.client.HTTPException):
        offline.fail('probe_connection_failed','The configured endpoint connection failed')
    finally:
        connection.close()


def network_probe(settings, baseline, *, fenced):
    tls = settings.TLS_ENABLED
    storage = settings.DEPLOYMENT_MODE == 'only_stroge'
    if tls:
        endpoint = protocol.endpoint(settings.STORAGE_ENDPOINT if storage else 'https://'+settings.SERVER_NAME)
    else:
        if baseline['role'] != 'Standalone' or storage:
            offline.fail('configuration_role_mismatch','Fixed node roles require verified HTTPS')
        # HTTP-only Standalone has no signed identity API. Probe its actual
        # Compose gateway, and report that narrower scope explicitly.
        endpoint = 'http://nginx'
    status,body,leaf = fetch(endpoint,'/health/ready')
    try:
        ready = json.loads(body)
    except ValueError:
        offline.fail('probe_response_invalid','Readiness did not return valid JSON')
    if not isinstance(ready,dict):
        offline.fail('probe_response_invalid','Readiness did not return a JSON object')
    if status != 200 or ready.get('status') != 'ready':
        offline.fail('probe_not_ready','Endpoint readiness failed (HTTP '+str(status)+')')
    signed = False
    if tls:
        challenge = secrets.token_hex(16)
        status,body,_leaf = fetch(endpoint,'/internal/v1/identity?challenge='+challenge)
        if status != 200:
            offline.fail('probe_identity_unavailable','Signed identity failed (HTTP '+str(status)+')')
        try:
            envelope = json.loads(body)
            public = envelope['payload']['public_key']
            if hashlib.sha256(public.encode()).hexdigest() != baseline['public_key_sha256']:
                raise ValueError('identity key changed')
            identity = protocol.verify(public,envelope)
            if (any(identity[key] != baseline[key] for key in ('node_id','role','endpoint'))
                    or identity['challenge'] != challenge or identity['protocol'] != protocol.PROTOCOL_VERSION):
                raise ValueError('identity binding changed')
        except (KeyError,TypeError,ValueError,AttributeError):
            offline.fail('probe_identity_mismatch','The served signed identity does not match the original node and fresh challenge')
        signed = True
    entry_status = None
    if not storage:
        entry_status,_body,_leaf = fetch(endpoint,'/api/v1/media/',body=False)
        expected = 503 if fenced else 200
        if entry_status != expected:
            offline.fail('probe_business_gate_mismatch','Business entry state failed (HTTP '+str(entry_status)+')')
    return {'readiness_status':200,'tls_enabled':tls,'certificate_verified':tls,'leaf_sha256':leaf,
        'signed_identity_verified':signed,'business_entry_status':entry_status,'media_read_verified':False}


async def execute(options):
    from pathlib import Path
    from app.core import db
    from app.core.config import SECRET_DIR,settings
    from app.services import runtime_gate
    root = runtime_gate.DATA_ROOT
    fenced = options.command == 'probe-fenced'
    restored = options.command == 'probe-restored'
    raw = offline.private_bytes(options.baseline,16*1024*1024)
    if hashlib.sha256(raw).hexdigest() != options.baseline_sha256:
        offline.fail('baseline_changed','The retained original baseline changed')
    baseline = json.loads(raw)
    marker = root/'.frontiercloud-native-maintenance'
    if fenced:
        if hashlib.sha256(offline.private_bytes(marker,4096)).hexdigest() != options.fence_sha256:
            offline.fail('maintenance_owner_changed','This probe requires the owned maintenance fence')
    elif any(path.exists() or path.is_symlink() for path in (marker,root/'.frontiercloud-maintenance')):
        offline.fail('maintenance_not_open','A maintenance fence still blocks opening')
    if not restored:
        await db.validate_existing_database()
    reader = db.engine
    if settings.DB_TYPE == 'sqlite':
        reader = create_async_engine(db.engine.url.set(database=Path(settings.SQLITE_PATH).absolute().as_uri(),query={'mode':'ro','uri':'true'}))
    try:
        current = await offline.inspect_database(reader,SECRET_DIR,root/'media-keys',
            baseline=baseline if fenced else None,include_tables=fenced,require_idle=fenced)
        expected_generation = baseline['generation'] if restored else db.SCHEMA_GENERATION
        if (current['generation'] != expected_generation or current['node_id'] != options.node_id
                or any(current[name] != baseline[name] for name in ('node_id','role','endpoint','public_key_sha256','keys'))):
            offline.fail('node_identity_changed','The current schema, node or keys differ from the original operation')
        proof = network_probe(settings,baseline,fenced=fenced)
        return {'version':1,'phase':'endpoint_restored' if restored else 'endpoint_fenced' if fenced else 'endpoint_open','node_id':current['node_id'],
            'generation':current['generation'],'baseline_sha256':options.baseline_sha256,'business_opened':not fenced,
            **({'original_state_verified':True,'original_tables':len(current['tables'])} if fenced else {}),**proof}
    finally:
        await reader.dispose()
        if reader is not db.engine:
            await db.engine.dispose()
