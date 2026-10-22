"""Real install-source exec with explicit isolated local-source/test-CA admission."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

sys.dont_write_bytecode=True
if __package__ in {None,''}:sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from scripts import frontiercloud_operator as cli
from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_entry as entry
from scripts import operator_install_package as packages
from scripts import operator_install_start as startup
from scripts import operator_prepare as preparation
from scripts import operator_registry as registry
from scripts import operator_source as sources
from tests import operator_application_native as applications


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--stage',type=Path,required=True)
    parser.add_argument('--command',choices=['install','upgrade'],default='install')
    parser.add_argument('--request',type=Path)
    parser.add_argument('--storage',action='store_true')
    parser.add_argument('--lose',choices=['none','start','publication'],default='none')
    options=parser.parse_args()
    if sys.platform!='linux' or os.geteuid()!=0 or os.environ.get('FC_OPERATOR_INSTALL_RUNTIME')!='1':
        raise RuntimeError('explicit root-only isolated installation acceptance is required')
    root=backup.absolute(os.environ['FC_OPERATOR_INSTALL_EVIDENCE'])
    directory=backup.absolute(options.directory);template=backup.absolute(options.stage);evidence=directory.parent
    if root not in directory.parents or root not in template.parents:
        raise RuntimeError('installation fixture must remain inside isolated evidence')
    checked=sources.verify_stage(template)
    if checked['provenance'].get('public_provenance_verified') is not False:
        raise RuntimeError('fixture cannot consume or pretend to prove a published release')
    request=backup.absolute(options.request) if options.request else None
    if request and (root not in request.parents or Path(__file__).resolve().parents[1]!=Path(registry.private_json(request)['stage_directory'])/'source'):
        raise RuntimeError('target fixture must execute from its exact staged source')
    original_docker=mapping.ComposeDocker;original_exec=os.execve;original_start=entry.start
    original_prepare=packages.prepare;original_begin=startup.Installation.begin;original_load=startup.Installation.load
    original_install=startup.Installation.start;original_publish=registry.publish
    application=applications.ApplicationTests('runTest');application.ca=evidence/'test-ca';application.clean_images={}
    application.docker=original_docker(directory,[],'install-fixture',[])
    def docker(*args,**values):
        value=original_docker(*args,**values)
        value.audit_file=evidence/('entry-'+('target' if request else 'bootstrap')+'-docker-inputs.jsonl')
        value.environment={**value.environment,'PYTHONDONTWRITEBYTECODE':'1','FC_OPERATOR_INSTALL_RUNTIME':'1',
            'FC_OPERATOR_INSTALL_EVIDENCE':str(root),'FC_OPERATOR_DISCOVERY_IMAGE':os.environ.get('FC_OPERATOR_DISCOVERY_IMAGE','frontiercloud-python-archive:db-test-tools')}
        return value
    def fixture_stage(stage):
        entry._private_directory(stage);registry.private_json(stage/'source-receipt.json')
        value=sources.verify_stage(stage)
        if root not in stage.parents or value!=checked:
            raise RuntimeError('fixture source or provenance changed')
        if backup.bounded_json(stage/'source/protocol/python-operator.json')!=entry.PROTOCOL:
            raise RuntimeError('fixture requires the real operator protocol')
        return value
    def latest():
        if (options.command=='install' and (directory/packages.INSTALLATION).exists()) or not request and options.command=='upgrade':
            raise RuntimeError('bootstrap must use retained install source before selection')
        return checked['provenance']
    def stage_source(destination,**values):
        if values!={'_selected':checked['provenance']}:raise RuntimeError('installation source selection changed')
        shutil.copytree(template,destination)
        return sources.verify_stage(destination)
    def execute(program,arguments,environment):
        path=Path(arguments[-1]);value=registry.private_json(path);selected=Path(value['stage_directory'])
        actual=[program,'-I','-B',str(selected/'source/tests/operator_install_handoff_fixture.py'),
            '--directory',str(directory),'--stage',str(template),'--request',str(path),'--command',options.command,'--lose',options.lose]
        backup.private_write(path.parent/'fixture-exec-input.json',backup.json_bytes({'fixture_only':True,'argv':actual,
            'product_argv':arguments,'working_directory':str(directory),'inherited_lock_fd':value['lock_fd'],
            'intent':value['intent'],'actual_os_exec':True,'public_release_proved':False}))
        original_exec(program,actual,environment)
    def build(docker,arguments,output,step,**values):
        preparation._command(docker,arguments,output,step,**values)
        if step=='install-image-build':
            config=docker.config()
            for kind in ('web','nginx') if 'nginx' in config['services'] else ('web',):
                application.trust(config['services'][kind]['image'],kind,output,'install-'+kind)
    def prepare(*args,**values):return original_prepare(*args,**values,acceptance=True,overrides=[directory/'fixture.yaml'],execute=build)
    def begin(*args,**values):return original_begin(*args,**values,acceptance=True)
    def load(*args,**values):return original_load(*args,**values,acceptance=True)
    def start_command(docker,arguments,output,step,**values):
        if step=='install-start':
            reference,receipt,state=entry.installations.recorded(directory)
            if state['phase']!='starting' or state['business_may_have_written'] is not True:raise RuntimeError('missing persisted startup boundary')
            name=next(name for name,key in receipt['fresh_resources']['volumes'].items() if key=='certificate_state')
            program="""import pathlib,shutil
