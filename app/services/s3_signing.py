"""Bounded S3 SigV4 control signatures and short browser object grants.

This module signs metadata; it never reads media or performs network I/O.
Do not log a grant URL or a signed request's headers.
"""
from dataclasses import dataclass
import datetime
import hashlib
import hmac
import ipaddress
import math
import re
import time
from urllib.parse import quote, urlsplit

from app.services.federation import protocol as p

MAX_GRANT_SECONDS = 300
MAX_CONTROL_BYTES = 512*1024


class S3Error(p.ProtocolError):
    def __init__(self,code):
        self.code = code if isinstance(code,str) and re.fullmatch(r's3_[a-z0-9_]{1,60}',code) else 's3_internal_error'
        super().__init__(self.code)


def _text(value, maximum):
    return isinstance(value,str) and 0 < len(value) <= maximum and all(33 <= ord(char) < 127 for char in value)


@dataclass(frozen=True,repr=False)
class Credentials:
    access_key: str
    secret_key: str
    session_token: str = ''
    expires_at: int | None = None

    def __post_init__(self):
        if (not isinstance(self.access_key,str) or not re.fullmatch(r'[A-Za-z0-9]{8,128}',self.access_key) or
                not _text(self.secret_key,256) or len(self.secret_key) < 16 or
                not isinstance(self.session_token,str) or (self.session_token and not _text(self.session_token,8192)) or
                (self.expires_at is not None and (type(self.expires_at) is not int or self.expires_at <= 0)) or
                (self.session_token and self.expires_at is None)):
            raise S3Error('s3_credentials_invalid')


@dataclass(frozen=True)
class Location:
    bucket: str
    region: str
    endpoint: str = ''
    style: str = 'auto'
    prefix: str = ''

    def __post_init__(self):
        if (not isinstance(self.bucket,str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]',self.bucket) or
                '..' in self.bucket or '.-' in self.bucket or '-.' in self.bucket or
                self.bucket.startswith(('xn--','sthree-','amzn-s3-demo-')) or
                self.bucket.endswith(('-s3alias','--ol-s3','.mrap','--x-s3','--table-s3')) or
                not isinstance(self.region,str) or not re.fullmatch(r'[a-z]{2}(?:-[a-z0-9]+){1,4}-[0-9]',self.region) or
                not isinstance(self.endpoint,str) or not isinstance(self.style,str) or
                self.style not in {'auto','path','virtual'} or not isinstance(self.prefix,str) or
                len(self.prefix) > 256 or (self.prefix and any(not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9._-]*',part)
                                                            for part in self.prefix.split('/')))):
            raise S3Error('s3_location_invalid')
        try:
            ipaddress.ip_address(self.bucket)
        except ValueError:
            pass
        else:
            raise S3Error('s3_bucket_invalid')
        custom = bool(self.endpoint)
        suffix = 'amazonaws.com.cn' if self.region.startswith('cn-') else 'amazonaws.com'
        origin = self.endpoint or f'https://s3.{self.region}.{suffix}'
        try:
            origin = p.endpoint(origin)
        except (p.ProtocolError,TypeError,AttributeError,UnicodeError) as exc:
            raise S3Error('s3_endpoint_invalid') from exc
        style = self.style if self.style != 'auto' else ('path' if custom or '.' in self.bucket else 'virtual')
        if style == 'virtual':
            try:
                ipaddress.ip_address(urlsplit(origin).hostname)
            except ValueError:
                pass
            else:
                raise S3Error('s3_virtual_requires_hostname')
        object.__setattr__(self,'endpoint',origin)
        object.__setattr__(self,'style',style)

    def object_key(self,member_id,object_id):
        if (not isinstance(member_id,str) or not p.IDENTIFIER.fullmatch(member_id) or
                not isinstance(object_id,str) or not p.OBJECT_ID.fullmatch(object_id)):
            raise S3Error('s3_object_identity_invalid')
        return '/'.join(filter(None,(self.prefix,member_id,'objects',object_id)))

    def url(self,key=''):
        if (not isinstance(key,str) or len(key.encode('utf-8')) > 1024 or
                any(ord(char) < 32 or ord(char) == 127 for char in key) or '\\' in key or
                any(part in {'.','..'} for part in key.split('/'))):
            raise S3Error('s3_key_invalid')
        origin = urlsplit(self.endpoint)
        if self.style == 'virtual':
            return f'https://{self.bucket}.{origin.netloc}/'+quote(key,safe='/-_.~')
        return self.endpoint+'/'+self.bucket+'/'+quote(key,safe='/-_.~')


@dataclass(frozen=True,repr=False)
class SignedRequest:
    url: str
    headers: dict
    expires_at: int | None = None


def _query(values):
    if not isinstance(values,(tuple,list)) or len(values) > 32:
        raise S3Error('s3_query_invalid')
    pairs = []
    for pair in values:
        if (not isinstance(pair,(tuple,list)) or len(pair) != 2 or
                not isinstance(pair[0],str) or not 0 < len(pair[0]) <= 128 or
                not isinstance(pair[1],str) or len(pair[1]) > 8192 or
                pair[0].lower().startswith('x-amz-') or
                any(ord(char) < 32 or ord(char) == 127 for value in pair for char in value)):
            raise S3Error('s3_query_invalid')
        pairs.append(tuple(pair))
    if sum(len(name.encode())+len(value.encode()) for name,value in pairs) > 16*1024:
        raise S3Error('s3_query_too_large')
    return pairs


