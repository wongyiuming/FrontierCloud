"""Isolated staged-entry acceptance, with explicit unpublished test-CA source."""
import argparse
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.dont_write_bytecode=True
if __package__ in {None,''}:
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from scripts import operator_backup as backup
from scripts import operator_abort as abort
from scripts import operator_deployment as mapping
from scripts import operator_entry as entry
from scripts import operator_prepare as preparation
from scripts import operator_registry as registry
from scripts import operator_source as sources
from scripts.operator_upgrade import Upgrade
from tests.operator_application_native import ApplicationTests


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--intent',type=Path,required=True)
    parser.add_argument('--lose-opening-reply',action='store_true')
    options=parser.parse_args()
    if sys.platform!='linux' or os.geteuid()!=0 or os.environ.get('FC_OPERATOR_APPLICATION_RUNTIME')!='1':
        raise RuntimeError('explicit root-only isolated application acceptance is required')
    evidence=backup.absolute(os.environ['FC_OPERATOR_APPLICATION_EVIDENCE'])
    path=backup.absolute(options.intent)
    intent=registry.private_json(path)
    directory=backup.absolute(intent['project_directory'])
    stage=backup.absolute(intent['stage_directory'])
    if (evidence not in directory.parents or evidence not in stage.parents or evidence not in path.parents
            or Path(__file__).resolve().parents[1]!=stage/'source'):
        raise RuntimeError('entry fixture must execute inside its isolated staged source and evidence')
    checked=sources.verify_stage(stage)
    if checked['provenance'].get('public_provenance_verified') is not False:
        raise RuntimeError('fixture cannot consume or pretend to prove a public release')
    if backup.bounded_json(stage/'source/protocol/python-operator.json')!=entry.PROTOCOL:
        raise RuntimeError('fixture must exercise the actual staged host protocol')
    descriptor=entry._lock(Path(intent['lock_path']))
    request=path.parent/'request.json'
    preparation._store(request,registry.sealed({**intent,'lock_fd':descriptor}))
    application=ApplicationTests('runTest')
    application.ca=directory.parent/'test-ca'
    application.docker=mapping.ComposeDocker(directory,[],intent['project'],[])
    application.clean_images={}
    original_prepare=preparation.prepare
    original_begin=Upgrade.begin
    original_advance=Upgrade.advance
    original_recover=abort.recover
    original_save=Upgrade._save
    def fixture_stage(selected):
        result=sources.verify_stage(selected)
        if selected!=stage or result!=checked:
            raise RuntimeError('fixture stage changed')
        return result
    def prepare(*arguments,**values):
        return original_prepare(*arguments,**values,acceptance=True,execute=application.candidate_build)
    def begin(*arguments,**values):return original_begin(*arguments,**values,acceptance=True)
    def advance(*arguments,**values):return original_advance(*arguments,**values,acceptance=True)
    def recover(*arguments,**values):return original_recover(*arguments,**values,acceptance=True)
    def save(operation,phase,**values):
        if options.lose_opening_reply and phase in {'opened','restored_opened'}:
            code=32 if phase=='opened' else 34
            print(json.dumps({'fixture_only':True,'business_may_have_written':True,
                'original_resources_may_have_new_writes':phase=='restored_opened','host_exit_code':code}),flush=True)
            os._exit(code)
        return original_save(operation,phase,**values)
    # Only native fixture admission and its private CA wrapper differ from the
    # product path. The staged orchestration, mapping, journal and helpers run.
    with patch.object(entry,'_stage',side_effect=fixture_stage),patch.object(preparation,'prepare',side_effect=prepare),\
            patch.object(Upgrade,'begin',side_effect=begin),patch.object(Upgrade,'advance',side_effect=advance),\
            patch.object(abort,'recover',side_effect=recover),\
            patch.object(Upgrade,'_save',new=save):
        result=entry.continue_upgrade(request)
    print(json.dumps({'fixture_only':True,'public_release_proved':False,'result':result,
        'executed_source':str(stage/'source'),'source_stage_sha256':checked['stage_sha256']}),flush=True)


if __name__=='__main__':main()
