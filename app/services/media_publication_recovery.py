"""Durable, bounded media publication proofs; never guess an ambiguous commit."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

from sqlalchemy import text

PREFIX = '.frontiercloud-media-publication-'
NAME = re.compile(r'^\.frontiercloud-media-publication-([a-f0-9]{32})\.json$')
MAXIMUM = (1 << 63) - 1


class PublicationCommitUnknown(RuntimeError):
    pass


def sync_directory(directory):
    if os.name != 'nt':
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def sync_hierarchy(root, directory):
    """Persist publication and every parent entry inside the managed root."""
    root, directory = Path(root), Path(directory)
    if directory != root and root not in directory.parents:
        raise RuntimeError('Publication directory escapes the managed root')
    while True:
        sync_directory(directory)
        if directory == root:
            return
        directory = directory.parent


def pending(root):
    return sorted(Path(root).parent.glob(PREFIX + '*'))


def journal_path(root, publication):
    return Path(root).parent / (PREFIX + publication['publication_id'] + '.json')


def validate(value):
    from app.services.media_crypto import Metadata
    fields = {'version', 'publication_id', 'object_id', 'path', 'kind', 'action', 'size', 'sha256', 'encryption', 'device', 'inode'}
    master = isinstance(value, dict) and value.get('version') == 2
    if master:
        fields |= {'upload_id', 'storage_member_id'}
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeError('Invalid media publication journal')
    parts = str(value['path']).split('/')
    kind, action = ('lyric', 'upload_lyric') if parts[0] == 'lyrics' else ('audio', 'upload_item') if parts[0] == 'music' else ('video', 'upload_item')
    extensions = {'lyric': {'.lrc'}, 'audio': {'.mp3', '.m4a', '.flac', '.wav'}, 'video': {'.mp4', '.webm', '.mkv'}}
    if master:
        action = 'upload-finalized'
    if (type(value['version']) is not int or value['version'] not in {1, 2}
            or not isinstance(value['path'], str) or len(value['path']) > 4096
            or not all(isinstance(value[k], str) for k in ('publication_id', 'object_id', 'sha256'))
            or not re.fullmatch('[a-f0-9]{32}', str(value['publication_id']))
            or not re.fullmatch('[a-f0-9]{64}', str(value['object_id']))
            or not re.fullmatch('[a-f0-9]{64}', str(value['sha256']))
            or parts[0] not in {'lyrics', 'music', 'vido'}
            or len(parts) not in ({2, 3, 4} if kind == 'lyric' else {3, 4})
            or Path(parts[-1]).suffix.lower() not in extensions[kind]
            or value['kind'] != kind or value['action'] != action
            or value['path'] == 'lyrics/default.lrc'
            or any(not p or p in {'.', '..'} or p.startswith('.') or '\\' in p or '\x00' in p for p in parts)
            or type(value['size']) is not int or not 0 < value['size'] <= MAXIMUM
            or any(type(value[k]) is not int or value[k] < 0 for k in ('device', 'inode'))):
        raise RuntimeError('Unsafe media publication journal')
    if master and (kind == 'lyric' or any(not isinstance(value[key], str)
            or not re.fullmatch('[a-f0-9]{32}', value[key]) for key in ('upload_id', 'storage_member_id'))):
        raise RuntimeError('Invalid MasterLocal publication identity')
    if value['encryption'] is not None:
        descriptor = Metadata.from_dict(value['encryption'])
        if (kind == 'lyric' and descriptor.plaintext_size > 2 * 1024 * 1024
                or descriptor.ciphertext_size != value['size']):
            raise RuntimeError('Invalid media publication descriptor')
    elif kind == 'lyric' and value['size'] > 2 * 1024 * 1024:
        raise RuntimeError('Invalid plain media publication size')
    return value


def create(root, relative, staged, payload, encryption, *, size=None, digest=None, reservation=None):
    info = staged.stat()
    publication = {'version': 1, 'publication_id': secrets.token_hex(16),
        'object_id': secrets.token_hex(32), 'path': relative,
        'kind': 'lyric' if relative.startswith('lyrics/') else 'audio' if relative.startswith('music/') else 'video',
        'action': 'upload_lyric' if relative.startswith('lyrics/') else 'upload_item',
        'size': len(payload) if size is None else size,
        'sha256': hashlib.sha256(payload).hexdigest() if digest is None else digest,
        'encryption': encryption.to_dict() if encryption else None,
        'device': info.st_dev, 'inode': info.st_ino}
    if reservation is not None:
        publication.update(version=2, action='upload-finalized', object_id=reservation['media_id'],
            upload_id=reservation['upload_id'], storage_member_id=reservation['storage_member_id'])
    publication = validate(publication)
    destination = journal_path(root, publication)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(json.dumps(publication, ensure_ascii=True, separators=(',', ':')).encode())
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(destination.parent)
    return publication


async def write_audit(conn, publication, audit=None):
    from datetime import datetime, timezone
    detail = {'path': publication['path'], 'publication_id': publication['publication_id']}
    if publication['version'] == 2:
        detail.update(upload_id=publication['upload_id'], storage_member_id=publication['storage_member_id'])
    if audit is not None:
        await audit(conn, 'success', 1, detail)
    else:
        await conn.execute(text('''INSERT INTO admin_audit_log
            (action,target_count,source_summary,result,detail,created_at)
            VALUES (:action,1,:source,'success',:detail,:now)'''),
            {'action': publication['action'], 'source': json.dumps([publication['path']]),
             'detail': json.dumps(detail), 'now': datetime.now(timezone.utc).replace(tzinfo=None)})


def validate_reservation(publication, row):
    validate(publication)
    expected = {'upload_id': row['upload_id'], 'object_id': row['media_id'],
        'storage_member_id': row['storage_member_id'], 'path': row['media_path'],
        'kind': row['object_kind'], 'size': int(row['expected_bytes'])}
    if (publication['version'] != 2 or any(publication[key] != value for key, value in expected.items())
            or bool(publication['encryption']) != bool(row.get('content_encrypted', 0))):
        raise RuntimeError('MasterLocal publication reservation changed')


async def register(database, publication, audit=None):
    from datetime import datetime, timezone
    from app.services import media_crypto
    body_complete = False
    try:
        async with database.begin() as conn:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            await conn.execute(text('''INSERT INTO media_objects
                (media_id,object_kind,media_path,path_locator,created_at,updated_at)
                VALUES (:id,:kind,:path,:locator,:now,:now)'''),
                {'id': publication['object_id'], 'kind': publication['kind'], 'path': publication['path'],
                 'locator': hashlib.sha256(publication['path'].encode()).hexdigest(), 'now': now})
            descriptor = media_crypto.Metadata.from_dict(publication['encryption']) if publication['encryption'] else None
            await media_crypto.put_encryption(conn, publication['object_id'], descriptor)
            await write_audit(conn, publication, audit)
            body_complete = True
    except BaseException as exc:
        if body_complete:
            raise PublicationCommitUnknown('Media SQL commit acknowledgement unavailable') from exc
        raise


def finish(root, publication):
    destination = journal_path(root, publication)
    destination.unlink()
    sync_directory(destination.parent)


def load(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_size > 8192
            or os.name != 'nt' and stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeError('Unsafe private media publication journal')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        if not os.path.samestat(info, os.fstat(stream.fileno())):
            raise RuntimeError('Media publication journal changed')
        value = validate(json.loads(stream.read(8193)))
    if path.name != PREFIX + value['publication_id'] + '.json':
        raise RuntimeError('Media publication journal identity mismatch')
    return value


def physical_proof(root, publication):
    from app.services.media_manager import resolve_safe_path
    destination = resolve_safe_path(Path(root), publication['path'])
    exists, original_inode = file_proof(destination, publication)
    return destination, exists, original_inode


def file_proof(destination, publication):
    try:
        info = destination.lstat()
    except FileNotFoundError:
        return False, False
    if not stat.S_ISREG(info.st_mode) or info.st_size != publication['size']:
        raise RuntimeError('Media publication bytes do not match recovery proof')
    descriptor = os.open(destination, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    digest = hashlib.sha256()
    with os.fdopen(descriptor, 'rb') as stream:
        if not os.path.samestat(info, os.fstat(stream.fileno())):
            raise RuntimeError('Media publication inode changed')
        while chunk := stream.read(65536):
            digest.update(chunk)
    if digest.hexdigest() != publication['sha256']:
        raise RuntimeError('Media publication checksum mismatch')
    return True, (info.st_dev, info.st_ino) == (publication['device'], publication['inode'])


def discard_staged(root, publication):
    staged = Path(root) / ('.cluster-upload-' + publication['upload_id'] + '.part')
    exists, original_inode = file_proof(staged, publication)
    if exists:
        if not original_inode:
            raise RuntimeError('Staged MasterLocal publication identity changed')
        staged.unlink()
        sync_directory(Path(root))


async def recover_master_local(root, database, publication, destination, exists, original_inode):
    """Prove one atomic catalog/quota/audit commit, or release its reservation."""
    from app.services import resource_pool, media_crypto
    from app.services.upload_claims import claim
    locator = hashlib.sha256(publication['path'].encode()).hexdigest()
    with claim(publication['upload_id']):
        async with database.connect() as conn:
            parameters = {'id': publication['object_id'], 'locator': locator,
                'upload': publication['upload_id'], 'member': publication['storage_member_id']}
            objects = (await conn.execute(text('''SELECT media_id,object_kind,media_path FROM media_objects
                WHERE media_id=:id OR path_locator=:locator'''), parameters)).mappings().all()
            global_rows = (await conn.execute(text('''SELECT media_id,storage_member_id,object_id,media_path,
                path_locator,object_kind,size_bytes,etag,state FROM global_media_objects
                WHERE media_id=:id OR path_locator=:locator'''), parameters)).mappings().all()
            row = (await conn.execute(text('SELECT * FROM cluster_upload_sessions WHERE upload_id=:upload'), parameters)).mappings().first()
            descriptor = await media_crypto.encryption_for_id(conn, publication['object_id'])
            audits = (await conn.execute(text('''SELECT detail FROM admin_audit_log
                WHERE action='upload-finalized' AND result='success' AND detail LIKE :needle LIMIT 2'''),
                {'needle': '%' + publication['publication_id'] + '%'})).scalars().all()
            member = (await conn.execute(text('''SELECT member_kind,used_bytes,reserved_bytes FROM cluster_storage_members
                WHERE member_id=:member'''), parameters)).mappings().first()
            node = (await conn.execute(text('SELECT node_id,role FROM node_identity WHERE singleton=1'))).mappings().first()
        member_matches = bool(member and member['member_kind'] == 'MasterLocal'
            and node and node['role'] == 'Master' and node['node_id'] == publication['storage_member_id'])
        descriptor_matches = (descriptor.to_dict() if descriptor else None) == publication['encryption']
        expected_object = {'media_id': publication['object_id'], 'object_kind': publication['kind'], 'media_path': publication['path']}
        expected_global = dict(expected_object, storage_member_id=publication['storage_member_id'],
            object_id=publication['object_id'], path_locator=locator, size_bytes=publication['size'],
            etag='"' + publication['sha256'] + '"', state='active')
        detail = json.loads(audits[0]) if len(audits) == 1 else {}
        audit_matches = (len(audits) == 1 and all(detail.get(key) == publication[key]
            for key in ('publication_id', 'path', 'upload_id', 'storage_member_id')))
        if row is not None:
            validate_reservation(publication, row)
        if (row and row['state'] == 'complete' and row['path_locator'] is None and member_matches
                and int(member['used_bytes']) >= publication['size'] and int(member['reserved_bytes']) >= 0
                and len(objects) == 1 and dict(objects[0]) == expected_object
                and len(global_rows) == 1 and dict(global_rows[0]) == expected_global
                and descriptor_matches and audit_matches and exists):
            discard_staged(root, publication)
            finish(root, publication)
            return
        empty_registration = not objects and not global_rows and not audits
        reserved = (row and row['state'] == 'reserved' and row['path_locator'] == locator
            and member_matches and int(member['reserved_bytes']) >= publication['size'] and descriptor_matches)
        released = row is None and descriptor is None
        if empty_registration and (reserved or released) and (not exists or original_inode):
            discard_staged(root, publication)
            if exists:
                destination.unlink()
                sync_hierarchy(root, destination.parent)
            if reserved:
                await resource_pool.fail_upload(publication['upload_id'], database)
            finish(root, publication)
            return
        raise RuntimeError('MasterLocal commit is uncertain; retain publication journal and bytes')


async def recover(root, database):
    """Caller owns the runtime writer lease and media exclusive fence."""
    files = pending(root)
    if len(files) > 256:
        raise RuntimeError('Too many pending media publications')
    for path in files:
        if not NAME.fullmatch(path.name):
            raise RuntimeError('Unrecognized media publication recovery entry')
        publication = load(path)
        destination, exists, original_inode = physical_proof(root, publication)
        if publication['version'] == 2:
            await recover_master_local(root, database, publication, destination, exists, original_inode)
            continue
        async with database.connect() as conn:
            objects = (await conn.execute(text('''SELECT media_id, object_kind, media_path FROM media_objects
                WHERE media_id=:id OR path_locator=:locator'''),
                {'id': publication['object_id'], 'locator': hashlib.sha256(publication['path'].encode()).hexdigest()})).mappings().all()
            descriptors = (await conn.execute(text('''SELECT descriptor_json FROM media_encryption
                WHERE object_kind='media' AND object_id=:id'''), {'id': publication['object_id']})).scalars().all()
            audits = (await conn.execute(text('''SELECT detail FROM admin_audit_log
                WHERE action=:action AND result='success' AND detail LIKE :needle LIMIT 2'''),
                {'action': publication['action'], 'needle': '%' + publication['publication_id'] + '%'})).scalars().all()
        expected = {'media_id': publication['object_id'], 'object_kind': publication['kind'], 'media_path': publication['path']}
        object_matches = len(objects) == 1 and dict(objects[0]) == expected
        descriptor_matches = (not descriptors if publication['encryption'] is None else
            len(descriptors) == 1 and descriptors[0] is not None and json.loads(descriptors[0]) == publication['encryption'])
        audit_matches = len(audits) == 1 and json.loads(audits[0]).get('publication_id') == publication['publication_id']
        if object_matches and descriptor_matches and audit_matches and exists:
            finish(root, publication)
            continue
        if not objects and not descriptors and not audits and (not exists or original_inode):
            if exists:
                destination.unlink()
                sync_directory(destination.parent)
            finish(root, publication)
            continue
        raise RuntimeError('Media publication commit is uncertain; retain journal and bytes for recovery')
