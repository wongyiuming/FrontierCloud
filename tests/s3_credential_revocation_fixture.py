"""Confined host-only credential revocation for the disposable S3 fixture."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

IMAGE='chrislusf/seaweedfs@sha256:4e61d15fd35994cb1e43e1e553dff106794841fd9a99ade2fc8c8bfce4d7872d'
BUCKET='frontiercloud-python-acceptance'


def run(*arguments):
    result=subprocess.run(arguments,check=True,capture_output=True,text=True,timeout=30)
    return result.stdout.strip()


def read(path):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>65536:
        raise RuntimeError('unsafe_revocation_fixture_file')
    return json.loads(path.read_text())


def write(path,value,replace=False):
    data=json.dumps(value,sort_keys=True,indent=2).encode()
    flags=os.O_WRONLY|getattr(os,'O_NOFOLLOW',0)|(0 if replace else os.O_CREAT|os.O_EXCL)
    descriptor=os.open(path,flags,0o600)
    with os.fdopen(descriptor,'wb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
            raise RuntimeError('unsafe_revocation_fixture_file')
        # The file itself is a bind mount: preserve its inode, owner and mode.
        stream.write(data);stream.truncate();stream.flush();os.fsync(stream.fileno())


def revoke(directory,*,command=run,reader=read,writer=write,clock=time.time):
    directory=Path(directory).absolute()
    if directory!=directory.resolve() or directory.name!='s3-compatible-fixture':
        raise RuntimeError('invalid_revocation_fixture_directory')
    project='fc-python-s3-'+hashlib.sha256(str(directory).encode()).hexdigest()[:12]
    manifest=reader(directory/'manifest.json')
    if any(manifest.get(key)!=value for key,value in dict(format='frontiercloud-python-confined-s3-fixture',version=1,
        project=project,network=project,image=IMAGE,bucket=BUCKET,endpoint='https://s3.4399.com:8333',
        exposed_host_ports=[],private_data_retained=True,aws_real_bucket=False).items()):
        raise RuntimeError('invalid_revocation_fixture_manifest')
    compose=reader(directory/'compose.json')
    service=compose.get('services',{}).get('s3',{})
    if (compose.get('name')!=project or set(compose.get('services',{}))!={'s3'} or service.get('image')!=IMAGE or
        service.get('cpus')!=1 or service.get('mem_limit')!='512m' or
        compose.get('networks',{}).get('fixture',{}).get('name')!=project or
        compose['networks']['fixture'].get('internal') is not True or
        '-s3.config=/fixture/credentials.json' not in service.get('command',[])):
        raise RuntimeError('invalid_revocation_fixture_compose')
    container_id=command('docker','compose','-p',project,'-f',str(directory/'compose.json'),'ps','-q','s3')
    if not re.fullmatch('[a-f0-9]{64}',container_id):
        raise RuntimeError('invalid_revocation_container_identity')
    container=json.loads(command('docker','inspect',container_id))
    if len(container)!=1:raise RuntimeError('ambiguous_revocation_container')
    container=container[0]
    labels=container.get('Config',{}).get('Labels',{})
    expected_mounts={'/data':(str(directory/'data'),True),**{
        '/fixture/'+name:(str(directory/name),False) for name in ('credentials.json','server.pem','server-key.pem')}}
    mounts={item['Destination']:(item.get('Source'),item.get('RW')) for item in container.get('Mounts',[])}
    host=container.get('HostConfig',{});state=container.get('State',{})
    if (container.get('Id')!=container_id or container.get('Config',{}).get('Image')!=IMAGE or
        any(labels.get(key)!=value for key,value in {'com.docker.compose.project':project,'com.docker.compose.service':'s3',
            'frontiercloud.acceptance':'python-s3-compatible'}.items()) or mounts!=expected_mounts or
        host.get('Privileged') or host.get('PublishAllPorts') or host.get('PortBindings') or
        not state.get('Running') or type(state.get('Pid')) is not int or state['Pid']<=0 or
        set(container.get('NetworkSettings',{}).get('Networks',{}))!={project} or
        any(container.get('NetworkSettings',{}).get('Ports',{}).values())):
        raise RuntimeError('unconfined_revocation_container')
    image=json.loads(command('docker','image','inspect',IMAGE))
    network=json.loads(command('docker','network','inspect',project))
    if (len(image)!=1 or image[0].get('Id')!=container.get('Image') or IMAGE not in image[0].get('RepoDigests',[]) or
        len(network)!=1 or network[0].get('Name')!=project or network[0].get('Internal') is not True or
        network[0].get('Labels',{}).get('com.docker.compose.project')!=project or
        set(network[0].get('Containers',{}))!={container_id}):
        raise RuntimeError('unconfined_revocation_provider')
    credentials=reader(directory/'credentials.json')
    identities=credentials.get('identities',[])
    pending=reader(directory/'transactions-credential-revocation'/'revocation.json')
    if (len(identities)!=2 or identities[0].get('name')!='fixture-admin' or identities[1].get('name')!='frontiercloud-member' or
        len(identities[0].get('credentials',[]))!=1 or len(identities[1].get('credentials',[]))!=2 or
        identities[1].get('actions')!=['Read:'+BUCKET,'Write:'+BUCKET,'List:'+BUCKET] or
        pending.get('format')!='frontiercloud-s3-credential-revocation' or pending.get('version')!=1 or
        pending.get('phase')!='prepared' or type(pending.get('grant_until')) is not int or pending['grant_until']<=clock()+60):
        raise RuntimeError('invalid_revocation_prepared_state')
    revoked=identities[1]['credentials'][1]
    digest=hashlib.sha256(revoked['accessKey'].encode()).hexdigest()
    if pending.get('revoked_access_key_sha256')!=digest or revoked==identities[1]['credentials'][0]:
        raise RuntimeError('revocation_key_not_owned_by_prepared_case')
    writer(directory/'credential-revocation-backup.json',credentials)
    changed=json.loads(json.dumps(credentials));changed['identities'][1]['credentials'].pop(1)
    writer(directory/'credentials.json',changed,True)
    command('docker','kill','--signal','HUP',container_id)
    after=json.loads(command('docker','inspect',container_id))
    if (len(after)!=1 or after[0].get('Id')!=container_id or not after[0].get('State',{}).get('Running') or
        after[0]['State'].get('Pid')!=state['Pid'] or after[0].get('Image')!=container.get('Image') or
        after[0].get('RestartCount')!=container.get('RestartCount')):
        raise RuntimeError('revocation_restarted_or_replaced_provider')
    receipt=dict(passed=True,format='frontiercloud-confined-s3-credential-revocation',version=1,
        container_id=container_id,pid_before=state['Pid'],pid_after=after[0]['State']['Pid'],
        image=IMAGE,revoked_access_key_sha256=digest,retained_credentials_unchanged=True,signal='HUP',provider_restarted=False)
    writer(directory/'credential-revocation-provider.json',receipt)
    return receipt


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--directory',type=Path,required=True)
    arguments=parser.parse_args()
    if not hasattr(os,'geteuid') or os.geteuid()!=0:raise RuntimeError('revocation_fixture_requires_test_host_root')
    print(json.dumps(revoke(arguments.directory)))
