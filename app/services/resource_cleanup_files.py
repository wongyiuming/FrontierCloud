"""Identity-pinned Source unlink primitives; never recursive directory deletion."""
import os
import stat

from app.services.resource_inventory import _root
from app.services.resource_observation import digest,native
from app.services.resource_transfer_protocol import TransferError


class RescanRequired(TransferError):
    def __init__(self):super().__init__('migration_cleanup_directory_requires_rescan')


def directory(root,parent,proof):
    descriptor,exists=_root(root)
    try:
        info=os.fstat(descriptor);expected=proof['root']
        if not exists or any(native(info)[key]!=expected[key] for key in ('device','inode','mode')):
            raise TransferError('migration_cleanup_root_changed')
        parts=parent.split('/')
        if not parts or any(part in ('','.','..') or '\\' in part or '\x00' in part for part in parts):
            raise TransferError('migration_cleanup_path_invalid')
        for part in parts:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
            os.close(descriptor);descriptor=child
        info=os.fstat(descriptor);expected=proof['directory']
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or
                any(native(info)[key]!=expected[key] for key in ('device','inode','mode'))):
            raise TransferError('migration_cleanup_directory_changed')
        return descriptor
    except BaseException:os.close(descriptor);raise


def _stat(descriptor,name):
    if name in ('','.','..') or '/' in name or '\\' in name or '\x00' in name:
        raise TransferError('migration_cleanup_path_invalid')
    try:info=os.stat(name,dir_fd=descriptor,follow_symlinks=False)
    except FileNotFoundError:return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1:
        raise TransferError('migration_cleanup_source_file_changed')
    return native(info)


def inspect(root,parent,name,proof):
    descriptor=directory(root,parent,proof)
    try:return dict(directory=native(os.fstat(descriptor)),file=_stat(descriptor,name))
    finally:os.close(descriptor)


def unlink(root,parent,name,proof,expected_directory,expected_file,allow_missing):
    descriptor=directory(root,parent,proof)
    try:
        if digest(native(os.fstat(descriptor)))!=expected_directory:raise RescanRequired()
        info=_stat(descriptor,name)
        if info is None:
            if not allow_missing:raise RescanRequired()
        else:
            if allow_missing:raise TransferError('migration_cleanup_source_file_reappeared')
            if info!=expected_file:raise TransferError('migration_cleanup_source_file_changed')
            os.unlink(name,dir_fd=descriptor)
        # Repeating this after a crash also establishes durability of an
        # already absent, explicitly journaled file. No ancestor is removed.
        os.fsync(descriptor)
        return native(os.fstat(descriptor))
    finally:os.close(descriptor)
