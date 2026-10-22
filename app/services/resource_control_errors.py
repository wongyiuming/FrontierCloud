"""Bounded signed application errors, tied to exact authenticated request bytes."""
import hashlib
import re

from fastapi.responses import JSONResponse

from app.services.federation import protocol as p

LIMIT=4096


def response(store,*,path,raw,relation,exc,prefix):
    code=str(exc) if isinstance(exc,p.ProtocolError) and re.fullmatch(r'(?:migration|inventory|observation|registry|retirement)_[a-z0-9_]{1,80}',str(exc)) else prefix+'_request_invalid'
    if isinstance(exc,OSError):code=prefix+'_filesystem_unavailable'
    def safe_id(value):return value if isinstance(value,str) and p.IDENTIFIER.fullmatch(value) else ''
    payload=dict(format='frontiercloud-python-control-error',version=1,node_id=store.node['node_id'],
        master_id=safe_id(relation.get('peer_id')),relationship_id=safe_id(relation.get('relationship_id')),
        path=path,request_sha256=hashlib.sha256(raw).hexdigest(),status=409,code=code)
    return JSONResponse(p.sign(store.unseal(store.node['private_key']),payload),status_code=409,headers={'Cache-Control':'no-store'})


def verify(envelope,*,key,node_id,master_id,relationship_id,path,raw,status):
    if not isinstance(envelope,dict) or envelope.keys()!={'payload','signature'} or len(p.canonical(envelope))>LIMIT:
        raise p.ProtocolError('migration_peer_error_invalid')
    value=p.verify(key,envelope)
    expected=dict(format='frontiercloud-python-control-error',version=1,node_id=node_id,master_id=master_id,
        relationship_id=relationship_id,path=path,request_sha256=hashlib.sha256(raw).hexdigest(),status=status)
    if (value.keys()!={*expected,'code'} or any(value.get(name)!=item for name,item in expected.items()) or
            type(value.get('version')) is not int or type(value.get('status')) is not int or
            not isinstance(value.get('code'),str) or not re.fullmatch(r'(?:migration|inventory|observation|registry|retirement)_[a-z0-9_]{1,80}',value['code'])):
        raise p.ProtocolError('migration_peer_error_invalid')
    return value['code']
