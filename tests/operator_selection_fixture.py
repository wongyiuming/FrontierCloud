"""Real source exec after an explicit simulated old-entry capability limit."""
import argparse
import json
import os
from pathlib import Path
import sys
import shutil
from unittest.mock import patch

sys.dont_write_bytecode=True
if __package__ in {None,''}:sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_entry as entry
from scripts import operator_registry as registry
from scripts import operator_source as sources


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase',choices=['old','retained','target'],required=True)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--stage',type=Path,required=True)
    parser.add_argument('--request',type=Path)
    parser.add_argument('--next-stage',type=Path)
    options=parser.parse_args()
    if sys.platform!='linux' or os.geteuid()!=0 or os.environ.get('FC_OPERATOR_APPLICATION_RUNTIME')!='1':
        raise RuntimeError('explicit root-only isolated application acceptance is required')
    root=backup.absolute(os.environ['FC_OPERATOR_APPLICATION_EVIDENCE'])
    directory=backup.absolute(options.directory);stage=backup.absolute(options.stage)
    if root not in directory.parents or root not in stage.parents:
        raise RuntimeError('selection fixture must stay in the isolated application evidence')
    evidence=directory.parent
    next_stage=backup.absolute(options.next_stage) if options.next_stage else None
    if next_stage and root not in next_stage.parents:raise RuntimeError('next fixture source must remain isolated')
    reference=registry.operation(directory)
    receipt=registry.private_json(Path(reference['prepared_directory'])/'prepared.json')
    if receipt.get('acceptance_only') is not True or 'media-init' not in receipt['added_services']:
        raise RuntimeError('fixture requires the real local-source upgrade which added its missing init service')
    allowed=[sources.verify_stage(Path(receipt['stage_directory']))]
    if next_stage:allowed.append(sources.verify_stage(next_stage))
    def fixture_stage(candidate):
        entry._private_directory(candidate)
        registry.private_json(candidate/'source-receipt.json')
        checked=sources.verify_stage(candidate)
        if (root not in candidate.parents or checked not in allowed or checked['provenance'].get('public_provenance_verified') is not False
                or backup.bounded_json(candidate/'source/protocol/python-operator.json')!=entry.PROTOCOL):
            raise RuntimeError('selection fixture cannot claim a public release or substitute its source')
        return checked
    original_docker=mapping.ComposeDocker
    def docker(*arguments,**values):
        result=original_docker(*arguments,**values)
        result.audit_file=evidence/('selection-'+options.phase+'-docker-inputs.jsonl')
        result.environment={**result.environment,'PYTHONDONTWRITEBYTECODE':'1','FC_OPERATOR_APPLICATION_RUNTIME':'1',
            'FC_OPERATOR_APPLICATION_EVIDENCE':str(root)}
        return result
    if options.phase=='old' or options.phase=='retained' and next_stage:
        original_exec=os.execve
        def execute(program,arguments,environment):
            selected=Path(arguments[-1]);value=registry.private_json(selected)
            expected='select_upgrade' if options.phase=='old' else 'new_upgrade'
            if value['intent']!=expected:raise RuntimeError('source exec did not retain its exact selection intent')
            target=Path(value['stage_directory']);phase='retained' if options.phase=='old' else 'target'
            actual=[program,'-I','-B',str(target/'source/tests/operator_selection_fixture.py'),
                '--phase',phase,'--directory',str(directory),'--stage',str(target),'--request',str(selected)]
            if next_stage:actual+=['--next-stage',str(next_stage)]
            step='selection-real-exec-'+('next' if next_stage else 'current')+'-'+options.phase+'-input.json'
            backup.private_write(evidence/step,backup.json_bytes({'fixture_only':True,
                'simulated_old_discovery_rejects':'media-init','argv':actual,'working_directory':str(directory),
                'inherited_lock_fd':value['lock_fd'],'intent':value['intent']}))
            original_exec(program,actual,environment)
    if options.phase=='old':
        with patch.object(entry,'_stage',side_effect=fixture_stage),patch.object(mapping,'ComposeDocker',side_effect=docker),\
                patch.object(mapping.Deployment,'discover',side_effect=AssertionError('old entry cannot recognize newly added media-init')),\
                patch.object(entry.releases,'select_latest',side_effect=AssertionError('old entry must hand off before latest-source lookup')),\
                patch.object(os,'execve',side_effect=execute):
            entry.start(directory,host_root=evidence/'selection-host')
        raise RuntimeError('expected real source exec')
    if Path(__file__).resolve().parents[1]!=stage/'source':
        raise RuntimeError('retained selector must run from the exact staged source')
    request=backup.absolute(options.request)
    value=registry.private_json(request)
    if value['intent']!=('new_upgrade' if options.phase=='target' else 'select_upgrade') or value['command']!='upgrade':
        raise RuntimeError('fixture did not retain its upgrade-selection intent')
    if options.phase=='target':
        from scripts import operator_prepare as preparation
        from scripts.operator_upgrade import Upgrade
        from tests import operator_application_native as applications
        application=applications.ApplicationTests('runTest');application.ca=directory.parent/'test-ca'
        application.docker=original_docker(directory,[],receipt['project'],[]);application.clean_images={}
        original_prepare=preparation.prepare;original_begin=Upgrade.begin;original_advance=Upgrade.advance
        def prepare(*arguments,**values):return original_prepare(*arguments,**values,acceptance=True,execute=application.candidate_build)
        def begin(*arguments,**values):return original_begin(*arguments,**values,acceptance=True)
        def advance(*arguments,**values):return original_advance(*arguments,**values,acceptance=True)
        with patch.object(entry,'_stage',side_effect=fixture_stage),patch.object(mapping,'ComposeDocker',side_effect=docker),\
                patch.object(preparation,'prepare',side_effect=prepare),patch.object(Upgrade,'begin',side_effect=begin),\
                patch.object(Upgrade,'advance',side_effect=advance):result=entry.continue_upgrade(request)
    elif next_stage:
        next_proof=sources.verify_stage(next_stage)['provenance']
        def stage_source(destination,**values):
            if values!={'_selected':next_proof}:raise RuntimeError('fixture selector changed the next source')
            shutil.copytree(next_stage,destination)
            return sources.verify_stage(destination)
        with patch.object(entry,'_stage',side_effect=fixture_stage),patch.object(mapping,'ComposeDocker',side_effect=docker),\
                patch.object(entry.releases,'select_latest',return_value=next_proof),patch.object(sources,'stage',side_effect=stage_source),\
                patch.object(os,'execve',side_effect=execute):result=entry.continue_upgrade(request)
    else:
        with patch.object(entry,'_stage',side_effect=fixture_stage),patch.object(mapping,'ComposeDocker',side_effect=docker),\
                patch.object(entry.releases,'select_latest',return_value=receipt['provenance']):result=entry.continue_upgrade(request)
    print(json.dumps({'fixture_only':True,'public_release_proved':False,'old_discovery_limit_simulated':True,
        'actual_added_service':'media-init','executed_source':str(stage/'source'),'result':result}),flush=True)


if __name__=='__main__':main()
