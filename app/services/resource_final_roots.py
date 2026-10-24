"""Private root identity bindings and bounded empty-Source census."""
import os
from pathlib import Path

from app.services.resource_inventory import Walker,_root
from app.services.resource_observation import digest
from app.services.resource_transfer_protocol import TransferError

KINDS=('media','recordings')


def capture(paths):
    result={}
    for kind in KINDS:
        path=Path(paths[kind]).absolute();descriptor,exists=_root(path)
        try:
            info=os.fstat(descriptor)
            result[kind]=dict(path_sha256=digest(str(path)),exists=exists,device=info.st_dev,inode=info.st_ino,
                mode=info.st_mode,uid=info.st_uid,gid=info.st_gid)
        finally:os.close(descriptor)
    return result


def check(paths,expected):
    if not isinstance(expected,dict) or expected.keys()!=set(KINDS) or capture(paths)!=expected:
        raise TransferError('migration_finalize_source_root_changed')


class EmptySource:
    def __init__(self,paths,expected,key,now):
        check(paths,expected)
        self.paths,self.expected,self.key=paths,expected,key
        self.deadline=now+300;self.index=0;self.walker=None;self.directories=0;self.complete=False

    def close(self):
        if self.walker is not None:self.walker.close();self.walker=None

    def page(self):
        if self.complete:raise TransferError('migration_finalize_inventory_already_complete')
        check(self.paths,self.expected)
        if self.walker is None:self.walker=Walker(self.paths[KINDS[self.index]])
        entries,finished=self.walker.page()
        if any(entry['kind']!='directory' for entry in entries):
            raise TransferError('migration_finalize_source_resources_remain')
        self.directories+=len(entries)
        if finished:
            self.close();self.index+=1;self.complete=self.index==len(KINDS)
        return self.complete
