"""Bounded publication I/O under durable managed-namespace exclusions.

Hard links assemble a private directory without copying payload bytes. The final
directory rename is atomic; arbitrary external writers are not coordinated by
these exclusions and are not a supported safe publication environment.
"""
import os
import stat

from app.services import resource_transfer_files as staging
from app.services.resource_inventory import _root
from app.services.resource_observation import digest,native
from app.services.resource_transfer_protocol import TransferError


def identity(info):return dict(device=info.st_dev,inode=info.st_ino)


def directory(info,expected=None):
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid():
        raise TransferError('migration_publish_directory_invalid')
    if expected is not None and identity(info)!=expected:
        raise TransferError('migration_publish_directory_changed')


def _public_parent(root,path,expected_root,*,create=False):
    descriptor,exists=_root(root)
    try:
        if not exists or identity(os.fstat(descriptor))!={'device':expected_root['device'],'inode':expected_root['base_inode']}:
            raise TransferError('migration_publish_root_changed')
        for name in path.split('/')[:-1]:
            if create:
                try:os.mkdir(name,0o755,dir_fd=descriptor);os.fsync(descriptor)
                except FileExistsError:pass
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
            directory(os.fstat(child));os.close(descriptor);descriptor=child
        return descriptor
    except BaseException:os.close(descriptor);raise


def prepare(root,task_id,unit,expected_root):
    private=staging._directory(root,task_id,unit['unit_id'],expected_root,create=False)
    parent=None;ready=None
    try:
        # A failed preparation can leave only this known empty assembly dir.
        try:os.mkdir('ready',0o700,dir_fd=private);os.fsync(private)
        except FileExistsError:pass
        ready=os.open('ready',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=private)
        directory(os.fstat(ready))
        with os.scandir(ready) as entries:
            if next(entries,None) is not None:raise TransferError('migration_publish_unjournaled_assembly')
        parent=_public_parent(root,unit['parent_path'],expected_root,create=True)
        if os.fstat(parent).st_dev!=os.fstat(ready).st_dev:raise TransferError('migration_publish_cross_device')
        name=unit['parent_path'].rsplit('/',1)[-1]
        try:
            existing=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
        except FileNotFoundError:empty=None
        else:
            try:
                directory(os.fstat(existing))
                with os.scandir(existing) as entries:
                    if next(entries,None) is not None:raise TransferError('migration_publish_target_occupied')
                empty=native(os.fstat(existing))
            finally:os.close(existing)
        return dict(ready=identity(os.fstat(ready)),parent=identity(os.fstat(parent)),empty_target=empty)
    finally:
        for descriptor in (ready,parent,private):
            if descriptor is not None:os.close(descriptor)


def _file(info,expected,*,links):
    value=native(info)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=links or
        any(value[key]!=expected[key] for key in expected if key not in ('ctime_ns','links'))):
        raise TransferError('migration_publish_file_changed')
    return value


def assemble(root,task_id,unit_id,obj,expected_root,proof,expected):
    private=staging._directory(root,task_id,unit_id,expected_root,create=False)
    ready=None
    try:
        ready=os.open('ready',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=private)
        directory(os.fstat(ready),proof['ready'])
        source=obj['logical_id']+'.part';target=obj['target_path'].rsplit('/',1)[-1]
        try:old=os.stat(source,dir_fd=private,follow_symlinks=False)
        except FileNotFoundError:old=None
        try:new=os.stat(target,dir_fd=ready,follow_symlinks=False)
        except FileNotFoundError:new=None
        if old is None:
            if new is None:raise TransferError('migration_publish_file_missing')
            return _file(new,expected,links=1)
        if new is None:
            # Before our first link, the complete verified stat must still match.
            if native(old)!=expected:raise TransferError('migration_publish_file_changed')
            os.link(source,target,src_dir_fd=private,dst_dir_fd=ready,follow_symlinks=False)
            os.fsync(ready)
        old=os.stat(source,dir_fd=private,follow_symlinks=False)
        new=os.stat(target,dir_fd=ready,follow_symlinks=False)
        _file(old,expected,links=2);_file(new,expected,links=2)
        if identity(old)!=identity(new):raise TransferError('migration_publish_file_changed')
        os.unlink(source,dir_fd=private);os.fsync(private);os.fsync(ready)
        return _file(os.stat(target,dir_fd=ready,follow_symlinks=False),expected,links=1)
    finally:
        if ready is not None:os.close(ready)
        os.close(private)


