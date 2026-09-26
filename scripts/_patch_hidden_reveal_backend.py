from pathlib import Path
import re

path = Path("app/api/v1/media.py")
text = path.read_text(encoding="utf-8")


def one(old: str, new: str, label: str) -> None:
    global text
    if text.count(old) != 1:
        raise SystemExit(f"{label}: expected one match, found {text.count(old)}")
    text = text.replace(old, new, 1)


def regex(pattern: str, replacement: str, label: str) -> None:
    global text
    text, count = re.subn(pattern, replacement, text, count=1, flags=re.S | re.M)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, found {count}")


one(
    'NO_STORE_HEADERS = {\n    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",\n    "Pragma": "no-cache",\n    "Expires": "0",\n}\n',
    'NO_STORE_HEADERS = {\n    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",\n    "Pragma": "no-cache",\n    "Expires": "0",\n}\nHIDDEN_REVEAL_COOKIES = {"music": "frontier_hidden_music", "video": "frontier_hidden_video"}\n',
    "cookie constants",
)
one(
    'async def _hidden_set() -> set[str]:\n    from app.services.media_manager import MediaManager\n    return await MediaManager.hidden_paths()\n',
    'async def _hidden_set() -> set[str]:\n    from app.services.media_manager import MediaManager\n    return await MediaManager.hidden_paths()\n\n\ndef _hidden_reveal_enabled(request: Request | None, media_type: str) -> bool:\n    cookie_name = HIDDEN_REVEAL_COOKIES.get(media_type)\n    return bool(request is not None and cookie_name and request.cookies.get(cookie_name) == "1")\n',
    "cookie helper",
)

