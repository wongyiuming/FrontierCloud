"""Exact, bounded retirement controls, separate from ordinary node authority."""
import hashlib
import json

from app.services.federation import protocol as p
from app.services.node_retirement_fences import RetirementError
from app.services.resource_observation import digest

PATH='/internal/v1/retirement'
LIMIT=16384
FORMAT='frontiercloud-python-retirement-control'
FIELDS={'format','version','action','request_id','binding'}
BINDING_FIELDS={'version','retirement_id','relationship_id','master_id','member_id','master_key_sha256','member_key_sha256',
    'master_endpoint','member_endpoint','protocol','mode'}


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('Duplicate retirement field')
        result[key]=value
    return result


def _constant(_value):raise ValueError('Nonfinite retirement number')


def binding_shape(value):
    if (not isinstance(value,dict) or value.keys()!=BINDING_FIELDS or type(value['version']) is not int or value['version']!=1 or
            type(value['protocol']) is not int or value['protocol']!=p.PROTOCOL_VERSION or value['mode'] not in {'Direct','Relay'}):raise ValueError()
    for name in ('retirement_id','relationship_id','master_id','member_id'):
        if not isinstance(value[name],str) or not p.IDENTIFIER.fullmatch(value[name]):raise ValueError()
    if value['master_id']==value['member_id']:raise ValueError()
    for side in ('master','member'):
        checksum=value[side+'_key_sha256'];endpoint=value[side+'_endpoint']
        if not isinstance(checksum,str) or not p.OBJECT_ID.fullmatch(checksum) or not isinstance(endpoint,str) or p.endpoint(endpoint)!=endpoint:raise ValueError()


def request(raw):
    try:
        if not isinstance(raw,bytes) or len(raw)>LIMIT:raise ValueError()
        value=json.loads(raw,object_pairs_hook=_unique,parse_constant=_constant)
        if (not isinstance(value,dict) or value.keys()!=FIELDS or value['format']!=FORMAT or type(value['version']) is not int or value['version']!=1 or
                value['action'] not in {'check','begin','status','complete'} or not isinstance(value['request_id'],str) or not p.IDENTIFIER.fullmatch(value['request_id']) or
                not isinstance(value['binding'],dict) or len(p.canonical(value['binding']))>8192):raise ValueError()
        binding_shape(value['binding'])
        return value
    except (ValueError,TypeError,KeyError,RecursionError,UnicodeError):raise RetirementError('retirement_request_invalid') from None


def reply(store,value,raw,row):
    result=dict(format=FORMAT,version=1,action=value['action'],request_id=value['request_id'],request_sha256=hashlib.sha256(raw).hexdigest(),
        node_id=store.node['node_id'],**{name:row[name] for name in ('retirement_id','relationship_id','master_id','member_id','binding_sha256','phase','started_at','completed_at')},
        roots_sha256=digest(row['roots']),receipt=row['receipt'] or None)
    envelope=p.sign(store.unseal(store.node['private_key']),result)
    if len(p.canonical(envelope))>LIMIT:raise RetirementError('retirement_response_limit')
    return envelope


def preflight_reply(store,value,raw,observation):
    result=dict(format=FORMAT,version=1,action='check',request_id=value['request_id'],request_sha256=hashlib.sha256(raw).hexdigest(),
        node_id=store.node['node_id'],binding_sha256=digest(value['binding']),
        **{name:value['binding'][name] for name in ('retirement_id','relationship_id','master_id','member_id')},**observation)
    envelope=p.sign(store.unseal(store.node['private_key']),result)
    if len(p.canonical(envelope))>LIMIT:raise RetirementError('retirement_response_limit')
    return envelope


def verify_preflight(value,envelope,key,binding,now):
    from app.services import node_retirement_receipts as receipts
    try:
        if value['action']!='check' or not isinstance(envelope,dict) or envelope.keys()!={'payload','signature'} or len(p.canonical(envelope))>LIMIT:raise ValueError()
        result=p.verify(key,envelope)
        expected=dict(format=FORMAT,version=1,action='check',request_id=value['request_id'],request_sha256=hashlib.sha256(p.canonical(value)).hexdigest(),
            node_id=binding['member_id'],binding_sha256=digest(binding),**{name:binding[name] for name in ('retirement_id','relationship_id','master_id','member_id')})
        if (not isinstance(result,dict) or result.keys()!={*expected,'observed_at','history','inspection','roots_sha256'} or
                any(result.get(name)!=item for name,item in expected.items()) or type(result['version']) is not int):raise ValueError()
        observed=receipts.integer(result['observed_at'],positive=True)
        if abs(now-observed)>p.AUTH_SKEW_SECONDS:raise ValueError()
        history=result['history'];inspection=result['inspection']
        if not isinstance(history,dict) or history.keys()!={'native_tasks'}:raise ValueError()
        receipts.integer(history['native_tasks']);receipts.checksum(result['roots_sha256'])
        if not isinstance(inspection,dict) or inspection.keys()!=receipts.INSPECTION or type(inspection['version']) is not int or inspection['version']!=1:raise ValueError()
        for name in receipts.INSPECTION-{'version','roots_sha256'}:receipts.integer(inspection[name])
        receipts.checksum(inspection['roots_sha256'])
        return result
    except (ValueError,TypeError,KeyError,RecursionError,p.ProtocolError):raise RetirementError('retirement_signed_preflight_invalid') from None


def verify(value,envelope,key,binding):
    try:
        if not isinstance(envelope,dict) or envelope.keys()!={'payload','signature'} or len(p.canonical(envelope))>LIMIT:raise ValueError()
        result=p.verify(key,envelope)
        expected=dict(format=FORMAT,version=1,action=value['action'],request_id=value['request_id'],request_sha256=hashlib.sha256(p.canonical(value)).hexdigest(),
            node_id=binding['member_id'],binding_sha256=digest(binding),**{name:binding[name] for name in ('retirement_id','relationship_id','master_id','member_id')})
        if (not isinstance(result,dict) or result.keys()!={*expected,'phase','started_at','completed_at','roots_sha256','receipt'} or
                any(result.get(name)!=item for name,item in expected.items()) or type(result['version']) is not int or result['phase'] not in {'retiring','completed'} or
                any(type(result[name]) is not int or not 0<=result[name]<2**63 for name in ('started_at','completed_at')) or result['started_at']<=0 or
                not isinstance(result['roots_sha256'],str) or not p.OBJECT_ID.fullmatch(result['roots_sha256']) or
                result['phase']=='retiring' and (result['completed_at'] or result['receipt'] is not None) or
                result['phase']=='completed' and (result['completed_at']<result['started_at'] or not isinstance(result['receipt'],dict))):raise ValueError()
        return result
    except (ValueError,TypeError,KeyError,RecursionError):raise RetirementError('retirement_signed_response_invalid') from None
