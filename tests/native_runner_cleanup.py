"""Exercise actual native-runner cleanup functions with disposable Docker jobs."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import uuid


ROOT=Path(__file__).resolve().parents[1]


def interrupt_fixture(_signal,_frame):
    """Let GNU timeout enter Python's owned-fixture cleanup path."""
    raise InterruptedError('Native browser fixture interrupted')


def finalize_cleanup(report, cleanup, destination, *, business_failed):
    """Make owned cleanup part of success while preserving a business error."""
    report['cleanup']=cleanup
    cleanup_failed=any(not item['stopped'] for item in cleanup)
    if cleanup_failed or business_failed:
        report['passed']=False
    try:
        destination.write_text(json.dumps(report,sort_keys=True))
    except Exception:
        if not business_failed:raise
    if cleanup_failed and not business_failed:
        raise RuntimeError('Exact native fixtures did not stop')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools-image',required=True)
    parser.add_argument('--output',required=True,type=Path)
    options=parser.parse_args();output=options.output.resolve()
    output.mkdir(mode=0o700);events=[];created=[];networks=[]
    def command(argv,*,expected=0,timeout=40):
        started=time.time();result=subprocess.run(argv,capture_output=True,text=True,timeout=timeout)
        events.append(dict(argv=argv,started_at=started,exit_code=result.returncode,
            stdout_sha256=hashlib.sha256(result.stdout.encode()).hexdigest(),stderr_sha256=hashlib.sha256(result.stderr.encode()).hexdigest()))
        if result.returncode!=expected:raise AssertionError((argv,result.returncode,result.stderr[-800:]))
        return result.stdout.strip()
    def container(name):
        identifier=command(['docker','run','-d','--name',name,'--network','none','--cpus','1','--memory','64m',
            options.tools_image,'python','-c','import time;time.sleep(3600)'])
        created.append(name);return identifier
    token=uuid.uuid4().hex[:12];sentinel='fc-runner-sentinel-'+token;sentinel_id=container(sentinel)
    report=dict(format='frontiercloud-python-native-runner-cleanup',actual_product_workflow=False,actual_docker=True,
        cases=[],inputs=events,passed=False)
    try:
        for kind,filename in [('final','test-python-final.sh'),('operator','test-python-operator.sh')]:
            source=(ROOT/'scripts'/filename).read_text()
            function='cleanup() {'+source.split('cleanup() {',1)[1].split('\n}',1)[0]+'\n}\n'
            directory=output/kind;directory.mkdir(mode=0o700)
            cleanup=directory/'cleanup.sh';cleanup.write_text(function)
            unit='fc-runner-unit-'+kind+'-'+token;container(unit)
            driver=directory/'driver.sh'
            if kind=='final':
                success_unit='fc-runner-success-hour-'+token;container(success_unit)
                context=Path(tempfile.mkdtemp(prefix='fc-python-unit-tools.',dir='/tmp'))
                assert context.resolve()==context and context.parent==Path('/tmp')
                (context/'owned-marker').write_text(token)
                prelude='unit_names=("'+unit+'" "'+success_unit+'")\ntools_context='+shlex.quote(str(context))+'\ngui_pid=""\n'
                prelude+='(docker wait "'+unit+'" >/dev/null) &\nsqlite_hour_pid=$!\n'
                prelude+='(docker wait "'+success_unit+'" >/dev/null) &\nsuccess_hour_pid=$!\n'
                absent=[unit,success_unit]
            else:
                database='fc-runner-db-'+token;container(database)
                network='fc-runner-net-'+token
                ids=command(['docker','network','ls','-q']).splitlines()
                existing=json.loads(command(['docker','network','inspect',*ids])) if ids else []
                occupied=[ipaddress.ip_network(row['Subnet']) for item in existing
                    for row in ((item.get('IPAM') or {}).get('Config') or []) if row.get('Subnet')]
                subnet=next((str(value) for index in range(250,100,-1) for value in [ipaddress.ip_network(f'10.241.{index}.0/24')]
                    if not any(value.overlaps(other) for other in occupied if other.version==4)),None)
                if subnet is None:raise RuntimeError('No isolated cleanup fixture subnet')
                command(['docker','network','create','--internal','--subnet',subnet,network]);networks.append(network)
                prelude='client='+unit+'\ndatabase='+database+'\nnetwork='+network+'\n'
                absent=[unit,database]
            pid_record=('printf "%s\\n" "$sqlite_hour_pid" "$success_hour_pid" > '+shlex.quote(str(directory/'job.pid'))+'\n') if kind=='final' else ''
            driver.write_text('set -euo pipefail\nsource '+shlex.quote(str(cleanup))+'\n'+prelude+
                pid_record+
                'trap cleanup EXIT\nfor attempt in $(seq 1 50); do if test -n "$(jobs -pr)"; then break; fi; sleep .02; done\nexit 17\n')
            command(['bash',str(driver)],expected=17,timeout=40)
            if kind=='final':
                for owned_pid in (directory/'job.pid').read_text().splitlines():assert not (Path('/proc')/owned_pid).exists()
            for name in absent:
                assert not command(['docker','ps','-aq','--filter','name=^/'+name+'$'])
            if kind=='final':assert not context.exists()
            else:
                value=subprocess.run(['docker','network','inspect',network],capture_output=True)
                assert value.returncode!=0
            sentinel_info=json.loads(command(['docker','inspect',sentinel]))[0]
            assert sentinel_info['Id']==sentinel_id and sentinel_info['State']['Running']
            report['cases'].append(dict(kind=kind,original_exit_code=17,background_job_started=kind=='final',
                background_jobs_started=2 if kind=='final' else 0,
                owned_jobs_joined=kind=='final',owned_containers_removed=True,
                unrelated_sentinel_preserved=True,source_cleanup_sha256=hashlib.sha256(function.encode()).hexdigest()))
        report['passed']=True
    finally:
        for name in reversed(created):
            subprocess.run(['docker','rm','-fv',name],capture_output=True,timeout=30)
        for name in networks:
            subprocess.run(['docker','network','rm',name],capture_output=True,timeout=30)
        (output/'result.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(dict(passed=True,cases=len(report['cases']),actual_product_workflow=False)))


if __name__=='__main__':main()
