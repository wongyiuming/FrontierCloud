"""Identity-pinned Target shadow removal after cancellation authorization.

These primitives provide no cancellation authority. Their caller must first
validate a fresh signed Master unit-cancel grant, prove current Source ownership
and retained originals, fence old native I/O and persist the returned unlink
intent. They must never be invoked for an acknowledged Master commit. Active
two-link assembly is reconciled and journaled before these one-link operations.
No payload is read, decrypted, copied or recursively removed here.
"""
import errno
import os
import stat

from app.services import resource_publish_files as publication,resource_transfer_files as staging
from app.services.resource_observation import native
from app.services.resource_pool import validate_media_path
from app.services.resource_transfer_protocol import TransferError,identifier

FORMAT='frontiercloud-python-shadow-unlink-intent'


def _validate(root,task_id,unit,obj,expected_root):
    identifier(task_id);identifier(unit['unit_id'],long=True)
    if (unit['state'] in {'committed','cleanup_pending','cleaned'} or unit.get('committed_at',0) or
            unit.get('cleanup_after',0) or unit['resource_kind'] not in {'media','recording'}):
        raise TransferError('migration_cancel_uncommitted_unit_required')
    identifier(obj['logical_id'],long=unit['resource_kind']=='media')
    if obj['unit_id']!=unit['unit_id'] or obj['resource_kind']!=unit['resource_kind']:
        raise TransferError('migration_cancel_object_binding_changed')
    path=obj['target_path']
    if unit['resource_kind']=='media':validate_media_path(path)
    else:
        parts=path.split('/')
        if len(parts)!=3 or not parts[2].endswith('.bin'):raise TransferError('migration_cancel_recording_path_invalid')
        for value in (parts[0],parts[1],parts[2][:-4]):identifier(value)
        if parts[2][:-4]!=obj['logical_id']:raise TransferError('migration_cancel_recording_path_invalid')
    if path.rsplit('/',1)[0]!=unit['parent_path']:
        raise TransferError('migration_cancel_object_binding_changed')
    # Even a published directory still belongs to the original private task
    # root. A replaced or removed task root cannot authorize public deletion.
    descriptor=staging._directory(root,task_id,expected_root=expected_root,create=False)
    os.close(descriptor)


def _private(root,task_id,unit,expected_root):
    try:return staging._directory(root,task_id,unit['unit_id'],expected_root,create=False)
    except FileNotFoundError:return None


def _publication_location(root,task_id,unit,expected_root,*,allow_missing=False):
    proof=unit['publication'];ready=proof.get('ready');parent=proof.get('parent')
    if not isinstance(ready,dict) or not isinstance(parent,dict):
        raise TransferError('migration_cancel_publication_identity_missing')
    private=_private(root,task_id,unit,expected_root);public=None;result=None
    try:
        if private is not None:
            try:descriptor=os.open('ready',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=private)
            except FileNotFoundError:pass
            else:
                try:publication.directory(os.fstat(descriptor),ready)
                except BaseException:os.close(descriptor);raise
                result=('ready',descriptor)
        public=publication._public_parent(root,unit['parent_path'],expected_root)
        publication.directory(os.fstat(public),parent)
        name=unit['parent_path'].rsplit('/',1)[-1]
        try:current=os.stat(name,dir_fd=public,follow_symlinks=False)
        except FileNotFoundError:current=None
        if current is not None and publication.identity(current)==ready:
            publication.directory(current,ready)
            if result is not None:raise TransferError('migration_cancel_multiple_publication_directories')
            descriptor=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=public)
            try:publication.directory(os.fstat(descriptor),ready)
            except BaseException:os.close(descriptor);raise
            result=('public',descriptor)
        elif current is not None and native(current)!=proof.get('empty_target'):
            raise TransferError('migration_cancel_foreign_target_directory')
        if result is None and not allow_missing:raise TransferError('migration_cancel_publication_directory_missing')
        selected=result;result=None;return selected
    finally:
        if private is not None:os.close(private)
        if public is not None:os.close(public)
        if result is not None:os.close(result[1])


def _location(root,task_id,unit,obj,expected_root,area):
    if area=='part':
        descriptor=_private(root,task_id,unit,expected_root)
        if descriptor is None:raise TransferError('migration_cancel_staging_directory_missing')
        return area,descriptor,obj['logical_id']+'.part'
    if area not in {'ready','public'}:raise TransferError('migration_cancel_unlink_intent_invalid')
    actual,descriptor=_publication_location(root,task_id,unit,expected_root)
    if actual!=area:
        os.close(descriptor);raise TransferError('migration_cancel_publication_location_changed')
    return actual,descriptor,obj['target_path'].rsplit('/',1)[-1]


def _file(descriptor,name):
    try:info=os.stat(name,dir_fd=descriptor,follow_symlinks=False)
    except FileNotFoundError:return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1:
        raise TransferError('migration_cancel_shadow_file_changed')
    return native(info)


def inspect_object(root,task_id,unit,obj,expected_root,expected_file,*,assembled=False):
    _validate(root,task_id,unit,obj,expected_root)
    if assembled:
        area,descriptor=_publication_location(root,task_id,unit,expected_root)
        name=obj['target_path'].rsplit('/',1)[-1]
    else:area,descriptor,name=_location(root,task_id,unit,obj,expected_root,'part')
    try:
        actual=_file(descriptor,name)
        if actual is None:raise TransferError('migration_cancel_shadow_file_missing')
        if actual!=expected_file:raise TransferError('migration_cancel_shadow_file_changed')
        return dict(format=FORMAT,version=1,task_id=task_id,unit_id=unit['unit_id'],logical_id=obj['logical_id'],
            area=area,directory=publication.identity(os.fstat(descriptor)),file=actual)
    finally:os.close(descriptor)