def open_directory(root,task_id,unit,expected_root,proof,*,published):
    if published:
        parent=_public_parent(root,unit['parent_path'],expected_root)
        try:
            directory(os.fstat(parent),proof['parent'])
            descriptor=os.open(unit['parent_path'].rsplit('/',1)[-1],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
        finally:os.close(parent)
    else:
        parent=staging._directory(root,task_id,unit['unit_id'],expected_root,create=False)
        try:descriptor=os.open('ready',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
        finally:os.close(parent)
    try:directory(os.fstat(descriptor),proof['ready']);return descriptor
    except BaseException:os.close(descriptor);raise


def move_directory(root,task_id,unit,expected_root,proof):
    private=staging._directory(root,task_id,unit['unit_id'],expected_root,create=False)
    parent=None
    try:
        parent=_public_parent(root,unit['parent_path'],expected_root)
        directory(os.fstat(parent),proof['parent'])
        name=unit['parent_path'].rsplit('/',1)[-1]
        try:current=os.stat(name,dir_fd=parent,follow_symlinks=False)
        except FileNotFoundError:current=None
        if current is not None and identity(current)==proof['ready']:
            directory(current,proof['ready']);return
        if current is not None:
            if native(current)!=proof['empty_target']:raise TransferError('migration_publish_target_changed')
            directory(current)
            # rmdir itself refuses a newly nonempty directory. No recursive delete.
            os.rmdir(name,dir_fd=parent);os.fsync(parent)
        descriptor=open_directory(root,task_id,unit,expected_root,proof,published=False)
        try:
            with os.scandir(private) as entries:
                first=next(entries,None)
                if first is None or first.name!='ready' or next(entries,None) is not None:
                    raise TransferError('migration_publish_unknown_entry')
            if digest(native(os.fstat(descriptor)))!=proof['scan_version']:
                raise TransferError('migration_publish_directory_changed')
            # Every managed writer holds the same namespace exclusions. A
            # nonempty/regular-file destination is also refused by rename itself.
            try:os.stat(name,dir_fd=parent,follow_symlinks=False)
            except FileNotFoundError:pass
            else:raise TransferError('migration_publish_target_changed')
            os.rename('ready',name,src_dir_fd=private,dst_dir_fd=parent)
            os.fsync(descriptor);os.fsync(parent);os.fsync(private)
        finally:os.close(descriptor)
    finally:
        if parent is not None:os.close(parent)
        os.close(private)


class Scan:
    def __init__(self,key,descriptor):
        self.key,self.descriptor=key,descriptor
        self.version=digest(native(os.fstat(descriptor)));self.iterator=os.scandir(descriptor)
        self.count=0;self.bytes=0;self.finished=False

    def close(self):
        if self.descriptor is not None:
            self.iterator.close();os.close(self.descriptor);self.descriptor=None

    def page(self):
        if self.finished or digest(native(os.fstat(self.descriptor)))!=self.version:
            raise TransferError('migration_publish_scan_changed')
        result=[]
        for _ in range(32):
            item=next(self.iterator,None)
            if item is None:self.finished=True;break
            info=item.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid():
                raise TransferError('migration_publish_unknown_entry')
            result.append(dict(name=item.name,native=native(info)))
            self.count+=1;self.bytes+=info.st_size
        if digest(native(os.fstat(self.descriptor)))!=self.version:raise TransferError('migration_publish_scan_changed')
        return result


def inspect_published(root,task_id,unit,obj,expected_root,proof,expected):
    descriptor=open_directory(root,task_id,unit,expected_root,proof,published=True)
    try:
        info=os.stat(obj['target_path'].rsplit('/',1)[-1],dir_fd=descriptor,follow_symlinks=False)
        if native(info)!=expected:raise TransferError('migration_publish_file_changed')
        return _file(info,expected,links=1)
    finally:os.close(descriptor)


def published_version(root,task_id,unit,expected_root,proof):
    descriptor=open_directory(root,task_id,unit,expected_root,proof,published=True)
    try:return digest(native(os.fstat(descriptor)))
    finally:os.close(descriptor)
