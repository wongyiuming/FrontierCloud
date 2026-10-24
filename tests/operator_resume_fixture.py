"""Explicit isolated-host process-death acceptance; never a product entry."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
from unittest.mock import patch
import uuid

if __package__ in {None,''}:
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from scripts import operator_backup as backup
from scripts import operator_prepare as prepare
from scripts import operator_upgrade as upgrade
from scripts import operator_recovery as recovery
from scripts import operator_deployment as mapping
from scripts import operator_abort as abort


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True,type=Path)
    parser.add_argument('--until',choices=['database_prepared','active_fenced','opened'],default='opened')
    parser.add_argument('--leave-helper',action='store_true')
    parser.add_argument('--lose-opening-reply',action='store_true')
    parser.add_argument('--restore-fenced',action='store_true')
    parser.add_argument('--lose-payload-move',action='store_true')
    parser.add_argument('--restore-open',action='store_true')
    parser.add_argument('--lose-recovery-reply',action='store_true')
    parser.add_argument('--abort-early',action='store_true')
    parser.add_argument('--lose-abort-reply',action='store_true')
    options = parser.parse_args()
    if (sys.platform != 'linux' or os.geteuid() != 0
            or os.environ.get('FC_OPERATOR_APPLICATION_RUNTIME') != '1'):
        raise RuntimeError('explicit root-only isolated application acceptance is required')
    evidence = backup.absolute(os.environ['FC_OPERATOR_APPLICATION_EVIDENCE'])
    directory = backup.absolute(options.directory)
    if evidence not in directory.parents:
        raise RuntimeError('fixture target must be inside the isolated application evidence directory')
    operation = upgrade.Upgrade.load(directory,acceptance=True,check_images=not options.restore_fenced)
    if options.abort_early:
        save=upgrade.Upgrade._save
        def lose_abort(self,phase,**values):
            if options.lose_abort_reply and phase=='restored_opened':
                print(json.dumps({'fixture_only':True,'original_resources_may_have_new_writes':True,'host_exit_code':34}),flush=True)
                os._exit(34)
            return save(self,phase,**values)
        with patch.object(upgrade.Upgrade,'_save',new=lose_abort):
            result=abort.OriginalAbort(operation).abort()
        print(json.dumps({'fixture_only':True,'phase':operation.state['phase'],'operation_id':operation.state['id'],'result':result}),flush=True)
        return 0
    if options.restore_open:
        save = upgrade.Upgrade._save
        def lose_recovery(self,phase,**values):
            if options.lose_recovery_reply and phase == 'restored_opened':
                print(json.dumps({'fixture_only':True,'recovered_business_may_have_written':True,'host_exit_code':34}),flush=True)
                os._exit(34)
            return save(self,phase,**values)
        with patch.object(upgrade.Upgrade,'_save',new=lose_recovery):
            result = recovery.Recovery(operation).open()
        print(json.dumps({'fixture_only':True,'phase':operation.state['phase'],'operation_id':operation.state['id'],
            'result':result}),flush=True)
        return 0
    if options.restore_fenced:
        rename = recovery.rename_noreplace
        def lost(source,destination):
            rename(source,destination)
            print(json.dumps({'fixture_only':True,'payload_renamed':True,'host_exit_code':33}),flush=True)
            os._exit(33)
        with patch.object(recovery,'rename_noreplace',new=lost if options.lose_payload_move else rename):
            result = recovery.Recovery(operation).prepare()
        print(json.dumps({'fixture_only':True,'phase':operation.state['phase'],'operation_id':operation.state['id'],
            'result':result}),flush=True)
        return 0
    if options.leave_helper:
        attempt = operation.prepared.directory/('host-death-'+uuid.uuid4().hex)
        attempt.mkdir(mode=0o700)
        def die(docker,arguments,output,step,**_options):
            cut = arguments.index('web',arguments.index('--entrypoint'))+1
            arguments = arguments[:cut]+['-c','import time; print("HOST_DEATH_HELPER_RUNNING",flush=True); time.sleep(150)']
            backup.private_write(output/'fault.json',backup.json_bytes({'fixture_only':True,'fault':'abrupt_host_process_death',
                'compose_client_signal':'SIGKILL','host_exit_code':31}))
            killpg = os.killpg
            try:
                with patch.object(prepare.os,'killpg',side_effect=lambda pid,_signal:killpg(pid,signal.SIGKILL)):
                    prepare._command(docker,arguments,output,step,timeout=5)
            except mapping.DeploymentError:
                owner = backup.bounded_json(output/'helper-owner.json')
                current = docker.run(['docker','inspect',owner['name']],json_output=True)[0]
                if not current['State']['Running']:
                    raise RuntimeError('daemon-side helper did not outlive the host client')
                print(json.dumps({'fixture_only':True,'daemon_helper_running':True,'container_id':current['Id'],
                    'helper_name':owner['name']}),flush=True)
                os._exit(31)  # Deliberately skip product finally and interpreter cleanup.
        operation._database_helper('preflight',attempt,execute=die)
        raise RuntimeError('expected host process death did not occur')
    save = upgrade.Upgrade._save
    def lose(self,phase,**values):
        if options.lose_opening_reply and phase == 'opened':
            print(json.dumps({'fixture_only':True,'business_may_have_written':True,'host_exit_code':32}),flush=True)
            os._exit(32)
        return save(self,phase,**values)
    with patch.object(upgrade.Upgrade,'_save',new=lose):
        while operation.state['phase'] != options.until:
            phase = operation.state['phase']
            if phase in {'images_prepared','fencing','fenced','stopping','cold','backing_up'}:
                operation.capture()
            elif phase in {'backed_up','database_inspecting','database_preparing'}:
                operation.prepare_database()
            elif phase in {'database_prepared','activating'}:
                operation.activate_fenced()
            elif phase in {'active_fenced','opening_stopping','opening_stopped','opening'}:
                operation.open()
            else:
                raise RuntimeError('fixture cannot reach its selected stop phase')
            operation = upgrade.Upgrade.load(directory,acceptance=True)
    print(json.dumps({'fixture_only':True,'phase':operation.state['phase'],'operation_id':operation.state['id'],
        'result':operation.state.get('open_result',operation.state.get('database_result'))}),flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
