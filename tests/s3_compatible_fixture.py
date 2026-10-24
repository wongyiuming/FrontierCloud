"""Create confined, reproducible S3 test infrastructure; never a product service."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets

from cryptography.hazmat.primitives import serialization

from tests.certificate_fixtures import TestCA
from tests.federation_stack import command


IMAGE='chrislusf/seaweedfs@sha256:4e61d15fd35994cb1e43e1e553dff106794841fd9a99ade2fc8c8bfce4d7872d'
BUCKET='frontiercloud-python-acceptance'


def prepare(directory,web_image):
    directory=directory.resolve()
    if directory.exists():
        raise RuntimeError('s3_fixture_directory_must_be_new')
    directory.mkdir(mode=0o700,parents=True)
    project='fc-python-s3-'+hashlib.sha256(str(directory).encode()).hexdigest()[:12]
    data=directory/'data';data.mkdir(mode=0o700)
    os.chown(data,1000,1000)
    ca,wrong=TestCA(),TestCA()
    leaf,key=ca.pair('s3.4399.com')
    for name,value in (('ca.pem',ca.root.public_bytes(serialization.Encoding.PEM)),
                       ('wrong-ca.pem',wrong.root.public_bytes(serialization.Encoding.PEM)),
                       ('server.pem',leaf),('server-key.pem',key)):
        (directory/name).write_bytes(value)
        if name!='server-key.pem':(directory/name).chmod(0o644)
    os.chown(directory/'server-key.pem',0,1000)
    (directory/'server-key.pem').chmod(0o640)
    identities=[]
    for name,actions in [('fixture-admin',['Admin','Read','Write','List','Tagging']),
                         ('frontiercloud-member',['Read:'+BUCKET,'Write:'+BUCKET,'List:'+BUCKET])]:
        identities.append(dict(name=name,actions=actions,
            credentials=[dict(accessKey=secrets.token_hex(12),secretKey=secrets.token_urlsafe(32))]))
    # Index zero remains the normal test key; only this second key is revoked.
    identities[1]['credentials'].append(dict(accessKey=secrets.token_hex(12),secretKey=secrets.token_urlsafe(32)))
    credentials=directory/'credentials.json'
    credentials.write_text(json.dumps(dict(identities=identities)))
    os.chown(credentials,0,1000);credentials.chmod(0o640)
    volumes=[dict(type='bind',source=str(directory/name),target='/fixture/'+name,read_only=True)
             for name in ('credentials.json','server.pem','server-key.pem')]
    network_ids=command('docker','network','ls','-q').splitlines()
    networks=json.loads(command('docker','network','inspect',*network_ids)) if network_ids else []
    occupied=[ipaddress.ip_network(row['Subnet']) for item in networks
        for row in ((item.get('IPAM') or {}).get('Config') or []) if row.get('Subnet')]
    subnet=next((str(value) for index in range(250,100,-1) for value in [ipaddress.ip_network(f'10.247.{index}.0/24')]
        if not any(value.overlaps(other) for other in occupied if other.version==4)),None)
    if subnet is None:raise RuntimeError('no_isolated_s3_provider_subnet')
    compose=dict(name=project,services=dict(s3=dict(image=IMAGE,cpus=1,mem_limit='512m',restart='no',
        labels={'frontiercloud.acceptance':'python-s3-compatible'},
        command=['server','-dir=/data','-ip=s3.4399.com','-ip.bind=0.0.0.0','-s3',
            '-master.volumeSizeLimitMB=64','-volume.max=12','-master.telemetry=false',
            '-s3.cert.file=/fixture/server.pem','-s3.key.file=/fixture/server-key.pem',
            '-s3.config=/fixture/credentials.json','-s3.autoCreateBucket=false','-s3.allowDeleteBucketNotEmpty=false',
            '-s3.allowedOrigins=https://admin.4399.com','-s3.port.iceberg=0','-s3.port.lance=0',
            '-s3.concurrentFileUploadLimit=2','-s3.readerCacheSizeMB=32'],
        volumes=[dict(type='bind',source=str(data),target='/data'),*volumes],
        networks=dict(fixture=dict(aliases=['s3.4399.com','wrong.4399.com'])))),
        networks=dict(fixture=dict(name=project,internal=True,ipam=dict(config=[dict(subnet=subnet)]))))
    (directory/'compose.json').write_text(json.dumps(compose,indent=2))
    build_context=directory/'client-build';build_context.mkdir(mode=0o700)
    build=build_context/'Dockerfile'
    build.write_text('FROM '+web_image+'\nUSER 0\n'
        'RUN apt-get update && apt-get install -y --no-install-recommends git && '
        'rm -rf /var/lib/apt/lists/* && git config --system --add safe.directory /workspace\n'
        'RUN python -m pip install --no-cache-dir boto3==1.43.114\n')
    client=project+':client'
    command('docker','build','-t',client,'-f',str(build),str(build_context))
    manifest=dict(format='frontiercloud-python-confined-s3-fixture',version=1,project=project,network=project,
        image=IMAGE,client_image=client,bucket=BUCKET,endpoint='https://s3.4399.com:8333',
        exposed_host_ports=[],aws_real_bucket=False,private_data_retained=True)
    (directory/'manifest.json').write_text(json.dumps(manifest,indent=2))
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--web-image',default='frontiercloud-acceptance-web')
    args=parser.parse_args()
    print(json.dumps(prepare(args.directory,args.web_image)))
