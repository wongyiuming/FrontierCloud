"""Bounded read-only first-install identity and endpoint helpers."""
from __future__ import annotations

import copy
import json
import re
import uuid

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_prepare as preparation
from scripts.operator_upgrade import HELPER_LABEL,_join_helper


def run(package,command, *, baseline=None,baseline_sha=None,node_id=None,execute=preparation._command):
    if command not in {'identity-original','probe-open'}:
        mapping.fail('install_probe_invalid','installation helper is read-only identity or open-endpoint verification')
    attempt=package.directory/('install-probe-'+uuid.uuid4().hex);attempt.mkdir(mode=0o700)
    web=copy.deepcopy(package.config['services']['web'])
    for mount in web.get('volumes',[]):mount['read_only']=True
    for key in ('ports','container_name','depends_on','restart','healthcheck'):web.pop(key,None)
    web['user']='10001:10001';web['read_only']=True
    used={row['source'] for row in web.get('volumes',[]) if row['type']=='volume'}
    config={'name':package.receipt['project'],'services':{'web':web},
        'volumes':{key:copy.deepcopy(package.config['volumes'][key]) for key in used}}
    if package.config.get('networks'):config['networks']=copy.deepcopy(package.config['networks'])
    if baseline:
        web.setdefault('volumes',[]).append({'type':'bind','source':str(baseline),'target':'/run/frontiercloud-install-baseline.json','read_only':True})
    preparation._store(attempt/'helper-compose.json',backup.compose_values(config,encode=True))
    docker=mapping.ComposeDocker(package.docker.directory,[attempt/'helper-compose.json'],package.receipt['project'],[])
    docker.audit_file=attempt/'docker-inputs.jsonl'
    token=uuid.uuid4().hex
    owner={'version':1,'operation':package.receipt['id'],'project':package.receipt['project'],'token':token,
        'name':'fc-op-helper-'+token,'image':web['image']}
    preparation._store(attempt/'helper-owner.json',owner)
    arguments=docker.base+['run','--rm','--name',owner['name'],'--label',HELPER_LABEL+'='+token,
        '--no-deps','-T','--pull','never','--entrypoint','python','web','-m','app.services.offline_upgrade',command]
    if baseline:arguments+=['--node-id',node_id,'--baseline','/run/frontiercloud-install-baseline.json','--baseline-sha256',baseline_sha]
    caught=None
    try:execute(docker,arguments,attempt,'install-'+command,timeout=120)
    except mapping.DeploymentError as error:caught=error
    finally:_join_helper(docker,owner)
    log=attempt/('install-'+command+'.log')
    if not log.exists() or log.stat().st_size>16*1024*1024:
        if caught:raise caught
        mapping.fail('install_probe_output_invalid','installation helper output is absent or too large')
    results=[]
    for line in log.read_text(encoding='utf-8').splitlines():
        try:value=json.loads(line)
        except ValueError:continue
        if isinstance(value,dict) and (value.get('version')==1 or set(value)=={'error','detail'}):results.append(value)
    if len(results)!=1:
        if caught:raise caught
        mapping.fail('install_probe_output_invalid','installation helper returned ambiguous output')
    result=results[0]
    if caught:
        code=result.get('error')
        if isinstance(code,str) and re.fullmatch('[a-z][a-z0-9_]{0,79}',code):
            mapping.fail(code,'read-only installation helper refused; private diagnostics retained')
        raise caught
    if result.get('error'):mapping.fail('install_probe_output_invalid','helper failure did not have a failed exit status')
    return result