root=pathlib.Path('/private/operator-test-ca');root.mkdir(mode=0o700,exist_ok=True)
for name in ('private.pem','root.crt'):
    source=pathlib.Path('/fixture')/name;target=root/name
    if target.exists():assert target.read_bytes()==source.read_bytes()
    else:shutil.copyfile(source,target);target.chmod(0o600)
"""
            attempt=output/'fixture-ca-seed';attempt.mkdir(mode=0o700)
            preparation._command(docker,['docker','run','--rm','--network','none','--user','0','--cpus','1','--memory','128m',
                '--mount','type=volume,source='+name+',target=/private',
                '--mount','type=bind,source='+str(evidence/'private-test-ca')+',target=/fixture,readonly',
                os.environ.get('FC_OPERATOR_DISCOVERY_IMAGE','frontiercloud-python-archive:db-test-tools'),
                'python','-c',program],attempt,'test-ca-seed')
        preparation._command(docker,arguments,output,step,**values)
        if step=='install-start' and options.lose=='start':
            print(json.dumps({'fixture_only':True,'fault':'host_exit_after_actual_compose_up','exit_code':35}),flush=True)
            os._exit(35)
    def install(operation,**values):return original_install(operation,**values,execute=start_command)
    def publish(*args,**values):
        result=original_publish(*args,**values)
        if options.lose=='publication':
            print(json.dumps({'fixture_only':True,'fault':'host_exit_after_actual_current_publication','exit_code':36}),flush=True)
            os._exit(36)
        return result
    def start(*args,**values):return original_start(*args,**values,host_root=evidence/'entry-host')
    with ExitStack() as stack:
        for target,name,replacement in ((entry,'_stage',fixture_stage),(mapping,'ComposeDocker',docker),(entry.releases,'select_latest',latest),
                (sources,'stage',stage_source),(os,'execve',execute),(entry,'start',start),(packages,'prepare',prepare),
                (startup.Installation,'begin',begin),(startup.Installation,'load',load),(startup.Installation,'start',install),
                (registry,'publish',publish)):
            stack.enter_context(patch.object(target,name,side_effect=replacement) if name not in {'start'} or target is not startup.Installation
                else patch.object(target,name,new=replacement))
        arguments=['--operator-handoff',str(request)] if request else [options.command,'--project-directory',str(directory)]+(['--storage'] if options.storage else [])
        preparation._store(evidence/('cli-call-'+os.urandom(8).hex()+'-input.json'),{'fixture_only':True,
            'real_cli_main_arguments':arguments,'entry_source':str(Path(cli.__file__).resolve()),
            'phase':'target' if request else 'bootstrap','command':options.command,'public_release_proved':False})
        code=cli.main(arguments)
    return code


if __name__=='__main__':raise SystemExit(main())