regex(
    r'async def get_media_categories\(media_type, valid_exts\):\n.*?\n    return categories\n',
    '''async def get_media_categories(media_type, valid_exts, *, include_hidden: bool = False):
    identity = f"{media_type}:revealed" if include_hidden else media_type
    generation, cached = await load_media_catalog("categories", identity)
    if cached is not None:
        return cached
    hidden = set() if include_hidden else await _hidden_set()
    if node_state.node["role"] == "Master":
        merged = {}
        for entry in await node_catalog.resources(root=_typed_media_root(media_type).name):
            if _is_publicly_hidden(entry["path"], hidden):
                continue
            name = entry["path"].split("/")[1]
            merged.setdefault(name, {"name": name, "url": _category_url(media_type, _typed_media_root(media_type).name + "/" + name)})
        categories = sorted(merged.values(), key=lambda entry: entry["name"].casefold())
    else:
        categories = await asyncio.to_thread(_get_media_categories_sync, media_type, valid_exts, hidden)
    await store_media_catalog(generation, "categories", identity, categories)
    return categories
''',
    "categories",
)
regex(
    r'@router\.get\("/catalog/categories"\)\nasync def get_media_categories_data\(.*?\n    \)\n',
    '''@router.post("/catalog/reveal")
async def reveal_hidden_catalog(
    request: Request,
    media_type: str = Query(..., pattern=r"^(music|video)$"),
):
    if request.headers.get("x-frontier-hidden-reveal") != "1":
        raise HTTPException(status_code=400, detail="Invalid hidden catalog reveal request")
    response = JSONResponse({"status": "ok", "media_type": media_type}, headers=NO_STORE_HEADERS)
    response.set_cookie(
        key=HIDDEN_REVEAL_COOKIES[media_type], value="1", path="/api/v1/media",
        httponly=True, samesite="lax",
    )
    return response


@router.get("/catalog/categories")
async def get_media_categories_data(
    request: Request,
    media_type: str = Query(..., pattern=r"^(music|video)$"),
):
    valid_exts = AUDIO_EXTS if media_type == "music" else VIDEO_EXTS
    include_hidden = _hidden_reveal_enabled(request, media_type)
    entries = await get_media_categories(media_type, valid_exts, include_hidden=include_hidden)
    headers = ({**NO_STORE_HEADERS, "X-Frontier-Hidden-Reveal": "1"}
               if include_hidden else {"Cache-Control": "private, max-age=30, stale-while-revalidate=300"})
    return JSONResponse({"entries": entries}, headers=headers)
''',
    "categories endpoint",
)
regex(
    r'async def get_media_subcategories\(media_type, category_subpath, valid_exts\):\n.*?\n    return subcategories\n',
    '''async def get_media_subcategories(media_type, category_subpath, valid_exts, *, include_hidden: bool = False):
    identity = f"{media_type}:{category_subpath}:{'revealed' if include_hidden else 'public'}"
    generation, cached = await load_media_catalog("subcategories", identity)
    if cached is not None:
        return cached
    hidden = set() if include_hidden else await _hidden_set()
    if node_state.node["role"] == "Master":
        merged = {}
        for entry in await node_catalog.resources(directory=category_subpath):
            if _is_publicly_hidden(entry["path"], hidden):
                continue
            parts = entry["path"].split("/")
            if len(parts) == 4:
                name = parts[2]
                merged.setdefault(name, {"name": name, "url": _category_url(media_type, category_subpath + "/" + name)})
        subcategories = sorted(merged.values(), key=lambda entry: entry["name"].casefold())
    else:
        subcategories = await asyncio.to_thread(_get_media_subcategories_sync, media_type, category_subpath, valid_exts, hidden)
    await store_media_catalog(generation, "subcategories", identity, subcategories)
    return subcategories
''',
    "subcategories",
)
regex(
    r'async def scan_media_files_by_category\(category_subpath, valid_exts, media_type\):\n.*?\n    return media_list\n',
    '''async def scan_media_files_by_category(category_subpath, valid_exts, media_type, *, include_hidden: bool = False):
    identity = f"{media_type}:{category_subpath}:{'revealed' if include_hidden else 'public'}"
    generation, cached = await load_media_catalog("tracks-v2", identity)
    if cached is not None:
        return cached
    hidden = set() if include_hidden else await _hidden_set()
    if node_state.node["role"] == "Master":
        media_list = [item for item in await node_routing.directory_items(category_subpath)
                      if not _is_publicly_hidden(item["media_path"], hidden)]
    else:
        async with media_mutation_lock.shared():
            ensure_media_mutations_ready()
            media_list = await asyncio.to_thread(_scan_media_files_by_category_sync, category_subpath, valid_exts, media_type, hidden)
            media_list = await media_objects.bind_items(media_list, media_type)
    await store_media_catalog(generation, "tracks-v2", identity, media_list)
    return media_list
''',
    "media scan",
)
one(
    'async def _player_entries(\n    path: str,\n    media_type: str,\n    playback_session_id: str,\n) -> list[dict]:\n',
    'async def _player_entries(\n    path: str,\n    media_type: str,\n    playback_session_id: str,\n    *,\n    include_hidden: bool = False,\n) -> list[dict]:\n',
    "player signature",
)
one(
    '    items = await scan_media_files_by_category(path, valid_exts, playback_type)\n',
    '    items = await scan_media_files_by_category(path, valid_exts, playback_type, include_hidden=include_hidden)\n',
    "player scan",
)
regex(
    r'@router\.get\("/catalog/media"\)\nasync def get_media_catalog_data\(.*?\n    \)\n',
    '''@router.get("/catalog/media")
async def get_media_catalog_data(
    request: Request,
    media_type: str = Query(..., pattern=r"^(music|video)$"),
    path: str = Query(..., min_length=1, max_length=1024),
    playback_session_id: str = Query(..., min_length=1, max_length=64),
):
    await _public_category_parts(path, media_type)
    include_hidden = _hidden_reveal_enabled(request, media_type)
    entries = await _player_entries(path, media_type, playback_session_id, include_hidden=include_hidden)
    headers = ({**NO_STORE_HEADERS, "X-Frontier-Hidden-Reveal": "1"}
               if include_hidden else {"Cache-Control": "private, max-age=15, stale-while-revalidate=120"})
    return JSONResponse({"entries": entries}, headers=headers)
''',
    "media endpoint",
)
one(
    'async def _local_stream_metadata(relative_path: str, object_kind: str) -> dict[str, str]:\n',
    'async def _local_stream_metadata(relative_path: str, object_kind: str, *, allow_hidden: bool = False) -> dict[str, str]:\n',
    "stream metadata signature",
)
one(
    '        if hidden:\n            raise HTTPException(status_code=404, detail="Media file not found")\n',
    '        if hidden and not allow_hidden:\n            raise HTTPException(status_code=404, detail="Media file not found")\n',
    "stream hidden guard",
)
one(
    '    metadata = await _local_stream_metadata(rel, "audio" if rel_parts[0] == "music" else "video")\n',
    '    reveal_type = "music" if rel_parts[0] == "music" else "video"\n    metadata = await _local_stream_metadata(rel, "audio" if rel_parts[0] == "music" else "video", allow_hidden=_hidden_reveal_enabled(request, reveal_type))\n',
    "stream reveal",
)

