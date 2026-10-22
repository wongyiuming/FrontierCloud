"""Verified TLS, bounded S3 metadata operations; never a media relay."""
import asyncio
import base64
import hashlib
import re
import ssl
import xml.etree.ElementTree as ET

import httpx

from app.services.s3_signing import S3Error, Signer

XML_BYTES = 256*1024
NAMESPACE = 'http://s3.amazonaws.com/doc/2006-03-01/'
ERROR_CODES = {
    'AccessDenied':'access_denied','InvalidAccessKeyId':'credentials_invalid',
    'ExpiredToken':'credentials_expired','InvalidToken':'credentials_invalid',
    'SignatureDoesNotMatch':'signature_mismatch','AuthorizationHeaderMalformed':'region_mismatch',
    'PermanentRedirect':'region_mismatch','NoSuchBucket':'bucket_missing','NoSuchKey':'object_missing',
    'NoSuchUpload':'upload_missing','InvalidPart':'part_invalid','InvalidPartOrder':'part_invalid',
    'EntityTooSmall':'part_invalid','BadDigest':'checksum_mismatch','InvalidDigest':'checksum_mismatch',
    'PreconditionFailed':'conflict','ConditionalRequestConflict':'conflict','SlowDown':'throttled',
    'NotImplemented':'unsupported','InvalidRequest':'request_rejected',
    'InvalidArgument':'request_invalid','InternalError':'upstream_error','ServiceUnavailable':'upstream_error',
}
# Only a complete, identity-consistent S3 rejection of this create request can
# prove that no multipart upload was created. Unknown errors remain uncertain.
CREATE_REJECTIONS = {
    'AccessDenied':403,'InvalidAccessKeyId':403,'ExpiredToken':400,'InvalidToken':400,
    'SignatureDoesNotMatch':403,'AuthorizationHeaderMalformed':400,'NoSuchBucket':404,
    'InvalidRequest':400,'InvalidArgument':400,'BadDigest':400,'InvalidDigest':400,
}


class ControlError(S3Error):
    def __init__(self,code,*,status=None,uncertain=False,provider_code=None,field=None,creation_rejected=False):
        self.code,self.status,self.uncertain = 's3_'+code,status,uncertain
        self.creation_rejected = creation_rejected is True
        self.provider_code = provider_code if provider_code in ERROR_CODES else None
        self.field = field if field in {'Bucket','Key','UploadId','ETag','ChecksumSHA256','IsTruncated','NextPartNumberMarker','PartNumber','Size'} else None
        super().__init__(self.code)


def _tag(element):
    tag = element.tag
    if tag.startswith('{'):
        if not tag.startswith('{'+NAMESPACE+'}'):
            raise ControlError('response_invalid')
        tag = tag[len(NAMESPACE)+2:]
    return tag


def _xml(data):
    if len(data) > XML_BYTES:
        raise ControlError('response_too_large')
    try:
        value = data.decode('utf-8-sig')
        if re.search(r'<!\s*(?:DOCTYPE|ENTITY)',value,re.IGNORECASE):
            raise ValueError()
        root = ET.fromstring(value)
    except (ValueError,ET.ParseError,UnicodeError):
        raise ControlError('response_invalid') from None
    stack,count = [(root,0)],0
    while stack:
        element,depth = stack.pop()
        count += 1
        if count > 8192 or depth > 12:
            raise ControlError('response_invalid')
        _tag(element)
        stack.extend((child,depth+1) for child in element)
    return root


def _field(root,name,*,optional=False,maximum=2048):
    values = [child for child in root if _tag(child) == name]
    if not values and optional:
        return ''
    if optional and len(values) == 1 and not len(values[0]) and values[0].text is None:
        return ''
    if len(values) != 1 or len(values[0]) or values[0].text is None:
        raise ControlError('response_invalid',field=name)
    value = values[0].text
    if not 0 < len(value) <= maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ControlError('response_invalid',field=name)
    return value


def _number(value,minimum,maximum):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]{1,20}',value) or not minimum <= int(value) <= maximum:
        raise ControlError('response_invalid')
    return int(value)


def checksum(value,*,composite=False):
    if not isinstance(value,str) or len(value) > 64:
        raise ControlError('checksum_invalid')
    raw,separator,count = value.partition('-')
    if separator and (not composite or not re.fullmatch(r'[1-9][0-9]{0,4}',count) or int(count) > 10000):
        raise ControlError('checksum_invalid')
    try:
        decoded = base64.b64decode(raw,validate=True)
        if len(decoded) != 32 or base64.b64encode(decoded).decode() != raw:
            raise ValueError()
    except ValueError:
        raise ControlError('checksum_invalid') from None
    return value


def _etag(value):
    if not isinstance(value,str) or not re.fullmatch(r'"[A-Za-z0-9._-]{1,126}"',value):
        raise ControlError('response_invalid')
    return value


def _version(value):
    if not isinstance(value,str) or len(value.encode('utf-8')) > 1024 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ControlError('response_invalid')
    return value


