"""Optional original-quality artwork, using current public media authorization."""
import hashlib
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from app.services import music_tags


router = APIRouter()


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
    tracks = await media.scan_media_files_by_category(path, media.AUDIO_EXTS, 'audio', include_hidden=include_hidden)
    await music_tags.decorate_tracks(tracks)
    result = {'cover': '', 'metadata_tracks': []}
    for entry in tracks:
        if str(entry.get('cover', '')).startswith('/api/v1/media/cover?'):
            return {'cover': entry['cover'], 'metadata_tracks': []}
        if (entry.get('encryption') or entry.get('browser_metadata')) and entry.get('media_path', '').lower().endswith('.mp3') and len(result['metadata_tracks']) < 8:
            result['metadata_tracks'].append(entry)
    if not tracks and len(path.split('/')) == 2:
        children = await media.get_media_subcategories('music', path, media.AUDIO_EXTS, include_hidden=include_hidden)
        for child in children:
            candidate = await _artwork(path + '/' + child['name'], include_hidden)
            if candidate['cover']:
                return candidate
            remaining = 8 - len(result['metadata_tracks'])
            result['metadata_tracks'].extend(candidate['metadata_tracks'][:remaining])
    return result


@router.get('/catalog/artwork')
async def directory_artwork(path: str = Query(..., min_length=1, max_length=1024), include_hidden: bool = False):
    from app.api.v1 import media
    await media._public_category_parts(path, 'music')
    return JSONResponse(await _artwork(path, include_hidden), headers={'Cache-Control': 'private, no-store'})


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
