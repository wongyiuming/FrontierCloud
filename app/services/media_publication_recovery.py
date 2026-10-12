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


def pending(root):
    return sorted(Path(root).parent.glob(PREFIX + '*'))


def journal_path(root, publication):
    return Path(root).parent / (PREFIX + publication['publication_id'] + '.json')


def validate(value):
    from app.services.media_crypto import Metadata
    if not isinstance(value, dict) or set(value) != {
            'version', 'publication_id', 'object_id', 'path', 'kind', 'action', 'size', 'sha256', 'encryption', 'device', 'inode'}:
        raise RuntimeError('Invalid media publication journal')
    parts = str(value['path']).split('/')
    kind, action = ('lyric', 'upload_lyric') if parts[0] == 'lyrics' else ('audio', 'upload_item') if parts[0] == 'music' else ('video', 'upload_item')
    extensions = {'lyric': {'.lrc'}, 'audio': {'.mp3', '.m4a', '.flac', '.wav'}, 'video': {'.mp4', '.webm', '.mkv'}}
    if (type(value['version']) is not int or value['version'] != 1
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
    if value['encryption'] is not None:
        descriptor = Metadata.from_dict(value['encryption'])
        if (kind == 'lyric' and descriptor.plaintext_size > 2 * 1024 * 1024
                or descriptor.ciphertext_size != value['size']):
            raise RuntimeError('Invalid media publication descriptor')
    elif kind == 'lyric' and value['size'] > 2 * 1024 * 1024:
        raise RuntimeError('Invalid plain media publication size')
    return value


def create(root, relative, staged, payload, encryption, *, size=None, digest=None):
    info = staged.stat()
    publication = validate({'version': 1, 'publication_id': secrets.token_hex(16),
        'object_id': secrets.token_hex(32), 'path': relative,
        'kind': 'lyric' if relative.startswith('lyrics/') else 'audio' if relative.startswith('music/') else 'video',
        'action': 'upload_lyric' if relative.startswith('lyrics/') else 'upload_item',
        'size': len(payload) if size is None else size,
        'sha256': hashlib.sha256(payload).hexdigest() if digest is None else digest,
        'encryption': encryption.to_dict() if encryption else None,
        'device': info.st_dev, 'inode': info.st_ino})
    destination = journal_path(root, publication)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(json.dumps(publication, ensure_ascii=True, separators=(',', ':')).encode())
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(destination.parent)
    return publication


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
            detail = {'path': publication['path'], 'publication_id': publication['publication_id']}
            if audit is not None:
                await audit(conn, 'success', 1, detail)
            else:
                await conn.execute(text('''INSERT INTO admin_audit_log
                    (action,target_count,source_summary,result,detail,created_at)
                    VALUES (:action,1,:source,'success',:detail,:now)'''),
                    {'action': publication['action'], 'source': json.dumps([publication['path']]),
                     'detail': json.dumps(detail), 'now': now})
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
    try:
        info = destination.lstat()
    except FileNotFoundError:
        return destination, False, False
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
    return destination, True, (info.st_dev, info.st_ino) == (publication['device'], publication['inode'])


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
