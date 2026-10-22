"""Bounded original MP3 metadata reads; encrypted tags remain browser-only."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import io
import os
from pathlib import Path
import ssl
import threading
import time
import weakref

from fastapi import HTTPException
import httpx


TAG_LIMIT = 8 * 1024 * 1024
PICTURE_LIMIT = 4 * 1024 * 1024
CACHE_LIMIT = 32 * 1024 * 1024
FRAME_LIMIT = 4096


@dataclass(frozen=True)
class Tags:
    title: str = ''
    artist: str = ''
    album: str = ''
    picture: bytes = b''
    mime: str = ''


def sync_safe(value: bytes) -> int:
    if len(value) != 4 or any(byte & 128 for byte in value):
        return -1
    return sum(byte << shift for byte, shift in zip(value, (21, 14, 7, 0)))


def _text(value: bytes) -> str:
    if not value or value[0] > 3:
        return ''
    encoding, raw = value[0], value[1:]
    if encoding in (1, 2):
        codec = 'utf-16-be'
        if raw.startswith(b'\xff\xfe'):
            codec, raw = 'utf-16-le', raw[2:]
        elif raw.startswith(b'\xfe\xff'):
            raw = raw[2:]
        decoded = raw.decode(codec, errors='replace')
    else:
        decoded = raw.decode('latin-1' if encoding == 0 else 'utf-8', errors='replace')
    decoded = decoded.split('\0', 1)[0].strip()
    return decoded if len(decoded.encode('utf-8')) <= 4096 else ''


def _picture(frame: bytes, old: bool) -> tuple[bytes, str, int]:
    if len(frame) < 5 or frame[0] > 3:
        return b'', '', 0
    encoding = frame[0]
    end = 4 if old else frame.find(b'\0', 1) + 1
    if end <= 0 or end >= len(frame):
        return b'', '', 0
    kind, start = frame[end], end + 1
    if encoding in (1, 2):
        end = next((i + 2 for i in range(start, len(frame) - 1, 2)
                    if frame[i:i+2] == b'\0\0'), -1)
    else:
        found = frame.find(b'\0', start)
        end = found + 1 if found >= 0 else -1
    if end < 0:
        return b'', '', 0
    image = frame[end:]
    if not 12 <= len(image) <= PICTURE_LIMIT:
        return b'', '', 0
    mime = ''
    if image.startswith(b'\xff\xd8\xff'):
        mime = 'image/jpeg'
    elif image.startswith(b'\x89PNG\r\n\x1a\n'):
        mime = 'image/png'
    elif image[:6] in (b'GIF87a', b'GIF89a'):
        mime = 'image/gif'
    elif image[:4] == b'RIFF' and image[8:12] == b'WEBP':
        mime = 'image/webp'
    return (image, mime, kind) if mime else (b'', '', 0)


def read_tags(reader, size: int | None = None) -> Tags:
    """Read only the bounded ID3 prefix and optional local ID3v1 suffix."""
    header = reader.read(10)
    values = {'title': '', 'artist': '', 'album': '', 'picture': b'', 'mime': ''}
    if len(header) == 10 and header[:3] == b'ID3' and header[3] in (2, 3, 4):
        version, length = header[3], sync_safe(header[6:10])
        if 0 <= length <= TAG_LIMIT and not (version == 2 and header[5] & 64):
            data = reader.read(length)
            if len(data) == length:
                if version < 4 and header[5] & 128:
                    data = data.replace(b'\xff\0', b'\xff')
                if version >= 3 and header[5] & 64:
                    skip = sync_safe(data[:4]) if version == 4 else int.from_bytes(data[:4], 'big') + 4
                    data = data[skip:] if 4 <= skip <= len(data) else b''
                position = 0
                for _ in range(FRAME_LIMIT):
                    width = 6 if version == 2 else 10
                    if len(data) - position < width:
                        break
                    frame_header = data[position:position+width]
                    name = frame_header[:3 if version == 2 else 4]
                    count = (int.from_bytes(frame_header[3:6], 'big') if version == 2 else
                             sync_safe(frame_header[4:8]) if version == 4 else
                             int.from_bytes(frame_header[4:8], 'big'))
                    if count <= 0 or count > len(data) - position - width:
                        break
                    flags = 0 if version == 2 else frame_header[9]
                    start = position + width
                    position = start + count
                    frame = data[start:position]
                    if (version == 3 and flags & 0xe0) or (version == 4 and flags & 0x4c):
                        continue
                    if version == 4 and (header[5] & 128 or flags & 2):
                        frame = frame.replace(b'\xff\0', b'\xff')
                    if version == 4 and flags & 1:
                        if len(frame) < 4 or sync_safe(frame[:4]) != len(frame) - 4:
                            continue
                        frame = frame[4:]
                    field = {b'TIT2': 'title', b'TT2': 'title', b'TPE1': 'artist',
                             b'TP1': 'artist', b'TALB': 'album', b'TAL': 'album'}.get(name)
                    if field:
                        values[field] = _text(frame)
                    elif name in (b'APIC', b'PIC'):
                        image, mime, kind = _picture(frame, version == 2)
                        if image and (not values['picture'] or kind == 3):
                            values.update(picture=image, mime=mime)
    if size is not None and size >= 128 and any(not values[key] for key in ('title', 'artist', 'album')):
        reader.seek(size - 128)
        tail = reader.read(128)
        if len(tail) == 128 and tail[:3] == b'TAG':
            for key, start in (('title', 3), ('artist', 33), ('album', 63)):
                if not values[key]:
                    values[key] = _text(b'\0' + tail[start:start+30])
    return Tags(**values)


_cache: OrderedDict[str, tuple[Tags, int]] = OrderedDict()
_cache_bytes = 0
_lock = threading.Lock()
_gates = weakref.WeakKeyDictionary()


def _cached(key: str) -> Tags | None:
    with _lock:
        value = _cache.get(key)
        if value is not None:
            _cache.move_to_end(key)
            return value[0]
    return None


def _remember(key: str, tags: Tags) -> Tags:
    global _cache_bytes
    size = len(tags.picture) + sum(len(s.encode('utf-8')) for s in (key, tags.title, tags.artist, tags.album)) + 256
    with _lock:
        if key in _cache or size > CACHE_LIMIT:
            return tags
        while _cache and (len(_cache) >= 256 or _cache_bytes + size > CACHE_LIMIT):
            _, (_, cost) = _cache.popitem(last=False)
            _cache_bytes -= cost
        _cache[key] = tags, size
        _cache_bytes += size
    return tags


def _gate():
    loop = asyncio.get_running_loop()
    with _lock:
        if loop not in _gates:
            _gates[loop] = asyncio.BoundedSemaphore(2)
        return _gates[loop]


async def _joined_read(descriptor: int, size: int) -> Tags:
    loop = asyncio.get_running_loop()
    completed = loop.create_future()
    ownership = threading.Lock()
    pending = [os.dup(descriptor)]

    def finish(result, error):
        if not completed.done():
            completed.set_result((result, error))

    def work():
        with ownership:
            owned, pending[0] = pending[0], None
        if owned is None:
            return
        result = error = None
        try:
            with os.fdopen(owned, 'rb') as reader:
                result = read_tags(reader, size)
        except BaseException as exc:
            error = exc
        finally:
            loop.call_soon_threadsafe(finish, result, error)

    def joined(task):
        error = asyncio.CancelledError() if task.cancelled() else task.exception()
        with ownership:
            owned, pending[0] = pending[0], None
        if owned is not None:
            # Cancellation before execution transfers ownership back here. A
            # queued worker that later starts sees no descriptor to read.
            os.close(owned)
            finish(None, error or RuntimeError('Metadata reader did not start'))

    task = asyncio.create_task(asyncio.to_thread(work), name='bounded-music-tag-read')
    task.add_done_callback(joined)
    cancelled = None
    while True:
        try:
            result, error = await asyncio.shield(completed)
            break
        except asyncio.CancelledError as exc:
            cancelled = exc
    if cancelled is not None:
        raise cancelled
    if error is not None:
        raise error
    return result


async def _remote_tags(row: dict, relation: dict) -> Tags:
    from app.services.federation import protocol as p
    from app.services.federation.state import state
    credential = state.unseal(relation['credential'])
    token = p.media_token(credential, relation['relationship_id'], state.node['node_id'],
                          row['owner_id'], row['object_id'], row['resource_id'], int(time.time()))
    url = p.endpoint(relation['peer_endpoint']) + '/internal/v1/media/' + row['object_id']
    size = int(row['payload']['size'])
    expected = {'X-Media-Resource-ID': row['resource_id'], 'X-Media-Owner-ID': row['owner_id'],
                'X-Media-Object-ID': row['object_id']}
    async with httpx.AsyncClient(verify=ssl.create_default_context(), trust_env=False,
                                follow_redirects=False, timeout=httpx.Timeout(10, connect=8)) as client:
        async def part(start, end):
            headers = {'Accept-Encoding': 'identity', 'X-Media-Capability': token,
                       'Range': f'bytes={start}-{end}'}
            async with client.stream('GET', url, headers=headers) as response:
                if (response.status_code != 206 or response.headers.get('Content-Range') != f'bytes {start}-{end}/{size}'
                        or response.headers.get('Content-Encoding', 'identity') != 'identity'
                        or any(response.headers.get(key) != value for key, value in expected.items())):
                    raise ValueError('Music metadata response identity or range mismatch')
                result = bytearray()
                async for chunk in response.aiter_raw():
                    if len(result) + len(chunk) > end - start + 1:
                        raise ValueError('Music metadata range exceeded its bound')
                    result.extend(chunk)
                if len(result) != end - start + 1:
                    raise ValueError('Incomplete music metadata range')
                return bytes(result)
        if size < 10:
            return Tags()
        header = await part(0, 9)
        length = sync_safe(header[6:10]) if header[:3] == b'ID3' else -1
        if 0 < length <= min(TAG_LIMIT, size - 10) and header[3] in (2, 3, 4):
            return read_tags(io.BytesIO(header + await part(10, length + 9)))
        return Tags()


async def music_metadata(path: str, identifier: str = '') -> Tags:
    """Reauthorize before every cache hit; never read or decrypt encrypted media."""
    from app.middleware.native_storage import _open_beneath
    from app.services import media_crypto, media_manager
    from app.services.federation import routing
    from app.services.federation.state import state
    gate = _gate()
    if gate.locked():
        raise HTTPException(503, 'Music metadata is busy', headers={'Retry-After': '2'})
    async with gate, media_manager.media_mutation_lock.shared():
        media_manager.ensure_media_mutations_ready()
        obj = await media_crypto._crypto_object(path, identifier)
        if obj['kind'] != 'audio' or Path(obj['path']).suffix.lower() != '.mp3' or obj['encryption'] is not None:
            return Tags()
        row, relation = (await routing.resolve(obj['resource_id'], obj['path'])
                         if state.node['role'] == 'Master' else (None, None))
        if row is not None and row.get('transport') == 'S3':
            return Tags()
        if relation is not None:
            key = f"remote:{row['resource_id']}:{row['object_id']}:{row['payload']['etag']}:{row['payload']['size']}"
            cached = _cached(key)
            if cached is not None:
                return cached
            result = await _remote_tags(row, relation)
            current = await media_crypto._crypto_object(path, identifier)
            latest, _ = await routing.resolve(obj['resource_id'], obj['path'])
            if current != obj or latest['payload']['etag'] != row['payload']['etag'] or latest['object_id'] != row['object_id']:
                raise HTTPException(409, 'Music metadata source changed')
            return _remember(key, result)
        descriptor, info = _open_beneath(media_manager.MEDIA_ROOT, obj['path'].split('/'))
        try:
            if info.st_size != obj['size_bytes']:
                raise HTTPException(409, 'Music metadata source size changed')
            stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            key = f"local:{obj['media_id']}:{stamp}"
            cached = _cached(key)
            if cached is not None:
                return cached
            result = await _joined_read(descriptor, info.st_size)
            after = os.fstat(descriptor)
            if stamp != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise HTTPException(409, 'Music metadata source changed')
            return _remember(key, result)
        finally:
            os.close(descriptor)


def lazy_tracks(tracks: list[dict]) -> list[dict]:
    """Catalog construction never reads optional tags or attachment bytes."""
    for track in tracks:
        if (track.get('type') == 'audio' and isinstance(track.get('media_path'), str)
                and track['media_path'].lower().endswith('.mp3')
                and not track.get('encryption') and not track.get('encrypted')
                and track.get('browser_metadata') is not True):
            track['native_metadata'] = True
    return tracks
