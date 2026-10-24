"""Collect only bounded host-entry argv receipts, never private diagnostics."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat


CLI_SCRIPTS=frozenset(('frontiercloud_operator.py','operator_resume_fixture.py',
    'operator_entry_fixture.py','operator_selection_fixture.py','operator_install_handoff_fixture.py'))
INPUT_FIELDS=frozenset(('argv','working_directory','phase','started_at','diagnostic_file',
    'source_stage_sha256','intent','fixture_only','product_argv','inherited_lock_fd',
    'actual_os_exec','public_release_proved','simulated_old_discovery_rejects'))
RESULT_FIELDS=frozenset(('argv','working_directory','started_at','finished_at','exit_code',
    'timed_out','diagnostic_file','diagnostic_sha256'))
BRIDGE_FIELDS=frozenset(('fixture_only','real_cli_main_arguments','entry_source','phase',
    'command','public_release_proved'))
PATH_OPTIONS=frozenset(('--directory','--stage','--request','--project-directory','--intent','--next-stage','--operator-handoff'))
VALUE_OPTIONS={'--command':{'install','upgrade'},'--phase':{'old','retained','target'},
    '--lose':{'none','start','publication'},'--until':{'database_prepared','active_fenced','opened'}}
FLAGS=frozenset(('--storage','--leave-helper','--lose-opening-reply','--restore-fenced','--lose-payload-move',
    '--restore-open','--lose-recovery-reply','--abort-early','--lose-abort-reply'))


def _arguments(value):
    if not isinstance(value,list) or not all(isinstance(item,str) for item in value):return False
    index=0
    while index<len(value):
        token=value[index];index+=1
        if token in FLAGS or token in {'install','upgrade','inspect','resume','recover'}:continue
        if token in PATH_OPTIONS:
            if index==len(value) or not PurePosixPath(value[index]).is_absolute():return False
        elif token in VALUE_OPTIONS:
            if index==len(value) or value[index] not in VALUE_OPTIONS[token]:return False
        else:return False
        index+=1
    return True


def _argv(value):
    if not isinstance(value,list) or not value or not all(isinstance(item,str) for item in value):
        return False
    # No shell/program string is published. The accepted children receive only
    # operator commands, flags and fixture paths; their private request is omitted.
    targets=[index for index,item in enumerate(value[1:],1) if Path(item).name in CLI_SCRIPTS]
    return (Path(value[0]).name.startswith('python') and len(targets)==1
        and all(item in {'-I','-B'} for item in value[1:targets[0]])
        and _arguments(value[targets[0]+1:]))


def _read(path,root):
    if any(item.is_symlink() for item in (path,*path.parents) if item!=root.parent):
        raise ValueError('symlink in private evidence path')
    mode=path.stat()
    if not stat.S_ISREG(mode.st_mode) or mode.st_size>1024*1024:
        raise ValueError('invalid bounded operator input receipt')
    return json.loads(path.read_text(encoding='utf-8'))


def _save(path,value,upload,uid,gid):
    raw=(json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode()
    if any(marker in raw for marker in (b'PRIVATE KEY-----',b'X-Amz-Signature=',b'X-Amz-Credential=',
            b'AWS4-HMAC-SHA256 Credential=')):
        raise ValueError('usable secret in operator input receipt')
    destination=upload/path
    descriptor=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'wb') as stream:stream.write(raw)
    if uid is not None:os.chown(destination,uid,gid)


def collect(evidence,upload,*,uid=None,gid=None):
    """Partial failures may lack receipts; only known successful writes are read."""
    copied=[]
    for kind in ('operator-install','operator-application'):
        base=evidence/'private'/kind
        if not base.exists():continue
        for case in sorted(base.iterdir()):
            if not re.fullmatch(r'test_[a-z0-9_]+',case.name):continue
            paths=list(case.glob('*-input.json'))
            # Installation exec crosses an actual os.execve boundary. Only its
            # two explicit argv receipts are public, never request/lock/CA/env.
            for attempt in (case/'entry-host').glob('attempt-*'):
                for name in ('handoff-input.json','fixture-exec-input.json'):
                    if (attempt/name).exists():paths.append(attempt/name)
            for path in sorted(paths):
                value=_read(path,evidence)
                bridge=bool(re.fullmatch(r'cli-call-[0-9a-f]{16}-input.json',path.name))
                if bridge:
                    if set(value)-BRIDGE_FIELDS or not _arguments(value.get('real_cli_main_arguments')):
                        raise ValueError('unknown CLI bridge receipt fields')
                elif not _argv(value.get('argv')):
                    argv=value.get('argv',[])
                    if isinstance(argv,list) and any(isinstance(item,str) and Path(item).name in CLI_SCRIPTS for item in argv):
                        raise ValueError('unknown host-entry command arguments')
                    continue
                elif set(value)-INPUT_FIELDS:
                    raise ValueError('unknown host-entry input receipt fields')
                if 'simulated_old_discovery_rejects' in value and value['simulated_old_discovery_rejects']!='media-init':
                    raise ValueError('unknown fixture-only selection context')
                if 'product_argv' in value and not _argv(value['product_argv']):
                    raise ValueError('unknown product handoff command arguments')
                suffix=path.relative_to(case).as_posix().replace('/','-')
                name='operator-'+case.name+'-'+suffix
                _save(name,value,upload,uid,gid);copied.append(name)
                # Match the independently captured exit/timeout to the exact
                # accepted argv; raw child stdout/stderr stays private.
                result=path.with_name(path.name.removesuffix('-input.json')+'-result.json')
                if not bridge and result.exists():
                    observed=_read(result,evidence)
                    if set(observed)-RESULT_FIELDS or observed.get('argv')!=value['argv']:
                        raise ValueError('host-entry result does not match input')
                    result_name=name.removesuffix('-input.json')+'-result.json'
                    _save(result_name,observed,upload,uid,gid);copied.append(result_name)
    return copied


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence',type=Path);parser.add_argument('upload',type=Path)
    parser.add_argument('--uid',type=int,required=True);parser.add_argument('--gid',type=int,required=True)
    arguments=parser.parse_args()
    collect(arguments.evidence,arguments.upload,uid=arguments.uid,gid=arguments.gid)


if __name__=='__main__':main()