def unlink_object(root,task_id,unit,obj,expected_root,intent):
    _validate(root,task_id,unit,obj,expected_root)
    expected=dict(format=FORMAT,version=1,task_id=task_id,unit_id=unit['unit_id'],logical_id=obj['logical_id'])
    if (not isinstance(intent,dict) or intent.keys()!={*expected,'area','directory','file'} or
            any(intent.get(name)!=value for name,value in expected.items()) or type(intent['version']) is not int):
        raise TransferError('migration_cancel_unlink_intent_invalid')
    _,descriptor,name=_location(root,task_id,unit,obj,expected_root,intent['area'])
    try:
        if publication.identity(os.fstat(descriptor))!=intent['directory']:
            raise TransferError('migration_cancel_unlink_directory_changed')
        actual=_file(descriptor,name)
        if actual is not None:
            if actual!=intent['file']:raise TransferError('migration_cancel_shadow_file_reappeared_or_changed')
            os.unlink(name,dir_fd=descriptor)
        # An absent file is acceptable only when replaying the exact persisted
        # intent. A fresh observation above never accepts an absent shadow.
        os.fsync(descriptor)
    finally:os.close(descriptor)


def remove_directory(root,task_id,unit,obj,expected_root):
    _validate(root,task_id,unit,obj,expected_root)
    found=_publication_location(root,task_id,unit,expected_root,allow_missing=True)
    if found is None:return
    area,descriptor=found;parent=None
    try:
        with os.scandir(descriptor) as entries:
            if next(entries,None) is not None:raise TransferError('migration_cancel_unknown_publication_entry')
        if area=='ready':
            parent=_private(root,task_id,unit,expected_root);name='ready'
            if parent is None:raise TransferError('migration_cancel_staging_directory_missing')
        else:
            parent=publication._public_parent(root,unit['parent_path'],expected_root)
            publication.directory(os.fstat(parent),unit['publication']['parent'])
            name=unit['parent_path'].rsplit('/',1)[-1]
        if publication.identity(os.stat(name,dir_fd=parent,follow_symlinks=False))!=unit['publication']['ready']:
            raise TransferError('migration_cancel_publication_directory_changed')
        try:os.rmdir(name,dir_fd=parent)
        except OSError as error:
            if error.errno in {errno.ENOTEMPTY,errno.EEXIST}:
                raise TransferError('migration_cancel_unknown_publication_entry') from error
            raise
        os.fsync(parent)
    finally:
        os.close(descriptor)
        if parent is not None:os.close(parent)


def verify_removed(root,task_id,unit,obj,expected_root):
    """Readonly final check; a completed unlink cannot authorize a new removal."""
    _validate(root,task_id,unit,obj,expected_root)
    private=_private(root,task_id,unit,expected_root)
    if private is not None:
        os.close(private);raise TransferError('migration_cancel_staging_directory_reappeared')
    if 'ready' in unit['publication']:
        found=_publication_location(root,task_id,unit,expected_root,allow_missing=True)
        if found is not None:
            os.close(found[1]);raise TransferError('migration_cancel_shadow_directory_reappeared')


def inspect_empty_ready(root,task_id,unit_id,expected_root):
    """A failed first publication can leave only its known empty ready directory."""
    identifier(task_id);identifier(unit_id,long=True)
    try:private=staging._directory(root,task_id,unit_id,expected_root,create=False)
    except FileNotFoundError:return None
    ready=None
    try:
        try:ready=os.open('ready',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=private)
        except FileNotFoundError:return None
        info=os.fstat(ready);publication.directory(info)
        if stat.S_IMODE(info.st_mode)!=0o700:raise TransferError('migration_cancel_unjournaled_assembly_unsafe')
        with os.scandir(ready) as entries:
            if next(entries,None) is not None:raise TransferError('migration_cancel_unjournaled_assembly_not_empty')
        return dict(format='frontiercloud-python-empty-ready-unlink',version=1,task_id=task_id,unit_id=unit_id,
            private=publication.identity(os.fstat(private)),ready=publication.identity(info))
    finally:
        if ready is not None:os.close(ready)
        os.close(private)


def remove_empty_ready(root,task_id,unit_id,expected_root,intent):
    expected=dict(format='frontiercloud-python-empty-ready-unlink',version=1,task_id=task_id,unit_id=unit_id)
    if (not isinstance(intent,dict) or intent.keys()!={*expected,'private','ready'} or type(intent['version']) is not int or
            any(intent.get(name)!=value for name,value in expected.items())):raise TransferError('migration_cancel_empty_ready_intent_invalid')
    # Revalidate task-root selection even when a previous completed rmdir left
    # no private unit directory to inspect.
    selected=staging._directory(root,task_id,expected_root=expected_root,create=False);os.close(selected)
    try:private=staging._directory(root,task_id,unit_id,expected_root,create=False)
    except FileNotFoundError:return
    ready=None
    try:
        if publication.identity(os.fstat(private))!=intent['private']:raise TransferError('migration_cancel_empty_ready_directory_changed')
        try:ready=os.open('ready',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=private)
        except FileNotFoundError:return
        info=os.fstat(ready);publication.directory(info,intent['ready'])
        if stat.S_IMODE(info.st_mode)!=0o700:raise TransferError('migration_cancel_unjournaled_assembly_unsafe')
        with os.scandir(ready) as entries:
            if next(entries,None) is not None:raise TransferError('migration_cancel_unjournaled_assembly_not_empty')
        os.rmdir('ready',dir_fd=private);os.fsync(private)
    finally:
        if ready is not None:os.close(ready)
        os.close(private)