class Control:
    def __init__(self,signer,*,context=None,transport=None):
        if not isinstance(signer,Signer):
            raise ControlError('configuration_invalid')
        context = ssl.create_default_context() if context is None else context
        if not isinstance(context,ssl.SSLContext) or context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise ControlError('tls_verification_required')
        self.signer = signer
        self.client = httpx.AsyncClient(verify=context,transport=transport,trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(30,connect=8),limits=httpx.Limits(max_connections=2,max_keepalive_connections=2),
            headers={'Accept-Encoding':'identity'})

    async def close(self):
        await self.client.aclose()

    async def _request(self,method,key='',*,query=(),headers=None,body=b'',root_name='',uncertain=False,creation=False):
        try:
            signed = self.signer.control(method,key,query=query,headers=headers,body=body)
        except S3Error as error:
            # This exact local failure occurs before any HTTP call. A later
            # transport/provider error with the same code cannot enter here.
            if creation and type(error) is S3Error and error.code == 's3_credentials_expired':
                raise ControlError('credentials_expired',creation_rejected=True) from None
            raise
        try:
            async with asyncio.timeout(180 if uncertain else 45):
                async with self.client.stream(method,signed.url,headers=signed.headers,content=body) as response:
                    if response.headers.get('content-encoding','identity') != 'identity':
                        raise ControlError('response_encoding',status=response.status_code,uncertain=uncertain)
                    data = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(data)+len(chunk) > XML_BYTES:
                            raise ControlError('response_too_large',status=response.status_code,uncertain=uncertain)
                        data.extend(chunk)
                    if 300 <= response.status_code < 400:
                        raise ControlError('region_mismatch',status=response.status_code)
                    try:
                        root = _xml(data) if data else None
                    except ControlError:
                        code = 'upstream_error' if response.status_code >= 400 else 'response_invalid'
                        raise ControlError(code,status=response.status_code,uncertain=uncertain) from None
                    if root is not None and _tag(root) == 'Error':
                        code = _field(root,'Code',maximum=128)
                        rejected = creation and CREATE_REJECTIONS.get(code) == response.status_code
                        if rejected:
                            try:
                                rejected = (all(_field(root,name,optional=True) in ('',expected) for name,expected in
                                    (('Bucket',self.signer.location.bucket),('BucketName',self.signer.location.bucket),('Key',key)))
                                    and not _field(root,'UploadId',optional=True)
                                    and _field(root,'Resource',optional=True) in ('','/'+self.signer.location.bucket,'/'+self.signer.location.bucket+'/'+key))
                            except ControlError:
                                rejected = False
                        raise ControlError(ERROR_CODES.get(code,'upstream_error'),status=response.status_code,
                                           uncertain=uncertain and not rejected,provider_code=code,creation_rejected=rejected)
                    if not 200 <= response.status_code < 300:
                        status = response.status_code
                        code = ({301:'region_mismatch',307:'region_mismatch',403:'access_denied',404:'not_found',
                                 409:'conflict',412:'conflict',429:'throttled'}.get(status,'upstream_error'))
                        raise ControlError(code,status=status,uncertain=uncertain)
                    if root_name and (root is None or _tag(root) != root_name):
                        raise ControlError('response_invalid',uncertain=uncertain)
                    return root,response.headers
        except (httpx.TimeoutException,TimeoutError):
            raise ControlError('timeout',uncertain=uncertain) from None
        except httpx.RequestError as error:
            chain,seen = error,set()
            tls = False
            while chain is not None and id(chain) not in seen:
                seen.add(id(chain))
                tls = tls or isinstance(chain,ssl.SSLError)
                chain = chain.__cause__ or chain.__context__
            raise ControlError('tls_error' if tls else 'connection_failed',uncertain=uncertain) from None

    async def head_bucket(self):
        await self._request('HEAD')

    async def create(self,key,session_id,object_id,*,content_type='application/octet-stream'):
        if (not isinstance(session_id,str) or not re.fullmatch(r'[a-f0-9]{32}',session_id) or
                not isinstance(object_id,str) or not re.fullmatch(r'[a-f0-9]{64}',object_id)):
            raise ControlError('identity_invalid')
        try:
            root,_ = await self._request('POST',key,query=[('uploads','')],headers={'content-type':content_type,
                'x-amz-checksum-algorithm':'SHA256','x-amz-checksum-type':'COMPOSITE',
                'x-amz-meta-fc-session':session_id,'x-amz-meta-fc-object':object_id},
                root_name='InitiateMultipartUploadResult',uncertain=True,creation=True)
            if _field(root,'Bucket') != self.signer.location.bucket or _field(root,'Key') != key:
                raise ControlError('response_identity',uncertain=True)
            return _field(root,'UploadId',maximum=2048)
        except ControlError as error:
            error.uncertain = not error.creation_rejected
            raise

    async def parts(self,key,upload_id,*,after=0,limit=1000):
        if type(after) is not int or not 0 <= after <= 10000 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ControlError('part_marker_invalid')
        root,_ = await self._request('GET',key,query=[('uploadId',upload_id),('part-number-marker',str(after)),('max-parts',str(limit))],root_name='ListPartsResult')
        if _field(root,'Bucket') != self.signer.location.bucket or _field(root,'Key') != key or _field(root,'UploadId') != upload_id:
            raise ControlError('response_identity')
        result,last = [],after
        for element in root:
            if _tag(element) != 'Part':
                continue
            number = _number(_field(element,'PartNumber'),1,10000)
            if number <= last or len(result) >= limit:
                raise ControlError('part_order_invalid')
            last = number
            returned_checksum = _field(element,'ChecksumSHA256',optional=True)
            result.append(dict(number=number,bytes=_number(_field(element,'Size'),1,5*1024**3),
                etag=_etag(_field(element,'ETag')),checksum=checksum(returned_checksum) if returned_checksum else None))
        truncated = _field(root,'IsTruncated')
        if truncated not in {'true','false'}:
            raise ControlError('response_invalid')
        marker = _field(root,'NextPartNumberMarker',optional=True)
        if truncated == 'true' and (not result or _number(marker,1,10000) != last):
            raise ControlError('part_marker_invalid')
        return dict(parts=result,complete=truncated == 'false',next=last)

    async def uploads(self,key):
        # Recovery inspects one unpredictable session-owned key, never imports
        # or enumerates unrelated objects. Ambiguous/truncated lists stay blocked.
        root,_ = await self._request('GET',query=[('uploads',''),('prefix',key),('max-uploads','32')],root_name='ListMultipartUploadsResult')
        if _field(root,'Bucket') != self.signer.location.bucket or _field(root,'Prefix',optional=True) not in ('',key):
            raise ControlError('response_identity')
        result = []
        for child in root:
            if _tag(child) != 'Upload':
                continue
            if _field(child,'Key') != key or len(result) >= 32:
                raise ControlError('response_identity')
            upload_id = _field(child,'UploadId',maximum=2048)
            if upload_id in result:
                raise ControlError('response_invalid')
            result.append(upload_id)
        if _field(root,'IsTruncated') != 'false':
            raise ControlError('recovery_list_ambiguous')
        return result

    async def complete(self,key,upload_id,parts):
        if not isinstance(parts,list) or not 1 <= len(parts) <= 10000:
            raise ControlError('parts_invalid')
        document = ET.Element('CompleteMultipartUpload',xmlns=NAMESPACE)
        for expected,value in enumerate(parts,1):
            if (not isinstance(value,dict) or value.keys() != {'number','etag','checksum'} or
                    type(value['number']) is not int or value['number'] != expected):
                raise ControlError('parts_invalid')
            part = ET.SubElement(document,'Part')
            for name,text in (('PartNumber',str(expected)),('ETag',_etag(value['etag'])),('ChecksumSHA256',checksum(value['checksum']))):
                ET.SubElement(part,name).text = text
        body = ET.tostring(document,encoding='utf-8')
        root,headers = await self._request('POST',key,query=[('uploadId',upload_id)],headers={'content-type':'application/xml',
            'if-none-match':'*'},body=body,root_name='CompleteMultipartUploadResult',uncertain=True)
        try:
            if _field(root,'Bucket') != self.signer.location.bucket or _field(root,'Key') != key:
                raise ControlError('response_identity',uncertain=True)
            returned = checksum(_field(root,'ChecksumSHA256'),composite=True)
            expected = base64.b64encode(hashlib.sha256(b''.join(base64.b64decode(value['checksum']) for value in parts)).digest()).decode()+'-'+str(len(parts))
            if returned != expected:
                raise ControlError('checksum_mismatch',uncertain=True)
            return dict(etag=_etag(_field(root,'ETag')),checksum=returned,
                        version_id=_version(headers.get('x-amz-version-id','')))
        except ControlError as error:
            error.uncertain = True
            raise

    async def head(self,key,*,version_id=''):
        query = [('versionId',_version(version_id))] if version_id else []
        _,headers = await self._request('HEAD',key,query=query,headers={'x-amz-checksum-mode':'ENABLED'})
        if version_id and headers.get('x-amz-version-id') != version_id:
            raise ControlError('response_identity')
        return dict(bytes=_number(headers.get('content-length'),0,2**63-1),etag=_etag(headers.get('etag')),
            checksum=checksum(headers.get('x-amz-checksum-sha256'),composite=True),
            checksum_type=headers.get('x-amz-checksum-type',''),version_id=_version(headers.get('x-amz-version-id','')),
            session_id=headers.get('x-amz-meta-fc-session',''),object_id=headers.get('x-amz-meta-fc-object',''))

    async def abort(self,key,upload_id):
        await self._request('DELETE',key,query=[('uploadId',upload_id)],uncertain=True)

    async def delete(self,key,*,version_id=''):
        await self._request('DELETE',key,query=[('versionId',version_id)] if version_id else [],uncertain=True)
