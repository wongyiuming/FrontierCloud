"""Optional original-quality artwork, using current public media authorization."""
import asyncio
import hashlib
from itertools import islice
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from app.services import music_tags


router = APIRouter()
ARTWORK_CANDIDATES = 8
ARTWORK_DIRECTORIES = 8
ARTWORK_TIMEOUT_SECONDS = 2.0


def category_artwork(entries: list[dict], media_type: str, include_hidden: bool) -> list[dict]:
    result = [dict(entry) for entry in entries]
    if media_type != 'music':
        return result
    for entry in result:
        value = parse_qs(urlsplit(entry.get('url', '')).query).get('path', [''])[0]
        query = {'path': value}
        if include_hidden:
            query['include_hidden'] = 'true'
        entry['artwork_url'] = '/api/v1/media/catalog/artwork?' + urlencode(query)
    return result


async def _artwork(path: str, include_hidden: bool) -> dict:
    from app.api.v1 import media
    result = {'cover': '', 'metadata_tracks': []}
    remaining = ARTWORK_CANDIDATES

    async def collect(directory):
        nonlocal remaining
        tracks = await media.scan_media_files_by_category(directory, media.AUDIO_EXTS, 'audio', include_hidden=include_hidden)
        # Spend the shared budget only on metadata-capable MP3 candidates.
        found = False
        for entry in tracks:
            if not entry.get('media_path', '').lower().endswith('.mp3'):
                continue
            entry = music_tags.lazy_tracks([dict(entry)])[0]
            if entry.get('encryption') or entry.get('browser_metadata') or entry.get('native_metadata'):
                result['metadata_tracks'].append(entry)
                found = True
                remaining -= 1
                if not remaining:
                    break
        return found

    try:
        async with asyncio.timeout(ARTWORK_TIMEOUT_SECONDS):
            if not await collect(path) and len(path.split('/')) == 2:
                children = await media.get_media_subcategories('music', path, media.AUDIO_EXTS, include_hidden=include_hidden)
                for child in islice(children, ARTWORK_DIRECTORIES - 1):
                    if not remaining:
                        break
                    await collect(path + '/' + child['name'])
    except TimeoutError:
        # Artwork is optional; cancellation does not turn a directory into an error.
        pass
    return result


@router.get('/catalog/artwork')
async def directory_artwork(path: str = Query(..., min_length=1, max_length=1024), include_hidden: bool = False):
    from app.api.v1 import media
    await media._public_category_parts(path, 'music')
    return JSONResponse(await _artwork(path, include_hidden), headers={'Cache-Control': 'private, no-store'})


@router.get('/metadata')
async def track_metadata(file_path: str = Query(..., min_length=1, max_length=4096),
                         resource_id: str = Query('', max_length=64)):
    try:
        tags = await music_tags.music_metadata(file_path, resource_id)
    except ValueError as exc:
        raise HTTPException(404, 'Music metadata not found') from exc
    value = {field: getattr(tags, field)[:4096] for field in ('title', 'artist', 'album') if getattr(tags, field)}
    if tags.picture:
        value['cover'] = '/api/v1/media/cover?' + urlencode({'file_path': file_path, 'resource_id': resource_id})
    return JSONResponse(value, headers={'Cache-Control': 'private, no-store'})


@router.api_route('/cover', methods=['GET', 'HEAD'])
async def music_cover(request: Request, file_path: str = Query(..., min_length=1, max_length=4096),
                      resource_id: str = Query('', max_length=64)):
    try:
        tags = await music_tags.music_metadata(file_path, resource_id)
    except ValueError as exc:
        raise HTTPException(404, 'Album cover not found') from exc
    if not tags.picture:
        raise HTTPException(404, 'Album cover not found')
    etag = '"cover-' + hashlib.sha256(tags.picture).hexdigest() + '"'
    headers = {'Cache-Control': 'private, max-age=300', 'ETag': etag}
    if any(value.strip().removeprefix('W/') in ('*', etag)
           for value in request.headers.get('if-none-match', '').split(',')):
        return Response(status_code=304, headers=headers)
    headers['Content-Length'] = str(len(tags.picture))
    return Response(b'' if request.method == 'HEAD' else tags.picture, media_type=tags.mime, headers=headers)
