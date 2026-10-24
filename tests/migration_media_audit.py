"""Match native Web authorization lineage; browser checks prove payload bytes."""
import json
import re
import time


def _hex(value, length):
    return isinstance(value,str) and re.fullmatch('[a-f0-9]{'+str(length)+'}',value) is not None


def _request(line, expected, owner_id, *, internal):
    if not isinstance(line,str) or len(line)>65536:return None
    try:row=json.loads(line)
    except (ValueError,UnicodeError):return None
    if not isinstance(row,dict):return None
    trace=row.get('trace_id');request=row.get('request_id')
    if not _hex(trace,32) or trace=='0'*32 or not _hex(request,32) or request=='0'*32:return None
    wanted=expected.get(trace);audit=row.get('media_audit')
    if not isinstance(wanted,dict) or not isinstance(audit,dict):return None
    resource=wanted.get('resource_id');media=wanted.get('object_id')
    if not _hex(owner_id,32) or not _hex(resource,64) or not _hex(media,64):return None
    # RealIPLogMiddleware records the application's authorized 200 response.
    # NativeStorageDelivery/Nginx applies Range later; this is not a sent-byte log.
    if (row.get('component')!='frontiercloud.http' or row.get('event')!='request_completed'
            or row.get('method')!='GET' or type(row.get('status')) is not int or row['status']!=200
            or row.get('path')!=('/internal/v1/media/'+media if internal else '/api/v1/media/stream')
            or not isinstance(wanted.get('range'),str) or row.get('range')!=wanted['range']
            or audit.get('resource_id')!=resource or audit.get('owner_id')!=owner_id or audit.get('media_id')!=media):return None
    return row,audit


def _safe(row, audit):
    return {**{key:row[key] for key in ('trace_id','request_id','method','path','status','range')},
        'media_audit':{key:audit[key] for key in ('resource_id','owner_id','media_id')}}


def match_master_request(line, expected, owner_id):
    matched=_request(line,expected,owner_id,internal=False)
    return _safe(*matched) if matched else None


def match_target_request(line, expected, owner_id, master_request_ids):
    matched=_request(line,expected,owner_id,internal=True)
    if not matched:return None
    row,audit=matched;trace=row['trace_id'];parent=audit.get('parent_request_id')
    if (audit.get('trace_id')!=trace or not _hex(parent,32) or parent=='0'*32
            or parent not in master_request_ids.get(trace,set())):return None
    result=_safe(row,audit)
    result['media_audit'].update(trace_id=trace,parent_request_id=parent)
    return result


def collect_media_audits(command, master_container, target_container, since, expected, owner_id):
    """Bounded read-only log projection shared by full and short native probes."""
    observed={};masters={};parents={};deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        raw=command('docker','logs','--since',str(since),'--tail','400',master_container,timeout=5)
        for line in raw.splitlines():
            row=match_master_request(line,expected,owner_id)
            if row:
                masters[row['trace_id'],row['request_id']]=row
                parents.setdefault(row['trace_id'],set()).add(row['request_id'])
        raw=command('docker','logs','--since',str(since),'--tail','400',target_container,timeout=5)
        for line in raw.splitlines():
            row=match_target_request(line,expected,owner_id,parents)
            if row:observed[row['trace_id']]=row
        if set(observed)==set(expected):break
        time.sleep(.2)
    return dict(master_authorization_audit=list(masters.values()),target_audit=list(observed.values()))