old = '''async def _get_player_or_subcategories(
    path: str,
    media_type: str,
    playback_type: str,
    valid_exts,
    player_template: str,
    title_prefix: str,
    direct: bool = False,
) -> HTMLResponse:
    parts = await _public_category_parts(path, media_type)
'''
new = '''async def _get_player_or_subcategories(
    path: str,
    media_type: str,
    playback_type: str,
    valid_exts,
    player_template: str,
    title_prefix: str,
    request: Request,
    direct: bool = False,
) -> HTMLResponse:
    include_hidden = _hidden_reveal_enabled(request, media_type)
    parts = await _public_category_parts(path, media_type)
'''
one(old, new, "player page signature")
one('        subcategories = await get_media_subcategories(media_type, path, valid_exts)\n', '        subcategories = await get_media_subcategories(media_type, path, valid_exts, include_hidden=include_hidden)\n', "subcategories call")
one('            if node_state.node["role"] == "Master" and await scan_media_files_by_category(path, valid_exts, playback_type):\n', '            if node_state.node["role"] == "Master" and await scan_media_files_by_category(path, valid_exts, playback_type, include_hidden=include_hidden):\n', "direct media call")
one('        "cacheKey": f"media:{media_type}:{path}",\n', '        "cacheKey": f"media:{media_type}:{path}:{\'revealed\' if include_hidden else \'public\'}",\n', "cache variant")
one('async def get_music_player_page(\n    path: str = Query(...),\n', 'async def get_music_player_page(\n    request: Request,\n    path: str = Query(...),\n', "music request")
one('        "audio-player.html",\n        "前沿音乐",\n        direct,\n', '        "audio-player.html",\n        "前沿音乐",\n        request,\n        direct,\n', "music request pass")
one('async def get_video_player_page(\n    path: str = Query(...),\n', 'async def get_video_player_page(\n    request: Request,\n    path: str = Query(...),\n', "video request")
one('        "video-player.html",\n        "前沿视讯",\n        direct,\n', '        "video-player.html",\n        "前沿视讯",\n        request,\n        direct,\n', "video request pass")

old_guard = '            if _is_publicly_hidden(normalized_track, await _hidden_set()):\n                raise FileNotFoundError\n'
new_guard = '            if _is_publicly_hidden(normalized_track, await _hidden_set()) and not _hidden_reveal_enabled(request, "music"):\n                raise FileNotFoundError\n'
if text.count(old_guard) != 2:
    raise SystemExit(f"lyric guard: expected two matches, found {text.count(old_guard)}")
text = text.replace(old_guard, new_guard)

path.write_text(text, encoding="utf-8")
