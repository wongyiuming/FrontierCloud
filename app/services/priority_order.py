"""Whole-scope drag ordering committed with its audit in one transaction."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import PurePosixPath

from sqlalchemy import select, text

from app.core.db import engine
from app.services import media_objects, priority_planner as planner
from app.services.federation import schema as s
from app.services.media_catalog_cache import invalidate_media_catalog


class StaleOrder(ValueError):
    pass


def scope_path(value: str) -> str:
    if value == '':
        return value
    parts = PurePosixPath(value).parts
    if (value != '/'.join(parts) or not parts or parts[0] not in {'music', 'vido'}
            or len(parts) > 3 or any(p.startswith('.') or '\\' in p or '\x00' in p for p in parts)):
        raise planner.PriorityMoveError('Invalid immediate media scope')
    return value


def filesystem_children(root, scope: str, kind: str) -> list[dict]:
    if scope == '':
        return [{'path': name} for name in ('music', 'vido') if (root / name).is_dir()] if kind == 'directory' else []
    directory = root / scope
    # Reject symlinks in any ancestor, rather than following a path out of media.
    current = root
    for part in scope.split('/'):
        current /= part
        if current.is_symlink():
            raise planner.PriorityMoveError('Symbolic media directories are unsupported')
    if not directory.is_dir():
        return []
    allowed = {'.mp3', '.m4a', '.flac', '.wav'} if scope.startswith('music') else {'.mp4', '.webm', '.mkv'}
    rows = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name.startswith('.') or entry.is_symlink():
                continue
            if kind == 'directory' and len(scope.split('/')) < 3 and entry.is_dir(follow_symlinks=False):
                rows.append({'path': scope + '/' + entry.name})
            elif kind == 'media' and entry.is_file(follow_symlinks=False) and PurePosixPath(entry.name).suffix.lower() in allowed:
                rows.append({'path': scope + '/' + entry.name})
    return rows


async def _items(conn, scope, kind, role, root):
    prefix = scope + '/' if scope else ''
    if role == 'Master':
        rows = (await conn.execute(select(s.global_media).where(s.global_media.c.state == 'active'))).mappings()
        children = {}
        for row in rows:
            path = row['media_path']
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            if kind == 'directory' and '/' in rest:
                path = prefix + rest.split('/', 1)[0]
                children.setdefault(path, {'path': path})
            elif kind == 'media' and '/' not in rest:
                children[path] = {'path': path, 'media_id': row['media_id']}
        raw = list(children.values())
    else:
        raw = filesystem_children(root, scope, kind)
    # Include hidden siblings and all pages; a filtered UI cannot define ordering.
    registered = {row['media_path']: row['media_id'] for row in
                  (await conn.execute(text('SELECT media_id, media_path FROM media_objects'))).mappings()}
    stats = {row['media_id']: int(row['preference']) for row in
             (await conn.execute(text('SELECT media_id, preference FROM media_playback_stats'))).mappings()}
    for item in raw:
        item.setdefault('media_id', registered.get(item['path'], ''))
        item['preference'] = stats.get(item['media_id'], 0)
    return planner.ordered(raw, kind)


async def order(scope, kind, *, database=engine, root=None):
    scope = scope_path(scope)
    if kind not in {'media', 'directory'}:
        raise planner.PriorityMoveError('Invalid priority kind')
    from app.services.media_manager import MEDIA_ROOT, media_mutation_lock, ensure_media_mutations_ready
    root = root or MEDIA_ROOT
    async with media_mutation_lock.shared():
        ensure_media_mutations_ready()
        async with database.connect() as conn:
            role = await conn.scalar(select(s.identity.c.role).where(s.identity.c.singleton == 1))
            if role not in {'Standalone', 'Master'}:
                raise planner.PriorityMoveError('Storage nodes cannot change business priorities')
            items = await _items(conn, scope, kind, role, root)
    return {'scope': scope, 'kind': kind, 'items': items, 'revision': planner.revision(scope, kind, items),
            'minimum': planner.MINIMUM, 'maximum': planner.MAXIMUM}


async def reorder(scope, kind, revision, path, before, after, audit, *, database=engine, root=None):
    scope = scope_path(scope)
    if kind not in {'media', 'directory'}:
        raise planner.PriorityMoveError('Invalid priority kind')
    from app.services.media_manager import MEDIA_ROOT, media_mutation_lock, ensure_media_mutations_ready
    root = root or MEDIA_ROOT
    async with media_mutation_lock:
        ensure_media_mutations_ready()
        async with database.begin() as conn:
            role = await conn.scalar(select(s.identity.c.role).where(s.identity.c.singleton == 1).with_for_update())
            if role not in {'Standalone', 'Master'}:
                raise planner.PriorityMoveError('Storage nodes cannot change business priorities')
            # Reject physical mutations whose metadata is still in flight.
            pending = list((await conn.execute(select(s.upload_sessions.c.media_path).where(
                s.upload_sessions.c.state == 'reserved'))).scalars())
            pending += list((await conn.execute(select(s.global_media.c.media_path).where(
                s.global_media.c.state != 'active'))).scalars())
            if any(not scope or p.startswith(scope + '/') for p in pending):
                raise StaleOrder('Media mutation in this scope is still in progress')
            old = await _items(conn, scope, kind, role, root)
            if planner.revision(scope, kind, old) != revision:
                raise StaleOrder('Scope changed; refresh the full sibling order')
            planned = planner.move(old, path, before, after, kind)
            previous = {v['path']: v for v in old}
            changed = [v for v in planned if v['preference'] != previous[v['path']]['preference']]
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            for item in changed:
                identifier = item['media_id']
                if not identifier:
                    if kind == 'directory':
                        identifier = secrets.token_hex(32)
                        await conn.execute(text('''INSERT INTO media_objects
                            (media_id,object_kind,media_path,path_locator,created_at,updated_at)
                            VALUES (:id,'directory',:path,:locator,:now,:now)'''),
                            {'id': identifier, 'path': item['path'], 'locator': hashlib.sha256(item['path'].encode()).hexdigest(), 'now': now})
                    else:
                        identifier = await media_objects.ensure_object(conn, item['path'], 'audio' if scope.startswith('music') else 'video')
                    item['media_id'] = identifier
                await conn.execute(text('''INSERT INTO media_playback_stats
                    (media_id,media_path,play_score,preference,created_at,updated_at)
                    VALUES (:id,:path,0,:value,:now,:now)
                    ON DUPLICATE KEY UPDATE preference=VALUES(preference),updated_at=VALUES(updated_at)'''),
                    {'id': identifier, 'path': item['path'], 'value': item['preference'], 'now': now})
            changes = [{'path': v['path'], 'old': previous[v['path']]['preference'], 'new': v['preference']} for v in changed]
            batches, batch = [], []
            for change in changes:
                if batch and len(json.dumps(batch + [change], ensure_ascii=False).encode()) > 7000:
                    batches.append(batch)
                    batch = []
                batch.append(change)
            batches.append(batch)
            for batch in batches:
                await audit(conn, 'success', len(batch), {'scope': scope, 'kind': kind,
                    'moved_path': path, 'changes': batch,
                    'manifest': [{'relative_path': v['path']} for v in batch]})
            final = await _items(conn, scope, kind, role, root)
            if [v['path'] for v in final] != [v['path'] for v in planned]:
                raise RuntimeError('Committed integer priorities do not represent the requested order')
    await invalidate_media_catalog()
    return {'scope': scope, 'kind': kind, 'items': final, 'revision': planner.revision(scope, kind, final),
            'changed': len(changed), 'minimum': planner.MINIMUM, 'maximum': planner.MAXIMUM}
