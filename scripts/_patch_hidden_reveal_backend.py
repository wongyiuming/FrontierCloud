from pathlib import Path
import re

path = Path("app/api/v1/media.py")
text = path.read_text(encoding="utf-8")


def one(old: str, new: str, label: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, found {count}")
    text = text.replace(old, new, 1)


def regex(pattern: str, replacement: str, label: str) -> None:
    global text
    text, count = re.subn(pattern, replacement, text, count=1, flags=re.S | re.M)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, found {count}")


one("from sqlalchemy import bindparam, text\n", "", "remove visibility SQL imports")
one(
    '''def _category_url(media_type: str, relative_path: str) -> str:
    query = urllib.parse.urlencode({"path": relative_path})
    return f"/api/v1/media/{media_type}/category?{query}"
''',
    '''def _category_url(media_type: str, relative_path: str, *, include_hidden: bool = False) -> str:
    query = {"path": relative_path}
    if include_hidden:
        query["include_hidden"] = "true"
    return f"/api/v1/media/{media_type}/category?{urllib.parse.urlencode(query)}"
''',
    "category URL",
)

regex(
    r'''async def get_media_categories\(media_type, valid_exts\):\n.*?\n    return categories\n''',
    '''async def get_media_categories(media_type, valid_exts, *, include_hidden: bool = False):
    identity = f"{media_type}:all" if include_hidden else media_type
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
            merged.setdefault(name, {
                "name": name,
                "url": _category_url(
                    media_type,
                    _typed_media_root(media_type).name + "/" + name,
                    include_hidden=include_hidden,
                ),
            })
        categories = sorted(merged.values(), key=lambda entry: entry["name"].casefold())
    else:
        categories = await asyncio.to_thread(_get_media_categories_sync, media_type, valid_exts, hidden)
        if include_hidden:
            for entry in categories:
                entry["url"] = _category_url(media_type, entry["url"].split("path=", 1)[1], include_hidden=True)
    await store_media_catalog(generation, "categories", identity, categories)
    return categories
''',
    "categories",
)

regex(
    r'''@router\.get\("/catalog/categories"\)\nasync def get_media_categories_data\(\n.*?\n    \)\n''',
    '''@router.get("/catalog/categories")
async def get_media_categories_data(
    media_type: str = Query(..., pattern=r"^(music|video)$"),
    include_hidden: bool = False,
):
    valid_exts = AUDIO_EXTS if media_type == "music" else VIDEO_EXTS
    entries = await get_media_categories(media_type, valid_exts, include_hidden=include_hidden)
    return JSONResponse(
        {"entries": entries},
        headers={"Cache-Control": "private, max-age=15" if include_hidden else "private, max-age=30, stale-while-revalidate=300"},
    )
''',
    "category endpoint",
)

one(
    '''def _get_media_subcategories_sync(media_type, category_subpath, valid_exts, hidden):
''',
    '''def _get_media_subcategories_sync(media_type, category_subpath, valid_exts, hidden, include_hidden=False):
''',
    "subcategory sync signature",
)
one(
    '''            subcategories.append({
                "name": child.name,
                "url": _category_url(media_type, rel_child),
            })
''',
    '''            subcategories.append({
                "name": child.name,
                "url": _category_url(media_type, rel_child, include_hidden=include_hidden),
            })
''',
    "subcategory URLs",
)
regex(
    r'''async def get_media_subcategories\(media_type, category_subpath, valid_exts\):\n.*?\n    return subcategories\n''',
    '''async def get_media_subcategories(media_type, category_subpath, valid_exts, *, include_hidden: bool = False):
    identity = f"{media_type}:{category_subpath}:{'all' if include_hidden else 'public'}"
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
                merged.setdefault(name, {
                    "name": name,
                    "url": _category_url(media_type, category_subpath + "/" + name, include_hidden=include_hidden),
                })
        subcategories = sorted(merged.values(), key=lambda entry: entry["name"].casefold())
    else:
        subcategories = await asyncio.to_thread(
            _get_media_subcategories_sync,
            media_type,
            category_subpath,
            valid_exts,
            hidden,
            include_hidden,
        )
    await store_media_catalog(generation, "subcategories", identity, subcategories)
    return subcategories
''',
    "subcategories",
)

regex(
    r'''async def scan_media_files_by_category\(category_subpath, valid_exts, media_type\):\n.*?\n    return media_list\n''',
    '''async def scan_media_files_by_category(category_subpath, valid_exts, media_type, *, include_hidden: bool = False):
    identity = f"{media_type}:{category_subpath}:{'all' if include_hidden else 'public'}"
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
            media_list = await asyncio.to_thread(
                _scan_media_files_by_category_sync,
                category_subpath,
                valid_exts,
                media_type,
                hidden,
            )
            media_list = await media_objects.bind_items(media_list, media_type)
    await store_media_catalog(generation, "tracks-v2", identity, media_list)
    return media_list
''',
    "media list",
)

one(
    '''async def _player_entries(
    path: str,
    media_type: str,
    playback_session_id: str,
) -> list[dict]:
''',
    '''async def _player_entries(
    path: str,
    media_type: str,
    playback_session_id: str,
    *,
    include_hidden: bool = False,
) -> list[dict]:
''',
    "player entries signature",
)
one(
    "    items = await scan_media_files_by_category(path, valid_exts, playback_type)\n",
    "    items = await scan_media_files_by_category(path, valid_exts, playback_type, include_hidden=include_hidden)\n",
    "player list",
)
regex(
    r'''@router\.get\("/catalog/media"\)\nasync def get_media_catalog_data\(\n.*?\n    \)\n''',
    '''@router.get("/catalog/media")
async def get_media_catalog_data(
    media_type: str = Query(..., pattern=r"^(music|video)$"),
    path: str = Query(..., min_length=1, max_length=1024),
    playback_session_id: str = Query(..., min_length=1, max_length=64),
    include_hidden: bool = False,
):
    await _public_category_parts(path, media_type)
    entries = await _player_entries(path, media_type, playback_session_id, include_hidden=include_hidden)
    return JSONResponse(
        {"entries": entries},
        headers={"Cache-Control": "private, max-age=15, stale-while-revalidate=120"},
    )
''',
    "media endpoint",
)

regex(
    r'''async def _local_stream_metadata\(relative_path: str, object_kind: str\) -> dict\[str, str\]:\n.*?\n    owner_id = node_state\.node\["node_id"\]\n''',
    '''async def _local_stream_metadata(relative_path: str, object_kind: str) -> dict[str, str]:
    async with engine.begin() as conn:
        media_id = await media_objects.ensure_object(conn, relative_path, object_kind)
    owner_id = node_state.node["node_id"]
''',
    "public direct stream",
)

regex(
    r'''@router\.get\("/music", response_class=HTMLResponse\)\nasync def get_music_categories_page\(\):\n.*?\n    \)\n\n\n@router\.get\("/video", response_class=HTMLResponse\)\nasync def get_video_categories_page\(\):\n.*?\n    \)\n''',
    '''@router.get("/music", response_class=HTMLResponse)
async def get_music_categories_page(include_hidden: bool = False):
    suffix = "&include_hidden=true" if include_hidden else ""
    return _render_category_page(
        "前沿音乐",
        "/api/v1/media",
        {
            "url": f"/api/v1/media/catalog/categories?media_type=music{suffix}",
            "cacheKey": f"categories:music:{'all' if include_hidden else 'public'}",
            "emptyText": "暂无音乐分类目录，请在 data/media/music 下创建分类文件夹",
        },
    )


@router.get("/video", response_class=HTMLResponse)
async def get_video_categories_page(include_hidden: bool = False):
    suffix = "&include_hidden=true" if include_hidden else ""
    return _render_category_page(
        "前沿视讯",
        "/api/v1/media",
        {
            "url": f"/api/v1/media/catalog/categories?media_type=video{suffix}",
            "cacheKey": f"categories:video:{'all' if include_hidden else 'public'}",
            "emptyText": "暂无视频分类目录，请在 data/media/vido 下创建分类文件夹",
        },
    )
''',
    "top-level catalog pages",
)

one(
    '''async def _get_player_or_subcategories(
    path: str,
    media_type: str,
    playback_type: str,
    valid_exts,
    player_template: str,
    title_prefix: str,
    direct: bool = False,
) -> HTMLResponse:
''',
    '''async def _get_player_or_subcategories(
    path: str,
    media_type: str,
    playback_type: str,
    valid_exts,
    player_template: str,
    title_prefix: str,
    include_hidden: bool = False,
    direct: bool = False,
) -> HTMLResponse:
''',
    "player page signature",
)
one(
    '    type_list_url = f"/api/v1/media/{media_type}"\n',
    '    type_list_url = f"/api/v1/media/{media_type}" + ("?include_hidden=true" if include_hidden else "")\n',
    "player back URL",
)
one(
    "        subcategories = await get_media_subcategories(media_type, path, valid_exts)\n",
    "        subcategories = await get_media_subcategories(media_type, path, valid_exts, include_hidden=include_hidden)\n",
    "player subcategories",
)
one(
    '            if node_state.node["role"] == "Master" and await scan_media_files_by_category(path, valid_exts, playback_type):\n',
    '            if node_state.node["role"] == "Master" and await scan_media_files_by_category(path, valid_exts, playback_type, include_hidden=include_hidden):\n',
    "direct media presence",
)
one(
    '                subcategories = [{"name": "当前目录曲目", "url": _category_url(media_type, path) + "&direct=true"}] + subcategories\n',
    '                subcategories = [{"name": "当前目录曲目", "url": _category_url(media_type, path, include_hidden=include_hidden) + "&direct=true"}] + subcategories\n',
    "direct media URL",
)
one(
    '        back_url = _category_url(media_type, parent_path)\n',
    '        back_url = _category_url(media_type, parent_path, include_hidden=include_hidden)\n',
    "parent URL",
)
one(
    '''    catalog_query = urllib.parse.urlencode(
        {
            "media_type": media_type,
            "path": path,
            "playback_session_id": session_id,
        }
    )
''',
    '''    catalog_params = {
        "media_type": media_type,
        "path": path,
        "playback_session_id": session_id,
    }
    if include_hidden:
        catalog_params["include_hidden"] = "true"
    catalog_query = urllib.parse.urlencode(catalog_params)
''',
    "player catalog query",
)
one(
    '        "cacheKey": f"media:{media_type}:{path}",\n',
    '        "cacheKey": f"media:{media_type}:{path}:{\'all\' if include_hidden else \'public\'}",\n',
    "player cache key",
)
one(
    '''async def get_music_player_page(
    path: str = Query(...),
    direct: bool = False,
):
''',
    '''async def get_music_player_page(
    path: str = Query(...),
    include_hidden: bool = False,
    direct: bool = False,
):
''',
    "music player query",
)
one(
    '''        "audio-player.html",
        "前沿音乐",
        direct,
''',
    '''        "audio-player.html",
        "前沿音乐",
        include_hidden,
        direct,
''',
    "music player pass",
)
one(
    '''async def get_video_player_page(
    path: str = Query(...),
    direct: bool = False,
):
''',
    '''async def get_video_player_page(
    path: str = Query(...),
    include_hidden: bool = False,
    direct: bool = False,
):
''',
    "video player query",
)
one(
    '''        "video-player.html",
        "前沿视讯",
        direct,
''',
    '''        "video-player.html",
        "前沿视讯",
        include_hidden,
        direct,
''',
    "video player pass",
)

hidden_lyric_guard = '''            if _is_publicly_hidden(normalized_track, await _hidden_set()):
                raise FileNotFoundError
'''
if text.count(hidden_lyric_guard) != 2:
    raise SystemExit(f"hidden lyric guard: expected two matches, found {text.count(hidden_lyric_guard)}")
text = text.replace(hidden_lyric_guard, "")

path.write_text(text, encoding="utf-8")
