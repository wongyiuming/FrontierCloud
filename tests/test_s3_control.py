"""Bounded metadata parsing, classified failures and uncertain completion."""
import base64
import hashlib
import ssl
import unittest

import httpx

from app.services.s3_control import Control, ControlError, XML_BYTES, checksum
from app.services.s3_signing import Credentials, Location, Signer


SHA = base64.b64encode(hashlib.sha256(b'original').digest()).decode()
COMPOSITE = base64.b64encode(hashlib.sha256(base64.b64decode(SHA)).digest()).decode()+'-1'


class Feed(httpx.AsyncByteStream):
    def __init__(self,value):
        self.value = value
    async def __aiter__(self):
        for offset in range(0,len(self.value),4096):
            yield self.value[offset:offset+4096]


class S3ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.reply = (200,{},b'')
        self.failure = None
        signer = Signer(Location('private-bucket','us-east-1',endpoint='https://s3.4399.com'),
                        Credentials('EXAMPLEACCESSKEY','private-secret-key-example'))
        self.control = Control(signer,transport=httpx.MockTransport(self.request))

    async def asyncTearDown(self):
        await self.control.close()

    async def request(self,request):
        self.calls.append(request)
        if self.failure is not None:
            raise self.failure
        status,headers,data = self.reply
        return httpx.Response(status,headers=headers,stream=Feed(data))

    def xml(self,name,fields,*,status=200,extra=''):
        text = '<'+name+' xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'+fields+extra+'</'+name+'>'
        self.reply = status,{},text.encode()

    async def test_create_uses_only_signed_metadata_and_checks_exact_bucket_key(self):
        self.xml('InitiateMultipartUploadResult','<Bucket>private-bucket</Bucket><Key>opaque</Key><UploadId>upload-123</UploadId>')
        value = await self.control.create('opaque','a'*32,'b'*64)
        self.assertEqual(value,'upload-123')
        request = self.calls[0]
        self.assertEqual((request.method,request.content),('POST',b''))
        self.assertEqual(request.headers['x-amz-checksum-algorithm'],'SHA256')
        self.assertEqual(request.headers['x-amz-meta-fc-session'],'a'*32)
        self.assertIn('authorization',request.headers)
        self.xml('InitiateMultipartUploadResult','<Bucket>other</Bucket><Key>opaque</Key><UploadId>upload-123</UploadId>')
        with self.assertRaises(ControlError) as error:
            await self.control.create('opaque','a'*32,'b'*64)
        self.assertEqual(error.exception.code,'s3_response_identity')
        self.assertTrue(error.exception.uncertain)

        self.xml('InitiateMultipartUploadResult','<Bucket>private-bucket</Bucket><Key>opaque</Key>')
        with self.assertRaises(ControlError) as error:
            await self.control.create('opaque','a'*32,'b'*64)
        self.assertEqual(error.exception.field,'UploadId')
        self.assertTrue(error.exception.uncertain)

    async def test_200_embedded_error_never_becomes_success_or_discloses_provider_message(self):
        self.xml('Error','<Code>InternalError</Code><Message>private-secret-key-example signed-url</Message>')
        with self.assertRaises(ControlError) as error:
            await self.control.complete('opaque','upload',[dict(number=1,etag='"part"',checksum=SHA)])
        self.assertEqual(error.exception.code,'s3_upstream_error')
        self.assertEqual(error.exception.status,200)
        self.assertTrue(error.exception.uncertain)
        self.assertNotIn('private',str(error.exception))

    async def test_known_errors_have_granular_safe_codes(self):
        for source,code,status in (('AccessDenied','access_denied',403),('ExpiredToken','credentials_expired',403),
            ('SignatureDoesNotMatch','signature_mismatch',403),('AuthorizationHeaderMalformed','region_mismatch',400),
            ('NoSuchUpload','upload_missing',404),('BadDigest','checksum_mismatch',400),('SlowDown','throttled',503)):
            self.xml('Error','<Code>'+source+'</Code><Message>private</Message>',status=status)
            with self.subTest(code=code), self.assertRaises(ControlError) as error:
                await self.control.abort('opaque','upload')
            self.assertEqual(error.exception.code,'s3_'+code)

    async def test_create_distinguishes_verified_explicit_rejection_from_unknown_outcome(self):
        for code,status in (('AccessDenied',403),('InvalidAccessKeyId',403),('ExpiredToken',400),
            ('InvalidToken',400),('SignatureDoesNotMatch',403),('AuthorizationHeaderMalformed',400),
            ('NoSuchBucket',404),('InvalidRequest',400),('InvalidArgument',400),
            ('BadDigest',400),('InvalidDigest',400)):
            for resource in ('','/private-bucket','/private-bucket/opaque'):
                echo='<Resource>'+resource+'</Resource>' if resource else ''
                self.xml('Error','<Code>'+code+'</Code><Key>opaque</Key><BucketName>private-bucket</BucketName>'+echo,status=status)
                with self.subTest(code=code,resource=resource),self.assertRaises(ControlError) as failure:
                    await self.control.create('opaque','a'*32,'b'*64)
                self.assertTrue(failure.exception.creation_rejected)
                self.assertFalse(failure.exception.uncertain)

    async def test_create_never_releases_on_ambiguous_or_inconsistent_error_response(self):
        for status,fields in ((200,'<Code>AccessDenied</Code>'),(500,'<Code>AccessDenied</Code>'),
            (503,'<Code>ExpiredToken</Code>'),
            (403,'<Code>InternalError</Code>'),(400,'<Code>AccessDenied</Code>'),
            (403,'<Code>AccessDenied</Code><Key>another-key</Key>'),
            (403,'<Code>AccessDenied</Code><Bucket>another-bucket</Bucket>'),
            (403,'<Code>AccessDenied</Code><BucketName>another-bucket</BucketName>'),
            (403,'<Code>AccessDenied</Code><UploadId>unexpected-upload</UploadId>'),
            (403,'<Code>AccessDenied</Code><Resource>/another-bucket/opaque</Resource>'),
            (403,'<Code>AccessDenied</Code><Resource>/private-bucket/another-key</Resource>'),
            (403,'<Code>AccessDenied</Code><Code>AccessDenied</Code>')):
            self.xml('Error',fields,status=status)
            with self.subTest(status=status,fields=fields),self.assertRaises(ControlError) as failure:
                await self.control.create('opaque','a'*32,'b'*64)
            self.assertFalse(getattr(failure.exception,'creation_rejected',False))
            self.assertTrue(failure.exception.uncertain)
        for status,data in ((403,b'not XML'),(403,b'<Error xmlns="https://untrusted.4399.com"><Code>AccessDenied</Code></Error>'),
                            (403,b'<Error><Code>AccessDenied</Code>'),(503,b'')):
            self.reply=status,{},data
            with self.subTest(status=status,data=data),self.assertRaises(ControlError) as failure:
                await self.control.create('opaque','a'*32,'b'*64)
            self.assertFalse(getattr(failure.exception,'creation_rejected',False))
            self.assertTrue(failure.exception.uncertain)
        self.failure=httpx.ReadTimeout('private request')
        with self.assertRaises(ControlError) as failure:
            await self.control.create('opaque','a'*32,'b'*64)
        self.assertFalse(getattr(failure.exception,'creation_rejected',False))
        self.assertTrue(failure.exception.uncertain)

    async def test_create_local_credential_expiry_proves_zero_http_calls_only_before_signing(self):
        valid_signer=self.control.signer
        self.control.signer=Signer(valid_signer.location,
            Credentials('EXAMPLEACCESSKEY','private-secret-key-example',expires_at=1020),clock=lambda:1000)
        with self.assertRaises(ControlError) as failure:
            await self.control.create('opaque','a'*32,'b'*64)
        self.assertEqual(failure.exception.code,'s3_credentials_expired')
        self.assertTrue(failure.exception.creation_rejected)
        self.assertFalse(failure.exception.uncertain)
        self.assertEqual(self.calls,[])
        # A similarly named error from the HTTP path supplies no such proof.
        self.control.signer=valid_signer
        self.failure=ControlError('credentials_expired')
        with self.assertRaises(ControlError) as failure:
            await self.control.create('opaque','a'*32,'b'*64)
        self.assertFalse(failure.exception.creation_rejected)
        self.assertTrue(failure.exception.uncertain)
        self.assertEqual(len(self.calls),1)

    async def test_explicit_rejection_is_not_general_permission_to_rewind_other_mutations(self):
        for operation in (lambda:self.control.complete('opaque','upload',[dict(number=1,etag='"part"',checksum=SHA)]),
                          lambda:self.control.abort('opaque','upload'),lambda:self.control.delete('opaque')):
            self.xml('Error','<Code>AccessDenied</Code>',status=403)
            with self.assertRaises(ControlError) as failure:
                await operation()
            self.assertFalse(getattr(failure.exception,'creation_rejected',False))
            self.assertTrue(failure.exception.uncertain)

    async def test_redirect_is_not_followed_and_unsafe_tls_is_refused(self):
        self.reply = 307,{'location':'https://untrusted.4399.com/?secret=private'},b'not XML'
        with self.assertRaisesRegex(ControlError,'region_mismatch'):
            await self.control.head_bucket()
        self.assertEqual(len(self.calls),1)
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with self.assertRaisesRegex(ControlError,'tls_verification_required'):
            Control(self.control.signer,context=context)

    async def test_timeout_and_certificate_errors_retain_uncertain_state_without_urls(self):
        self.failure = httpx.ReadTimeout('private signed request URL')
        with self.assertRaises(ControlError) as error:
            await self.control.abort('opaque','upload')
        self.assertEqual((error.exception.code,error.exception.uncertain),('s3_timeout',True))
        failure = httpx.ConnectError('private URL')
        failure.__cause__ = ssl.SSLCertVerificationError('untrusted CA')
        self.failure = failure
        with self.assertRaisesRegex(ControlError,'s3_tls_error'):
            await self.control.head_bucket()

    async def test_body_limit_dtd_invalid_utf8_namespace_and_depth_are_rejected(self):
        for data in (b'x'*(XML_BYTES+1),b'<!DOCTYPE x [<!ENTITY a "expand">]><x>&a;</x>',
            b'\xff\xfe<data/>',b'<x xmlns="https://untrusted.4399.com"/>',
            b'<a>'*16+b'</a>'*16):
            self.reply = 200,{},data
            with self.subTest(size=len(data)), self.assertRaises(ControlError):
                await self.control.create('opaque','a'*32,'b'*64)

    async def test_parts_are_bound_sorted_and_validate_returned_sha256_values(self):
        prefix = '<Bucket>private-bucket</Bucket><Key>opaque</Key><UploadId>upload</UploadId><IsTruncated>true</IsTruncated><NextPartNumberMarker>2</NextPartNumberMarker>'
        part = '<Part><PartNumber>2</PartNumber><Size>12</Size><ETag>"part"</ETag><ChecksumSHA256>'+SHA+'</ChecksumSHA256></Part>'
        self.xml('ListPartsResult',prefix+part)
        result = await self.control.parts('opaque','upload',after=1)
        self.assertEqual(result,dict(parts=[dict(number=2,bytes=12,etag='"part"',checksum=SHA)],complete=False,next=2))
        for changed in (part+part,part.replace('<Size>12','<Size>-1'),part.replace(SHA,'invalid'),
                        part.replace('<PartNumber>2','<PartNumber>1')):
            self.xml('ListPartsResult',prefix+changed)
            with self.assertRaises(ControlError):
                await self.control.parts('opaque','upload',after=1)
        self.xml('ListPartsResult',prefix+part.replace('<ChecksumSHA256>'+SHA+'</ChecksumSHA256>',''))
        value = await self.control.parts('opaque','upload',after=1)
        self.assertIsNone(value['parts'][0]['checksum'])
        with self.assertRaises(ControlError):
            await self.control.complete('opaque','upload',[{name:value['parts'][0][name] for name in ('number','etag','checksum')}])

    async def test_completion_is_conditional_and_rejects_reordered_or_missing_parts(self):
        self.xml('CompleteMultipartUploadResult','<Bucket>private-bucket</Bucket><Key>opaque</Key><ETag>"multipart-etag"</ETag><ChecksumSHA256>'+COMPOSITE+'</ChecksumSHA256>')
        value = await self.control.complete('opaque','upload',[dict(number=1,etag='"part"',checksum=SHA)])
        self.assertEqual(value['checksum'],COMPOSITE)
        self.assertEqual(self.calls[-1].headers['if-none-match'],'*')
        self.assertIn(b'<ChecksumSHA256>',self.calls[-1].content)
        for parts in ([],[dict(number=2,etag='"part"',checksum=SHA)],[dict(number=True,etag='"part"',checksum=SHA)],
                      [dict(number=1,etag='"part"')]):
            with self.assertRaises(ControlError):
                await self.control.complete('opaque','upload',parts)
        self.xml('CompleteMultipartUploadResult','<Bucket>private-bucket</Bucket><Key>opaque</Key><ETag>"multipart-etag"</ETag><ChecksumSHA256>'+SHA+'-1</ChecksumSHA256>')
        with self.assertRaises(ControlError) as error:
            await self.control.complete('opaque','upload',[dict(number=1,etag='"part"',checksum=SHA)])
        self.assertEqual(error.exception.code,'s3_checksum_mismatch')
        self.assertTrue(error.exception.uncertain)

    async def test_recovery_list_is_exact_bounded_and_does_not_import_other_keys(self):
        prefix = '<Bucket>private-bucket</Bucket><Prefix>opaque</Prefix><IsTruncated>false</IsTruncated>'
        upload = '<Upload><Key>opaque</Key><UploadId>recoverable</UploadId></Upload>'
        self.xml('ListMultipartUploadsResult',prefix+upload)
        self.assertEqual(await self.control.uploads('opaque'),['recoverable'])
        for body in (prefix+upload+upload,prefix.replace('false','true')+upload,
            prefix+upload.replace('<Key>opaque','<Key>opaque-extra')):
            self.xml('ListMultipartUploadsResult',body)
            with self.assertRaises(ControlError):
                await self.control.uploads('opaque')

    async def test_head_returns_metadata_only_and_checksum_is_not_an_etag(self):
        self.reply = 200,{'content-length':'123','etag':'"opaque-etag"','x-amz-checksum-sha256':SHA+'-1',
            'x-amz-checksum-type':'COMPOSITE','x-amz-meta-fc-session':'a'*32,'x-amz-meta-fc-object':'b'*64},b''
        value = await self.control.head('opaque')
        self.assertEqual((value['bytes'],value['etag'],value['checksum']),(123,'"opaque-etag"',SHA+'-1'))
        self.assertEqual(self.calls[-1].method,'HEAD')
        self.assertEqual(self.calls[-1].headers['x-amz-checksum-mode'],'ENABLED')
        for invalid in ('',SHA+'-0',SHA+'-10001','not sha256',None):
            with self.assertRaises(ControlError):
                checksum(invalid,composite=True)
        self.reply[1]['x-amz-version-id'] = 'v'*1025
        with self.assertRaisesRegex(ControlError,'response_invalid'):
            await self.control.head('opaque')
        self.reply[1]['x-amz-version-id'] = 'different-version'
        with self.assertRaisesRegex(ControlError,'response_identity'):
            await self.control.head('opaque',version_id='expected-version')
