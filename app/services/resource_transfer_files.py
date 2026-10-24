"""Joined, nofollow staging primitives. Only private task paths are writable."""
import hashlib
import errno
import os
import stat

from app.services.resource_inventory import _root
from app.services.resource_observation import digest,native
from app.services.resource_transfer_protocol import TransferError

DIRECTORY='.frontiercloud-migration'


def _directory(root,task_id,unit_id=None,expected_root=None,*,create=True):
    descriptor,exists=_root(root)
    if not exists:
        os.close(descriptor);raise TransferError('migration_transfer_staging_root_missing')
    if expected_root is not None and (os.fstat(descriptor).st_dev,os.fstat(descriptor).st_ino)!=(expected_root['device'],expected_root['base_inode']):
        os.close(descriptor);raise TransferError('migration_transfer_staging_root_changed')
    try:
        for name in (DIRECTORY,task_id,*([unit_id] if unit_id else [])):
            if create:
                try:
                    os.mkdir(name,0o700,dir_fd=descriptor);os.fsync(descriptor)
                except FileExistsError:pass
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
            info=os.fstat(child)
            if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o077:
                os.close(child);raise TransferError('migration_transfer_staging_permissions_invalid')
            if name==task_id and expected_root is not None and (info.st_dev,info.st_ino)!=(expected_root['device'],expected_root['root_inode']):
                os.close(child);raise TransferError('migration_transfer_staging_root_changed')
            os.close(descriptor);descriptor=child
        return descriptor
    except BaseException:
        os.close(descriptor);raise


def prepare_probe(root,task_id):
    base,exists=_root(root)
    try:
        if not exists:raise TransferError('migration_transfer_staging_root_missing')
        base_info=os.fstat(base)
    finally:os.close(base)
    directory=_directory(root,task_id)
    try:return dict(device=os.fstat(directory).st_dev,root_inode=os.fstat(directory).st_ino,
        base_inode=base_info.st_ino,writable_verified=False)
    finally:os.close(directory)


def probe(root,task_id,binding,expected_root=None):
    directory=_directory(root,task_id,expected_root=expected_root)
    expected=hashlib.sha256(('frontiercloud-staging-probe:'+task_id+':'+binding).encode()).digest()
    descriptor=None
    try:
        descriptor=os.open('probe.bin',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=directory)
        info=os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or
                stat.S_IMODE(info.st_mode)&0o077 or not 0<=info.st_size<=len(expected) or
                os.pread(descriptor,info.st_size,0)!=expected[:info.st_size]):
            raise TransferError('migration_transfer_probe_conflict')
        write(descriptor,expected,0);os.fsync(descriptor)
        if os.pread(descriptor,len(expected),0)!=expected:raise TransferError('migration_transfer_probe_failed')
        current=os.stat('probe.bin',dir_fd=directory,follow_symlinks=False)
        if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):raise TransferError('migration_transfer_probe_conflict')
        os.unlink('probe.bin',dir_fd=directory);os.fsync(directory)
        result=prepare_probe(root,task_id);result['writable_verified']=True;return result
    finally:
        if descriptor is not None:os.close(descriptor)
        os.close(directory)


def open_stage(root,task_id,obj,expected_root):
    directory=_directory(root,task_id,obj['unit_id'],expected_root)
    descriptor=None
    try:
        descriptor=os.open(obj['logical_id']+'.part',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=directory)
        info=os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or
                stat.S_IMODE(info.st_mode)&0o077 or not 0<=info.st_size<=obj['expected_bytes']):
            raise TransferError('migration_transfer_staging_conflict')
        os.fsync(directory)
        return descriptor,info
    except BaseException:
        if descriptor is not None:os.close(descriptor)
        raise
    finally:os.close(directory)


def write(descriptor,data,offset):
    view=memoryview(data);written=0
    while written<len(view):
        count=os.pwrite(descriptor,view[written:],offset+written)
        if count<=0:raise OSError('migration_transfer_short_write')
        written+=count


