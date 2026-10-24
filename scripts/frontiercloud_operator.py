#!/usr/bin/env python3
"""One-shot host Python operator. Only implemented operations are exposed."""
from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import operator_backup as backup
from scripts.operator_deployment import Deployment, DeploymentError
from scripts.operator_release import ReleaseError, select_latest


def recorded_operation(directory):
    """Private journal observation only; never reconciles or stops a helper."""
    from scripts import operator_entry
    reference,state=operator_entry._operation(directory)
    if reference is None:
        from scripts.operator_install_entry import recorded
        reference,_receipt,state=recorded(directory)
        if reference is None:return None
    return {'recorded_phase':state['phase'],'business_may_have_written':bool(state.get('business_may_have_written')),
        **({'recovery_mode':state['recovery'].get('mode','snapshot')} if state.get('recovery') else {})}


def error_details(error):
    if isinstance(error,(DeploymentError,ReleaseError)):
        return {'error':error.code,'message':str(error)}
    if isinstance(error,backup.BackupError):
        return {'error':'operator_validation_failed','message':str(error)}
    if isinstance(error,OSError):
        code={errno.ENOSPC:'host_space_exhausted',errno.EACCES:'host_permission_denied',errno.EPERM:'host_permission_denied',
            errno.ENOENT:'host_material_missing',errno.EROFS:'host_readonly_filesystem',errno.EXDEV:'host_cross_device_refused',
            errno.EEXIST:'host_destination_occupied',errno.ETIMEDOUT:'host_io_timeout'}.get(error.errno,'host_io_failed')
        return {'error':code,'host_errno':error.errno,'message':errno.errorcode.get(error.errno,'host I/O operation failed')}
    return {'error':'invalid_operator_material','message':type(error).__name__}


def main(arguments=None):
    supplied=list(sys.argv[1:] if arguments is None else arguments)
    handoff=supplied[:1]==['--operator-handoff']
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="read-only inspection of this Compose deployment")
    inspect.add_argument("--project-directory", type=Path, default=Path.cwd())
    resume = commands.add_parser('resume',help='resume the same recorded host upgrade operation')
    resume.add_argument('--project-directory',type=Path,default=Path.cwd())
    upgrade=commands.add_parser('upgrade',help='select published source and upgrade or resume this deployment')
    upgrade.add_argument('--project-directory',type=Path,default=Path.cwd())
    recover=commands.add_parser('recover',help='cancel before database preparation or recover the same recorded old state')
    recover.add_argument('--project-directory',type=Path,default=Path.cwd())
    install=commands.add_parser('install',help='install published source or resume the same recorded installation')
    install.add_argument('--project-directory',type=Path,default=Path.cwd())
    install.add_argument('--storage',action='store_const',const=True,default=None,help='select the independent Storage base on first installation')
    commands.add_parser("candidate", help="read-only verification of the newest published Python source")
    if handoff:
        private=argparse.ArgumentParser(add_help=False)
        private.add_argument('--operator-handoff',type=Path,required=True)
        options=private.parse_args(supplied)
        options.command='upgrade'
        options.project_directory=Path.cwd()
    else:
        options = parser.parse_args(supplied)
    try:
        if options.command == "candidate":
            result = {"state": "candidate_verified", **select_latest(), "deployment_changed": False}
        else:
            if sys.platform != "linux" or os.geteuid() != 0:
                raise DeploymentError("unsupported_host", "run this operator as root on the local Linux Docker host")
            if handoff:
                from scripts import operator_entry
                from scripts.operator_registry import private_json
                request=private_json(options.operator_handoff)
                command=request.get('command','upgrade') if isinstance(request,dict) else None
                if not isinstance(command,str) or command not in {'upgrade','resume','recover','install'}:
                    raise DeploymentError('operator_handoff_invalid','the private source handoff has no valid command')
                options.command=command
                result=operator_entry.continue_upgrade(options.operator_handoff)
            elif options.command in {'upgrade','resume','recover','install'}:
                from scripts import operator_entry
                result=operator_entry.start(options.project_directory,command=options.command,
                    **({'storage':options.storage} if options.command=='install' else {}))
            else:
                result = Deployment.discover(options.project_directory).summary()
        print(json.dumps(result, sort_keys=True))
        return 0
    except (backup.BackupError, OSError, ValueError, KeyError, TypeError) as error:
        waiting = isinstance(error, ReleaseError) and error.waiting
        mutating=options.command in {'resume','recover','upgrade','install'}
        observed=None
        if mutating and sys.platform=='linux' and os.geteuid()==0:
            try:observed=recorded_operation(options.project_directory)
            except (backup.BackupError,OSError,ValueError,KeyError,TypeError):pass
        print(json.dumps({'state':'waiting' if waiting else 'refused','command':options.command,**error_details(error),
            'deployment_changed':None if mutating else False,**(observed or {})}))
        return 3 if waiting else 1


if __name__ == "__main__":
    raise SystemExit(main())
