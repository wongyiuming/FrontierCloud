"""Independent AWS SDK checks of the optional compatible-service fixture.

This is infrastructure evidence only, not FrontierCloud integration acceptance.
No presigned URL, access key, secret, upload ID or private endpoint is printed.
"""
import datetime
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid
from unittest.mock import patch


def run():
    import boto3
    import botocore
    from botocore.config import Config
    from botocore.exceptions import ClientError, SSLError
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import httpx
    import ssl
    from app.services.s3_signing import Credentials, Location, Signer
    from app.services.s3_control import Control, ControlError

    started = time.monotonic()
    root = Path(os.environ['S3_FIXTURE'])
    secrets = json.loads((root/'credentials.json').read_text())['identities']
    admin, member = (item['credentials'][0] for item in secrets[:2])
    bucket = 'frontiercloud-python-acceptance'
    endpoint = 'https://s3.4399.com:8333'
    ca = str(root/'ca.pem')
    config = Config(region_name='us-east-1', signature_version='s3v4',
        retries={'total_max_attempts': 1}, connect_timeout=5, read_timeout=15,
        s3={'addressing_style':'path'}, request_checksum_calculation='when_required',
        response_checksum_validation='when_required')
    def client(credentials=member, **kwargs):
        return boto3.client('s3', endpoint_url=kwargs.pop('endpoint_url',endpoint),
            aws_access_key_id=credentials['accessKey'], aws_secret_access_key=credentials['secretKey'],
            verify=kwargs.pop('verify',ca), config=config, **kwargs)
    owner = client(admin)
    ready_deadline=time.monotonic()+30
    while True:
        try:
            owner.list_buckets()
            break
        except SSLError:
            raise AssertionError('fixture_tls_verification_failed') from None
        except Exception as error:
            if time.monotonic()>=ready_deadline:
                raise AssertionError('fixture_readiness_failed_'+type(error).__name__) from None
            time.sleep(1)
    results = []
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        results.append(name)
        print(json.dumps({'passed':name}),file=sys.stderr,flush=True)
    try:
        owner.head_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response['ResponseMetadata']['HTTPStatusCode'] != 404:
            raise
        owner.create_bucket(Bucket=bucket)
    owner.put_bucket_cors(Bucket=bucket, CORSConfiguration={'CORSRules':[{
        'AllowedOrigins':['https://admin.4399.com'], 'AllowedMethods':['GET','PUT','HEAD'],
        'AllowedHeaders':['content-type','range','if-none-match','x-amz-checksum-sha256'],
        'ExposeHeaders':['ETag','Content-Range','Accept-Ranges'], 'MaxAgeSeconds':60}]})
    check('bucket_cors_readback', owner.get_bucket_cors(Bucket=bucket)['CORSRules'][0]['AllowedOrigins'] == ['https://admin.4399.com'])
    for name, kwargs in (('wrong_ca_rejected',dict(verify=str(root/'wrong-ca.pem'))),
                         ('wrong_san_rejected',dict(endpoint_url='https://wrong.4399.com:8333'))):
        bad = client(**kwargs)
        try:
            bad.head_bucket(Bucket=bucket)
        except SSLError:
            results.append(name)
            print(json.dumps({'passed':name}),file=sys.stderr,flush=True)
        else:
            raise AssertionError(name)
        finally:
            bad.close()
    sdk = client()
    if os.environ.get('S3_FIXTURE_PHASE') == 'restart':
        marker = json.loads((root/'restart-marker.json').read_text())
        # Bucket metadata readiness does not prove that restarted volume
        # servers have registered their data locations with the coordinator.
        # Wait for this exact previously committed object, never recreate it.
        deadline = time.monotonic()+30
        pending = []
        while True:
            try:
                value = sdk.get_object(Bucket=bucket,Key=marker['key'])
                break
            except ClientError as error:
                status = error.response['ResponseMetadata']['HTTPStatusCode']
                code = error.response['Error']['Code']
                if status not in (500,503) or code not in {'InternalError','ServiceUnavailable','SlowDown'} or time.monotonic() >= deadline:
                    raise
                pending.append(dict(status=status,code=code))
                print(json.dumps({'readiness_pending':code,'http_status':status}),file=sys.stderr,flush=True)
                time.sleep(1)
        try:
            check('restart_preserves_committed_object',hashlib.sha256(value['Body'].read(1024)).hexdigest() == marker['sha256'])
        finally:
            value['Body'].close()
        sdk.delete_object(Bucket=bucket,Key=marker['key'])
        sdk.close()
        owner.close()
        return dict(phase='restart',tests=results,count=len(results),tls_verification=True,
                    product_integration=False,aws_real_bucket=False,readiness_pending=pending,
                    object_not_recreated=True)
    prefix = 'acceptance/'+uuid.uuid4().hex+'/'
    context = ssl.create_default_context(cafile=ca)
    with httpx.Client(verify=context, trust_env=False, timeout=20, follow_redirects=False) as browser:
        data = b'FrontierCloud-private-original-'+bytes(range(256))*256
        instant = int(time.time())
        signer = Signer(Location(bucket,'us-east-1',endpoint=endpoint),
                        Credentials(member['accessKey'],member['secretKey']),clock=lambda:instant)
        from urllib.parse import parse_qs, urlsplit
        special_key = prefix+'签名 original%.bin'
        project_grant = signer.presign('PUT',special_key,seconds=60,headers={
            'content-type':'application/octet-stream','content-length':str(len(data)),'if-none-match':'*'})
        with patch('botocore.auth.get_current_datetime',return_value=datetime.datetime.fromtimestamp(instant,datetime.timezone.utc)):
            reference = sdk.generate_presigned_url('put_object',Params={'Bucket':bucket,'Key':special_key,
                'ContentType':'application/octet-stream','ContentLength':len(data),'IfNoneMatch':'*'},ExpiresIn=60)
        check('project_signature_matches_independent_aws_sdk',
            parse_qs(urlsplit(project_grant.url).query) == parse_qs(urlsplit(reference).query))
        response = browser.put(project_grant.url,content=data,headers=project_grant.headers)
        check('project_presigned_conditional_upload',response.status_code == 200)
        request = signer.control('HEAD',special_key)
        response = browser.head(request.url,headers=request.headers)
        check('project_signed_control_head',response.status_code == 200 and int(response.headers['content-length']) == len(data))
        request = signer.presign('GET',special_key,seconds=60)
        response = browser.get(request.url,headers={'Range':'bytes=3-9'})
        check('project_presigned_range_bytes',response.status_code == 206 and response.content == data[3:10])
        response = browser.put(project_grant.url,content=data,headers=project_grant.headers)
        check('project_upload_cannot_overwrite',response.status_code == 412)
        sdk.delete_object(Bucket=bucket,Key=special_key)
        async def metadata_round():
            control = Control(signer,context=context)
            control_key = prefix+'control.bin'
            step = 'head_bucket'
            try:
                await control.head_bucket()
                session_id,object_id = uuid.uuid4().hex,uuid.uuid4().hex*2
                step = 'create'
                upload_id = await control.create(control_key,session_id,object_id)
                sample = b'opaque-browser-test-bytes'
                part_sha = base64.b64encode(hashlib.sha256(sample).digest()).decode()
                try:
                    sdk.upload_part(Bucket=bucket,Key=control_key,UploadId=upload_id,PartNumber=1,
                        Body=sample,ChecksumSHA256=base64.b64encode(bytes(32)).decode())
                except ClientError as exc:
                    check('project_control_bad_part_digest_refused',exc.response['Error']['Code'] in {'BadDigest','InvalidDigest'})
                else:
                    raise AssertionError('project_control_bad_part_digest_refused')
                sdk.upload_part(Bucket=bucket,Key=control_key,UploadId=upload_id,PartNumber=1,
                    Body=sample,ChecksumSHA256=part_sha)
                step = 'parts'
                listing = await control.parts(control_key,upload_id)
                check('project_control_lists_exact_part_metadata',listing['complete'] and len(listing['parts']) == 1 and
                    listing['parts'][0]['bytes'] == len(sample) and listing['parts'][0]['checksum'] in (None,part_sha))
                step = 'complete'
                completed = await control.complete(control_key,upload_id,[
                    dict(number=1,etag=listing['parts'][0]['etag'],checksum=part_sha)])
                step = 'head'
                head = await control.head(control_key)
                composite = base64.b64encode(hashlib.sha256(hashlib.sha256(sample).digest()).digest()).decode()+'-1'
                check('project_control_completes_and_checks_metadata',head['bytes'] == len(sample) and
                    head['checksum'] == composite and completed['checksum'] == composite and
                    head['session_id'] == session_id and head['object_id'] == object_id)
                download = sdk.get_object(Bucket=bucket,Key=control_key)
                try:
                    check('project_control_result_has_original_bytes',download['Body'].read(1024) == sample)
                finally:
                    download['Body'].close()
                await control.delete(control_key,version_id=head['version_id'])
                abandoned = await control.create(control_key,uuid.uuid4().hex,uuid.uuid4().hex*2)
                await control.abort(control_key,abandoned)
                try:
                    await control.parts(control_key,abandoned)
                except ControlError as exc:
                    check('project_control_abort_is_observably_complete',exc.code == 's3_upload_missing')
                else:
                    raise AssertionError('project_control_abort_is_observably_complete')
            except ControlError as exc:
                print(json.dumps({'control_failure_code':exc.code,'provider_code':exc.provider_code,
                    'field':exc.field,'step':step,'status':exc.status,'uncertain':exc.uncertain}),file=sys.stderr,flush=True)
                raise
            finally:
                await control.close()
        asyncio.run(metadata_round())
        key = prefix+'original.bin'
        url = sdk.generate_presigned_url('put_object',Params={'Bucket':bucket,'Key':key},ExpiresIn=60)
        response = browser.put(url,content=data)
        check('presigned_direct_upload', response.status_code == 200)
        public = endpoint+'/'+bucket+'/'+key
        check('anonymous_private_read_denied',browser.get(public).status_code == 403)
        signed = sdk.generate_presigned_url('get_object',Params={'Bucket':bucket,'Key':key},ExpiresIn=60)
        response = browser.get(signed,headers={'Range':'bytes=3-9'})
        check('presigned_range_206',response.status_code == 206 and response.content == data[3:10]
            and response.headers['content-range'] == f'bytes 3-9/{len(data)}')
        before = sdk.head_object(Bucket=bucket,Key=key)['ETag']
        try:
            sdk.put_object(Bucket=bucket,Key=key,Body=b'replacement',IfNoneMatch='*')
        except ClientError as exc:
            check('conditional_overwrite_rejected',exc.response['ResponseMetadata']['HTTPStatusCode'] == 412)
        else:
            raise AssertionError('conditional_overwrite_rejected')
        check('existing_object_unchanged',sdk.head_object(Bucket=bucket,Key=key)['ETag'] == before)
        response = browser.options(public,headers={'Origin':'https://admin.4399.com',
            'Access-Control-Request-Method':'PUT','Access-Control-Request-Headers':'content-type'})
        check('browser_cors_preflight',response.status_code in (200,204) and
            response.headers.get('access-control-allow-origin') == 'https://admin.4399.com')
        response = browser.get(signed,headers={'Origin':'https://admin.4399.com','Range':'bytes=3-9'})
        exposed = {item.strip().lower() for item in response.headers.get('access-control-expose-headers','').split(',')}
        check('browser_range_headers_exposed',response.status_code == 206 and {'etag','content-range'} <= exposed)
        response = browser.options(public,headers={'Origin':'https://untrusted.4399.com','Access-Control-Request-Method':'PUT'})
        check('untrusted_origin_not_granted',response.headers.get('access-control-allow-origin') not in ('*','https://untrusted.4399.com'))
        check('tampered_signature_denied',browser.get(signed+'&unexpected=value').status_code == 403)
        previous = datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(seconds=120)
        with patch('botocore.auth.get_current_datetime',return_value=previous):
            expired = sdk.generate_presigned_url('get_object',Params={'Bucket':bucket,'Key':key},ExpiresIn=60)
        check('expired_presigned_read_denied',browser.get(expired).status_code == 403)
        cipher_key = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)
        ciphertext = AESGCM(cipher_key).encrypt(nonce,data,b'fixture-browser-boundary')
        encrypted_key = prefix+'ciphertext.bin'
        sdk.put_object(Bucket=bucket,Key=encrypted_key,Body=ciphertext,IfNoneMatch='*')
        loaded = sdk.get_object(Bucket=bucket,Key=encrypted_key)
        try:
            original = loaded['Body'].read(len(ciphertext)+1)
        finally:
            loaded['Body'].close()
        check('ciphertext_bytes_and_client_decryption',original == ciphertext and
            AESGCM(cipher_key).decrypt(nonce,original,b'fixture-browser-boundary') == data)
        multipart_key = prefix+'multipart.bin'
        upload_id = sdk.create_multipart_upload(Bucket=bucket,Key=multipart_key)['UploadId']
        parts, expected = [], hashlib.sha256()
        for number, size in ((1,5*1024*1024),(2,65539)):
            chunk = bytes([number])*size
            expected.update(chunk)
            url = sdk.generate_presigned_url('upload_part',Params={'Bucket':bucket,'Key':multipart_key,
                'UploadId':upload_id,'PartNumber':number},ExpiresIn=60)
            part = browser.put(url,content=chunk)
            check('multipart_part_'+str(number),part.status_code == 200 and bool(part.headers.get('etag')))
            parts.append({'PartNumber':number,'ETag':part.headers['etag']})
        sdk.complete_multipart_upload(Bucket=bucket,Key=multipart_key,UploadId=upload_id,
            MultipartUpload={'Parts':parts},IfNoneMatch='*')
        actual = hashlib.sha256()
        download = sdk.get_object(Bucket=bucket,Key=multipart_key)
        try:
            for chunk in download['Body'].iter_chunks(chunk_size=65536):
                actual.update(chunk)
        finally:
            download['Body'].close()
        check('multipart_complete_bytes',actual.hexdigest() == expected.hexdigest())
        abandoned_key = prefix+'abandoned.bin'
        abandoned = sdk.create_multipart_upload(Bucket=bucket,Key=abandoned_key)['UploadId']
        sdk.upload_part(Bucket=bucket,Key=abandoned_key,UploadId=abandoned,PartNumber=1,Body=b'partial')
        check('incomplete_upload_observable',len(sdk.list_parts(Bucket=bucket,Key=abandoned_key,UploadId=abandoned)['Parts']) == 1)
        sdk.abort_multipart_upload(Bucket=bucket,Key=abandoned_key,UploadId=abandoned)
        try:
            sdk.list_parts(Bucket=bucket,Key=abandoned_key,UploadId=abandoned)
        except ClientError as exc:
            check('aborted_upload_gone',exc.response['Error']['Code'] == 'NoSuchUpload')
        else:
            raise AssertionError('aborted_upload_gone')
        try:
            sdk.head_object(Bucket=bucket,Key=abandoned_key)
        except ClientError as exc:
            check('incomplete_never_published',exc.response['ResponseMetadata']['HTTPStatusCode'] == 404)
        else:
            raise AssertionError('incomplete_never_published')
        for candidate in (key,encrypted_key,multipart_key):
            sdk.delete_object(Bucket=bucket,Key=candidate)
        check('managed_fixture_cleanup',not sdk.list_objects_v2(Bucket=bucket,Prefix=prefix).get('Contents'))
        marker = os.urandom(64)
        marker_key = 'acceptance-restart/'+uuid.uuid4().hex
        sdk.put_object(Bucket=bucket,Key=marker_key,Body=marker,IfNoneMatch='*')
        (root/'restart-marker.json').write_text(json.dumps({'key':marker_key,'sha256':hashlib.sha256(marker).hexdigest()}))
    sdk.close()
    owner.close()
    return dict(format='frontiercloud-s3-compatible-infrastructure',version=1,
        product_integration=False,aws_real_bucket=False,browser_gui_acceptance=False,
        project_signer_primitive=True,
        project_control_primitive=True,
        fixture='SeaweedFS 4.48',tls_verification=True,host_ports_published=False,
        tests=results,count=len(results),boto3=boto3.__version__,botocore=botocore.__version__,
        elapsed_seconds=round(time.monotonic()-started,4))


if __name__ == '__main__':
    try:
        print(json.dumps(run(),sort_keys=True))
    except Exception as error:
        # SDK/HTTP exceptions can contain secret-bearing URLs. Preserve only a
        # test assertion's fixed name, never a transport exception's text.
        code = str(error) if isinstance(error,AssertionError) else type(error).__name__
        chain = []
        current = error
        while current is not None and len(chain) < 8:
            chain.append(dict(type=type(current).__name__,
                              verify_code=getattr(current,'verify_code',None)))
            current = current.__cause__ or current.__context__
        response = getattr(error,'response',{})
        provider_code = response.get('Error',{}).get('Code','') if isinstance(response,dict) else ''
        operation = getattr(error,'operation_name','')
        print(json.dumps({'failed':code,'causes':chain,'product_integration':False,
            'provider_code':provider_code if isinstance(provider_code,str) and re.fullmatch(r'[A-Za-z0-9_]{1,64}',provider_code) else None,
            'http_status':response.get('ResponseMetadata',{}).get('HTTPStatusCode') if isinstance(response,dict) else None,
            'operation':operation if isinstance(operation,str) and re.fullmatch(r'[A-Za-z]{1,64}',operation) else None}))
        raise SystemExit(1) from None