def stage_identity(info):return digest([info.st_dev,info.st_ino])


def inspect_stage(root,task_id,obj,expected_root):
    try:directory=_directory(root,task_id,obj['unit_id'],expected_root,create=False)
    except FileNotFoundError:return None
    try:
        try:descriptor=os.open(obj['logical_id']+'.part',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        except FileNotFoundError:return None
        try:
            info=os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or
                    stat.S_IMODE(info.st_mode)&0o077 or not 0<=info.st_size<=obj['expected_bytes']):
                raise TransferError('migration_abort_stage_conflict')
            return dict(identity=stage_identity(info),version=digest(native(info)),bytes=info.st_size)
        finally:os.close(descriptor)
    finally:os.close(directory)


def unlink_stage(root,task_id,obj,expected_root,identity,version):
    try:directory=_directory(root,task_id,obj['unit_id'],expected_root,create=False)
    except FileNotFoundError:return
    try:
        name=obj['logical_id']+'.part'
        try:info=os.stat(name,dir_fd=directory,follow_symlinks=False)
        except FileNotFoundError:return
        if not stat.S_ISREG(info.st_mode) or stage_identity(info)!=identity or digest(native(info))!=version:
            raise TransferError('migration_abort_stage_changed')
        os.unlink(name,dir_fd=directory);os.fsync(directory)
    finally:os.close(directory)


def remove_unit(root,task_id,unit_id,expected_root):
    if expected_root is None:
        base,exists=_root(root);os.close(base)
        if not exists:return
    try:directory=_directory(root,task_id,expected_root=expected_root,create=False)
    except FileNotFoundError:return
    try:
        try:child=os.open(unit_id,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
        except FileNotFoundError:return
        try:
            info=os.fstat(child)
            if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o077:raise TransferError('migration_abort_stage_conflict')
        finally:os.close(child)
        try:os.rmdir(unit_id,dir_fd=directory)
        except OSError as exc:
            if exc.errno in {errno.ENOTEMPTY,errno.EEXIST}:raise TransferError('migration_abort_unknown_stage') from exc
            raise
        os.fsync(directory)
    finally:os.close(directory)


def finish_task(root,task_id,expected_root,binding):
    if expected_root is None:
        base,exists=_root(root);os.close(base)
        if not exists:return
    try:directory=_directory(root,task_id,expected_root=expected_root,create=False)
    except FileNotFoundError:return
    try:
        # A failed probe may retain only its explicitly bound 32-byte marker.
        try:descriptor=os.open('probe.bin',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        except FileNotFoundError:descriptor=None
        if descriptor is not None:
            try:
                info=os.fstat(descriptor);expected=hashlib.sha256(('frontiercloud-staging-probe:'+task_id+':'+binding).encode()).digest()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or
                        not 0<=info.st_size<=32 or os.pread(descriptor,33,0)!=expected[:info.st_size]):
                    raise TransferError('migration_abort_probe_conflict')
                current=os.stat('probe.bin',dir_fd=directory,follow_symlinks=False)
                if digest(native(current))!=digest(native(info)):raise TransferError('migration_abort_probe_conflict')
                os.unlink('probe.bin',dir_fd=directory);os.fsync(directory)
            finally:os.close(descriptor)
    finally:os.close(directory)
    base,exists=_root(root)
    try:
        if not exists:raise TransferError('migration_transfer_staging_root_missing')
        parent=os.open(DIRECTORY,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=base)
        try:
            # rmdir refuses every unknown nonempty task/unit, without recursion.
            try:os.rmdir(task_id,dir_fd=parent)
            except OSError as exc:
                if exc.errno in {errno.ENOTEMPTY,errno.EEXIST}:raise TransferError('migration_abort_unknown_stage') from exc
                raise
            os.fsync(parent)
        finally:os.close(parent)
    finally:os.close(base)
