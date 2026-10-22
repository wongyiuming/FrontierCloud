"""AWS published vectors and bounded, secret-safe S3 grant inputs."""
import unittest
from urllib.parse import parse_qs, urlsplit

from app.services.s3_signing import Credentials, Location, Signer, S3Error, _signature

# Public examples from AWS's S3 SigV4 header/query signing references.
ACCESS = 'AKIAIOSFODNN7EXAMPLE'
SECRET = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'
NOW = 1369353600


class S3SigningTests(unittest.TestCase):
    def signer(self,*,credentials=None,clock=lambda:NOW,**kwargs):
        return Signer(Location('examplebucket','us-east-1',endpoint='https://s3.amazonaws.com',style='virtual',**kwargs),
                      Credentials(ACCESS,SECRET) if credentials is None else credentials,clock=clock)

    def test_aws_published_header_vector(self):
        value = self.signer().control('GET','test.txt',headers={'Range':'bytes=0-9'})
        self.assertEqual(value.url,'https://examplebucket.s3.amazonaws.com/test.txt')
        self.assertTrue(value.headers['authorization'].endswith('Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41'))

    def test_aws_published_query_signature_vector(self):
        canonical = ('GET\n/test.txt\nX-Amz-Algorithm=AWS4-HMAC-SHA256&'
            'X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request&'
            'X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host\n'
            'host:examplebucket.s3.amazonaws.com\n\nhost\nUNSIGNED-PAYLOAD')
        self.assertEqual(_signature(SECRET,'us-east-1','20130524','20130524T000000Z',canonical),
                         'aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404')

    def test_regional_aws_dotted_bucket_and_custom_endpoint_styles(self):
        self.assertEqual(Location('bucket-name','eu-west-1').url('file'),
                         'https://bucket-name.s3.eu-west-1.amazonaws.com/file')
        self.assertEqual(Location('bucket.name','eu-west-1').url('file'),
                         'https://s3.eu-west-1.amazonaws.com/bucket.name/file')
        self.assertEqual(Location('bucket-name','cn-north-1').url('file'),
                         'https://bucket-name.s3.cn-north-1.amazonaws.com.cn/file')
        self.assertEqual(Location('bucket-name','us-east-1',endpoint='https://s3.4399.com:8333').url('file'),
                         'https://s3.4399.com:8333/bucket-name/file')

    def test_uri_encoding_preserves_raw_key_and_never_normalizes_paths(self):
        value = self.signer().presign('GET','music/刺客 100%+?.mp3')
        self.assertIn('/music/%E5%88%BA%E5%AE%A2%20100%25%2B%3F.mp3?',value.url)
        self.assertIn('/a//b%252Fc?',self.signer().presign('GET','a//b%2Fc').url)
        self.assertNotIn(SECRET,value.url)
        self.assertNotIn(SECRET,repr(value))
        for key in ('../escape','path/./escape','path/../escape','x\\y','x\ny','x'*1025):
            with self.subTest(key=key), self.assertRaises(S3Error):
                self.signer().presign('GET',key)

    def test_grants_expire_before_temporary_credentials_and_secrets_are_not_repr(self):
        credentials = Credentials(ACCESS,SECRET,'SESSION+/=',NOW+120)
        value = self.signer(credentials=credentials).presign('GET','file')
        query = parse_qs(urlsplit(value.url).query)
        self.assertEqual(query['X-Amz-Expires'],['90'])
        self.assertEqual(query['X-Amz-Security-Token'],['SESSION+/='])
        self.assertEqual(value.expires_at,NOW+90)
        self.assertNotIn(SECRET,repr(credentials))
        self.assertNotIn('SESSION',repr(credentials))
        self.assertNotIn('X-Amz',repr(value))
        for expiry in (NOW,NOW+30):
            with self.assertRaisesRegex(S3Error,'credentials_expired'):
                self.signer(credentials=Credentials(ACCESS,SECRET,'session',expiry)).presign('GET','file')

    def test_unsafe_endpoint_bucket_prefix_and_style_inputs_are_rejected(self):
        for endpoint in ('http://s3.4399.com','https://user:secret@s3.4399.com','https://s3.4399.com/path',
                         'https://s3.4399.com?secret=foo','https://localhost','https://169.254.169.254',1):
            with self.subTest(endpoint=endpoint), self.assertRaises(S3Error):
                Location('bucket-name','us-east-1',endpoint=endpoint)
        for bucket in ('BUCKET','192.168.1.1','bad..bucket','bucket--x-s3','bucket-s3alias','xn--bucket','aa'):
            with self.subTest(bucket=bucket), self.assertRaises(S3Error):
                Location(bucket,'us-east-1')
        for prefix in ('/top','top/','top//inner','../top','top/..','has space'):
            with self.subTest(prefix=prefix), self.assertRaises(S3Error):
                Location('bucket-name','us-east-1',prefix=prefix)
        for style in ('unknown',[],True):
            with self.subTest(style=style), self.assertRaises(S3Error):
                Location('bucket-name','us-east-1',style=style)

    def test_only_opaque_identity_keys_are_generated_under_exact_prefix(self):
        location = Location('bucket-name','us-east-1',prefix='frontiercloud/test')
        self.assertEqual(location.object_key('a'*32,'b'*64),'frontiercloud/test/'+'a'*32+'/objects/'+'b'*64)
        for member,object_id in (('a'*64,'b'*64),('a'*32,'../x'),(None,'b'*64)):
            with self.assertRaises(S3Error):
                location.object_key(member,object_id)

    def test_browser_grants_are_short_and_control_bodies_are_bounded(self):
        for seconds in (0,-1,True,301,3600,'300'):
            with self.subTest(seconds=seconds), self.assertRaises(S3Error):
                self.signer().presign('GET','file',seconds=seconds)
        for method in ('POST','DELETE','OPTIONS',[],True):
            with self.assertRaises(S3Error):
                self.signer().presign(method,'file')
        for body in (bytes(512*1024+1),'body',None):
            with self.assertRaises(S3Error):
                self.signer().control('POST',body=body)
        self.assertEqual(self.signer().presign('GET','file').expires_at,NOW+300)

    def test_headers_queries_and_credentials_cannot_inject_authority(self):
        for headers in ({'Host':'foreign.4399.com'},{'Authorization':'secret'},{'X-Amz-Date':'tomorrow'},
                        {'content-type':'a\r\nInjected:value'},{'Range':False},{'range':'bytes=0-1','Range':'bytes=2-3'}):
            with self.subTest(headers=headers), self.assertRaises(S3Error):
                self.signer().presign('GET','file',headers=headers)
        for query in ([('X-Amz-Signature','bad')],[('x-amz-credential','bad')],[('uploadId','bad\nvalue')],
                      [('uploadId',None)],{'key':'value'},[('uploadId','x'*17000)]):
            with self.assertRaises(S3Error):
                self.signer().presign('PUT','file',query=query)
        for arguments in ((ACCESS,'short'),(ACCESS,SECRET,'session'),('bad/credential',SECRET),
                          (ACCESS,SECRET,'session',True)):
            with self.assertRaises(S3Error):
                Credentials(*arguments)

    def test_canonical_headers_normalize_whitespace_and_query_sorts_encoded_pairs(self):
        value = self.signer().presign('PUT','file',headers={'Content-Type':'  application/octet-stream\t ',
            'Content-Length':'3','If-None-Match':'*'},query=[('z','has space'),('a','+'),('a','%')])
        self.assertEqual(value.headers['content-type'],'application/octet-stream')
        self.assertIn('a=%25&a=%2B&z=has%20space',value.url)
        self.assertIn('content-length%3Bcontent-type%3Bhost%3Bif-none-match',value.url)

    def test_invalid_clock_never_issues_a_grant(self):
        for clock in (0,-1,True,float('nan'),float('inf'),'now'):
            with self.assertRaises(S3Error):
                self.signer(clock=lambda:clock).presign('GET','file')