def canonical_query(values):
    return '&'.join(name+'='+value for name,value in sorted(
        (quote(name,safe='-_.~'),quote(value,safe='-_.~')) for name,value in values))


def _headers(values,host):
    if not isinstance(values,dict) or len(values) > 16:
        raise S3Error('s3_headers_invalid')
    result = {'host':host}
    allowed = {'content-type','content-length','if-match','if-none-match','range',
               'x-amz-checksum-sha256','x-amz-checksum-algorithm','x-amz-checksum-type','x-amz-checksum-mode'}
    for name,value in values.items():
        if (not isinstance(name,str) or not isinstance(value,str) or
                not re.fullmatch(r'[A-Za-z0-9-]{1,64}',name) or name.lower() in result or
                (name.lower() not in allowed and not re.fullmatch(r'x-amz-meta-fc-[a-z0-9-]{1,32}',name.lower())) or
                len(value) > 4096 or any(ord(char) < 32 and char != '\t' or ord(char) >= 127 for char in value)):
            raise S3Error('s3_headers_invalid')
        result[name.lower()] = re.sub(r'[ \t]+',' ',value.strip(' \t'))
    if sum(len(name)+len(value) for name,value in result.items()) > 8192:
        raise S3Error('s3_headers_too_large')
    return result


def _signature(secret,region,day,stamp,canonical):
    key = ('AWS4'+secret).encode()
    for value in (day,region,'s3','aws4_request'):
        key = hmac.new(key,value.encode(),hashlib.sha256).digest()
    scope = day+'/'+region+'/s3/aws4_request'
    signing = 'AWS4-HMAC-SHA256\n'+stamp+'\n'+scope+'\n'+hashlib.sha256(canonical.encode()).hexdigest()
    return hmac.new(key,signing.encode(),hashlib.sha256).hexdigest()


class Signer:
    def __init__(self,location,credentials,*,clock=time.time):
        if not isinstance(location,Location) or not isinstance(credentials,Credentials):
            raise S3Error('s3_signer_invalid')
        self.location,self.credentials,self.clock = location,credentials,clock

    def _time(self):
        now = self.clock()
        if type(now) not in (int,float) or not math.isfinite(now) or not 0 < now < 253402300799:
            raise S3Error('s3_clock_invalid')
        now = int(now)
        if self.credentials.expires_at is not None and self.credentials.expires_at-now <= 30:
            raise S3Error('s3_credentials_expired')
        stamp = datetime.datetime.fromtimestamp(now,datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        return now,stamp[:8],stamp

    def _canonical(self,method,url,query,headers,payload):
        names = ';'.join(sorted(headers))
        values = ''.join(name+':'+headers[name]+'\n' for name in sorted(headers))
        return '\n'.join((method,urlsplit(url).path,canonical_query(query),values,names,payload)),names

    def presign(self,method,key,*,query=(),headers=None,seconds=MAX_GRANT_SECONDS):
        if not isinstance(method,str) or method not in {'GET','HEAD','PUT'} or not key or type(seconds) is not int or not 1 <= seconds <= MAX_GRANT_SECONDS:
            raise S3Error('s3_grant_invalid')
        now,day,stamp = self._time()
        if self.credentials.expires_at is not None:
            seconds = min(seconds,self.credentials.expires_at-now-30)
        url = self.location.url(key)
        values = _headers({} if headers is None else headers,urlsplit(url).netloc)
        query = _query(query)
        scope = day+'/'+self.location.region+'/s3/aws4_request'
        query += [('X-Amz-Algorithm','AWS4-HMAC-SHA256'),('X-Amz-Credential',self.credentials.access_key+'/'+scope),
                  ('X-Amz-Date',stamp),('X-Amz-Expires',str(seconds)),('X-Amz-SignedHeaders',';'.join(sorted(values)))]
        if self.credentials.session_token:
            query.append(('X-Amz-Security-Token',self.credentials.session_token))
        canonical,_ = self._canonical(method,url,query,values,'UNSIGNED-PAYLOAD')
        signature = _signature(self.credentials.secret_key,self.location.region,day,stamp,canonical)
        return SignedRequest(url+'?'+canonical_query(query)+'&X-Amz-Signature='+signature,
                             {name:value for name,value in values.items() if name != 'host'},now+seconds)

    def control(self,method,key='',*,query=(),headers=None,body=b''):
        if not isinstance(method,str) or method not in {'GET','HEAD','PUT','POST','DELETE'} or not isinstance(body,bytes) or len(body) > MAX_CONTROL_BYTES:
            raise S3Error('s3_control_invalid')
        _,day,stamp = self._time()
        url = self.location.url(key)
        values = _headers({} if headers is None else headers,urlsplit(url).netloc)
        values['x-amz-date'] = stamp
        values['x-amz-content-sha256'] = hashlib.sha256(body).hexdigest()
        if self.credentials.session_token:
            values['x-amz-security-token'] = self.credentials.session_token
        query = _query(query)
        canonical,names = self._canonical(method,url,query,values,values['x-amz-content-sha256'])
        signature = _signature(self.credentials.secret_key,self.location.region,day,stamp,canonical)
        scope = day+'/'+self.location.region+'/s3/aws4_request'
        values['authorization'] = 'AWS4-HMAC-SHA256 Credential='+self.credentials.access_key+'/'+scope+', SignedHeaders='+names+', Signature='+signature
        return SignedRequest(url+('?' + canonical_query(query) if query else ''),values)
