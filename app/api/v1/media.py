import asyncio
import html as html_escape
import json
import mimetypes
import urllib.parse
import uuid
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from app.services.media_catalog_cache import load_media_catalog, store_media_catalog
from app.services import karaoke_identity, playback
from app.services import network_observation
from app.services import lyrics
from app.services import media_objects
from app.core.config import settings
from app.core.db import engine
from app.core.static_assets import static_asset_url
from app.api.internal_nodes import require_https
from app.services.federation.state import state as node_state
from app.services.federation.catalog import catalog as node_catalog
from app.services.federation import routing as node_routing
from app.services.federation import protocol as node_protocol
from app.services.media_manager import ensure_media_mutations_ready, media_mutation_lock

router = APIRouter()
BASE_DIR = Path(__file__).resolve().parents[3]
MEDIA_ROOT = (BASE_DIR / "data" / "media").resolve()
MUSIC_ROOT = (MEDIA_ROOT / "music").resolve()
VIDEO_ROOT = (MEDIA_ROOT / "vido").resolve()
LYRICS_ROOT = (MEDIA_ROOT / "lyrics").resolve()
STATIC_MEDIA_DIR = BASE_DIR / "static" / "media"
MUSIC_ROOT.mkdir(parents=True, exist_ok=True)
VIDEO_ROOT.mkdir(parents=True, exist_ok=True)
LYRICS_ROOT.mkdir(parents=True, exist_ok=True)

AUDIO_EXTS = (".mp3", ".m4a", ".flac", ".wav")
VIDEO_EXTS = (".mp4", ".webm", ".mkv")
NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


class PlaybackReport(BaseModel):
    media_path: str = Field(min_length=1, max_length=1024)
    resource_id: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")
    playback_session_id: str = Field(min_length=1, max_length=64)
    played_seconds: float = Field(gt=0, le=86400)
    duration: float = Field(gt=0, le=86400)


class NetworkObservation(BaseModel):
    addresses: list[str] = Field(default_factory=list, max_length=8)
    failure: str | None = Field(None, max_length=32)


def inject_page_runtime(html: str) -> str:
    player_assets = [
        static_asset_url("js/player.js"),
        static_asset_url("css/player.css"),
        static_asset_url("js/lyrics.js"),
        static_asset_url("css/lyrics.css"),
    ]
    replacements = {
        "{{STUN_URLS_JSON}}": safe_json_dumps(settings.webrtc_stun_urls()),
        "{{WEBRTC_INTERVAL_MS}}": str(settings.WEBRTC_REPORT_COOLDOWN * 1000),
        "{{NETWORK_OBSERVATION_JS_URL}}": html_escape.escape(static_asset_url("js/network-observation.js"), quote=True),
        "{{PLAYER_JS_URL}}": html_escape.escape(static_asset_url("js/player.js"), quote=True),
        "{{PLAYER_CSS_URL}}": html_escape.escape(static_asset_url("css/player.css"), quote=True),
        "{{LYRICS_JS_URL}}": html_escape.escape(static_asset_url("js/lyrics.js"), quote=True),
        "{{LYRICS_CSS_URL}}": html_escape.escape(static_asset_url("css/lyrics.css"), quote=True),
        "{{MEDIA_BROWSER_JS_URL}}": html_escape.escape(static_asset_url("js/media-browser.js"), quote=True),
        "{{MEDIA_PREFETCH_ASSETS_JSON}}": safe_json_dumps(player_assets),
    }
    for marker, value in replacements.items():
        html = html.replace(marker, value)
    return html


def safe_json_dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False).replace("&", r"\u0026").replace("<", r"\u003c").replace(">", r"\u003e").replace("'", r"\u0027")


def resolve_safe_path(base_dir: Path, sub_path: str) -> Path:
    try:
        clean = str(sub_path or "").replace("\\", "/").lstrip("/")
        target = (base_dir / clean).resolve()
        if not target.is_relative_to(base_dir.resolve()):
            raise ValueError
        return target
    except Exception as exc:
        raise ValueError("Invalid path") from exc


async def _hidden_set() -> set[str]:
    from app.services.media_manager import MediaManager
    return await MediaManager.hidden_paths()


def _is_publicly_hidden(relative_path: str, hidden: set[str]) -> bool:
    parts = relative_path.split("/")
    for i in range(1, len(parts) + 1):
        if "/".join(parts[:i]) in hidden:
            return True
    return False


def _typed_media_root(media_type: str) -> Path:
    return MUSIC_ROOT if media_type == "music" else VIDEO_ROOT


def _direct_media_files(directory: Path, valid_exts) -> list[Path]:
    """Return supported media directly inside one directory."""
    return [
        path
        for path in directory.iterdir()
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in valid_exts
    ]


def _category_url(media_type: str, relative_path: str, *, include_hidden: bool = False) -> str:
    query = {"path": relative_path}
    if include_hidden:
        query["include_hidden"] = "true"
    return f"/api/v1/media/{media_type}/category?{urllib.parse.urlencode(query)}"


def _has_visible_direct_media(directory: Path, valid_exts, hidden: set[str]) -> bool:
    return any(
        not _is_publicly_hidden(path.relative_to(MEDIA_ROOT).as_posix(), hidden)
        for path in _direct_media_files(directory, valid_exts)
    )


def _get_media_categories_sync(media_type, valid_exts, hidden: set[str], include_hidden=False):
    categories = []
    type_root = _typed_media_root(media_type)
    if not type_root.exists():
        return categories
    for entry in sorted(type_root.iterdir(), key=lambda p: p.name.casefold()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        rel_entry = entry.relative_to(MEDIA_ROOT).as_posix()
        if _is_publicly_hidden(rel_entry, hidden):
            continue
        has_direct_media = _has_visible_direct_media(entry, valid_exts, hidden)
        has_child_media = any(
            not _is_publicly_hidden(child.relative_to(MEDIA_ROOT).as_posix(), hidden)
            and _has_visible_direct_media(child, valid_exts, hidden)
            for child in entry.iterdir()
            if child.is_dir() and not child.is_symlink()
        )
        if has_direct_media or has_child_media:
            categories.append({"name": entry.name, "url": _category_url(media_type, rel_entry, include_hidden=include_hidden)})
    return categories


async def get_media_categories(media_type, valid_exts, *, include_hidden: bool = False):
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
        categories = await asyncio.to_thread(
            _get_media_categories_sync,
            media_type,
            valid_exts,
            hidden,
            include_hidden,
        )
    await store_media_catalog(generation, "categories", identity, categories)
    return categories


@router.get("/catalog/categories")
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


def _get_media_subcategories_sync(media_type, category_subpath, valid_exts, hidden, include_hidden=False):
    try:
        category_dir = resolve_safe_path(MEDIA_ROOT, category_subpath)
    except ValueError:
        return []
    expected_root = _typed_media_root(media_type)
    if (
        not category_dir.exists()
        or not category_dir.is_dir()
        or category_dir.is_symlink()
        or category_dir.parent != expected_root
    ):
        return []

    subcategories = []
    for child in sorted(category_dir.iterdir(), key=lambda path: path.name.casefold()):
        if not child.is_dir() or child.is_symlink():
            continue
        rel_child = child.relative_to(MEDIA_ROOT).as_posix()
        if _is_publicly_hidden(rel_child, hidden):
            continue
        if _has_visible_direct_media(child, valid_exts, hidden):
            subcategories.append({
                "name": child.name,
                "url": _category_url(media_type, rel_child, include_hidden=include_hidden),
            })
    return subcategories


async def get_media_subcategories(media_type, category_subpath, valid_exts, *, include_hidden: bool = False):
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


def _scan_media_files_by_category_sync(category_subpath, valid_exts, media_type, hidden):
    try:
        target_dir = resolve_safe_path(MEDIA_ROOT, category_subpath)
    except ValueError:
        return []
    expected_root = MUSIC_ROOT if media_type == "audio" else VIDEO_ROOT
    rel_parts = target_dir.relative_to(MEDIA_ROOT).parts if target_dir.exists() else ()
    if (
        not target_dir.exists()
        or not target_dir.is_dir()
        or target_dir.is_symlink()
        or len(rel_parts) not in {2, 3}
        or rel_parts[0] != expected_root.name
    ):
        return []
    result = []
    for file_path in _direct_media_files(target_dir, valid_exts):
        rel = file_path.relative_to(MEDIA_ROOT).as_posix()
        if _is_publicly_hidden(rel, hidden):
            continue
        result.append({
            "media_path": rel,
            "title": file_path.stem,
            "artist": "前沿娱乐",
            "type": media_type,
            "url": f"/api/v1/media/stream?file_path={urllib.parse.quote(rel)}",
            "cover": "/favicon.ico",
        })
    return result


async def scan_media_files_by_category(category_subpath, valid_exts, media_type, *, include_hidden: bool = False):
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


async def _public_category_parts(path: str, media_type: str) -> tuple[str, ...]:
    try:
        _, parts = _validated_public_directory(path, media_type)
        return parts
    except HTTPException:
        parts = tuple(path.split("/"))
        expected_root = _typed_media_root(media_type).name
        if (
            node_state.node["role"] != "Master"
            or len(parts) not in (2, 3)
            or parts[0] != expected_root
            or any(not part or part.startswith(".") for part in parts)
            or not await node_catalog.resources(directory=path)
        ):
            raise
        return parts


async def _player_entries(
    path: str,
    media_type: str,
    playback_session_id: str,
    *,
    include_hidden: bool = False,
) -> list[dict]:
    playback_type = "audio" if media_type == "music" else "video"
    valid_exts = AUDIO_EXTS if media_type == "music" else VIDEO_EXTS
    items = await scan_media_files_by_category(path, valid_exts, playback_type, include_hidden=include_hidden)
    remote = [item for item in items if item.get("resource_id")]
    media_list = await playback.attach_stats_and_sort(
        [item for item in items if not item.get("resource_id")],
        playback_session_id,
    )
    if playback_type == "audio":
        media_list = await lyrics.attach_links(media_list)
    if remote:
        media_list = playback.sort_media(
            media_list + await node_routing.attach_master_stats(remote),
            playback_session_id,
        )
    karaoke_identity.attach(media_list)
    return media_list


@router.get("/catalog/media")
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


@lru_cache(maxsize=16)
def load_html_template(filename: str) -> str:
    path = STATIC_MEDIA_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Template {filename} not found")
    return path.read_text(encoding="utf-8")


@router.get("/stream")
@router.head("/stream", include_in_schema=False)
async def stream_media_file(file_path: str | None = None, resource_id: str | None = None, request: Request = None):
    if resource_id is not None:
        require_https(request)
        response = await node_routing.stream(resource_id, file_path)
        _bind_response_media_audit(request, response)
        return response
    if not file_path:
        raise HTTPException(status_code=422, detail="file_path or resource_id is required")
    async with media_mutation_lock.shared():
        ensure_media_mutations_ready()
        return await _local_stream_response(file_path, request)


def _bind_response_media_audit(request: Request | None, response: Response) -> None:
    if request is not None:
        request.scope["media_audit"] = {
            "resource_id": response.headers.get("X-Media-Resource-ID", ""),
            "owner_id": response.headers.get("X-Media-Owner-ID", ""),
            "media_id": response.headers.get("X-Media-Object-ID", ""),
        }


async def _local_stream_metadata(relative_path: str, object_kind: str) -> dict[str, str]:
    async with engine.begin() as conn:
        media_id = await media_objects.ensure_object(conn, relative_path, object_kind)
    owner_id = node_state.node["node_id"]
    return {"resource_id": node_protocol.resource_id(owner_id, media_id), "owner_id": owner_id, "media_id": media_id}


async def _local_stream_response(file_path: str, request: Request | None) -> Response:
    try:
        safe_path = resolve_safe_path(MEDIA_ROOT, file_path)
    except ValueError:
        raise HTTPException(status_code=403, detail="Forbidden path access")
    if safe_path.is_symlink() or not safe_path.is_file():
        if node_state.node["role"] == "Master":
            rows = await node_catalog.resources(path=file_path)
            if rows:
                require_https(request)
                response = await node_routing.stream(rows[0]["resource_id"], file_path)
                _bind_response_media_audit(request, response)
                return response
        raise HTTPException(status_code=404, detail="Media file not found")
    rel_parts = safe_path.relative_to(MEDIA_ROOT).parts
    if len(rel_parts) not in {3, 4} or rel_parts[0] not in {"music", "vido"}:
        raise HTTPException(status_code=403, detail="Forbidden media layout")
    allowed_exts = AUDIO_EXTS if rel_parts[0] == "music" else VIDEO_EXTS
    if safe_path.suffix.lower() not in allowed_exts:
        raise HTTPException(status_code=403, detail="Forbidden media type")
    rel = safe_path.relative_to(MEDIA_ROOT).as_posix()
    metadata = await _local_stream_metadata(rel, "audio" if rel_parts[0] == "music" else "video")
    if request is not None:
        request.scope["media_audit"] = metadata
    content_type = mimetypes.guess_type(safe_path.name)[0] or "application/octet-stream"
    return Response(
        media_type=content_type,
        headers={
            "X-Accel-Redirect": (
                "/_protected_media/"
                + urllib.parse.quote(rel, safe="/")
            ),
            "Cache-Control": "public, max-age=86400",
            "X-Media-Resource-ID": metadata["resource_id"],
            "X-Media-Owner-ID": metadata["owner_id"],
            "X-Media-Object-ID": metadata["media_id"],
        },
    )


@router.get("/", response_class=HTMLResponse)
@router.get("", response_class=HTMLResponse)
async def get_media_index_page():
    return HTMLResponse(
        inject_page_runtime(load_html_template("index.html")),
        headers=NO_STORE_HEADERS,
    )


@router.get("/refresh")
async def refresh_media_interface():
    return RedirectResponse(
        url=f"/api/v1/media?ui={uuid.uuid4().hex}",
        status_code=303,
        headers={**NO_STORE_HEADERS, "Clear-Site-Data": '"cache"'},
    )


@router.get("/music", response_class=HTMLResponse)
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


def _render_category_page(page_title: str, back_url: str, config: dict) -> HTMLResponse:
    html = load_html_template("category.html")
    html = html.replace("{{PAGE_TITLE}}", html_escape.escape(page_title))
    html = html.replace("{{BACK_URL}}", html_escape.escape(back_url, quote=True))
    html = html.replace("{{CATALOG_CONFIG_JSON}}", safe_json_dumps(config))
    html = inject_page_runtime(html)
    return HTMLResponse(html, headers=NO_STORE_HEADERS)


def _validated_public_directory(path: str, media_type: str) -> tuple[Path, tuple[str, ...]]:
    try:
        directory = resolve_safe_path(MEDIA_ROOT, path)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Media category not found") from exc
    parts = directory.relative_to(MEDIA_ROOT).parts if directory.exists() else ()
    expected_root = _typed_media_root(media_type)
    if (
        not directory.is_dir()
        or directory.is_symlink()
        or len(parts) not in {2, 3}
        or parts[0] != expected_root.name
    ):
        raise HTTPException(status_code=404, detail="Media category not found")
    return directory, parts


def _render_subcategory_page(page_title: str, back_url: str, categories) -> HTMLResponse:
    return _render_category_page(
        page_title,
        back_url,
        {"bootstrap": categories, "cacheKey": "", "url": "", "emptyText": "暂无分类目录"},
    )


async def _get_player_or_subcategories(
    path: str,
    media_type: str,
    playback_type: str,
    valid_exts,
    player_template: str,
    title_prefix: str,
    include_hidden: bool = False,
    direct: bool = False,
) -> HTMLResponse:
    parts = await _public_category_parts(path, media_type)
    type_list_url = f"/api/v1/media/{media_type}" + ("?include_hidden=true" if include_hidden else "")
    display_path = "/".join(parts[1:])
    if len(parts) == 2:
        subcategories = await get_media_subcategories(media_type, path, valid_exts, include_hidden=include_hidden)
        if subcategories and not direct:
            if node_state.node["role"] == "Master" and await scan_media_files_by_category(path, valid_exts, playback_type, include_hidden=include_hidden):
                subcategories = [{"name": "当前目录曲目", "url": _category_url(media_type, path, include_hidden=include_hidden) + "&direct=true"}] + subcategories
            return _render_subcategory_page(
                f"{title_prefix} - {display_path}",
                type_list_url,
                subcategories,
            )
        back_url = type_list_url
    else:
        parent_path = "/".join(parts[:2])
        back_url = _category_url(media_type, parent_path, include_hidden=include_hidden)

    session_id = str(uuid.uuid4())
    catalog_params = {
        "media_type": media_type,
        "path": path,
        "playback_session_id": session_id,
    }
    if include_hidden:
        catalog_params["include_hidden"] = "true"
    catalog_query = urllib.parse.urlencode(catalog_params)
    catalog_config = {
        "url": f"/api/v1/media/catalog/media?{catalog_query}",
        "cacheKey": f"media:{media_type}:{path}:{'all' if include_hidden else 'public'}",
    }
    html = load_html_template(player_template)
    html = html.replace("{{PAGE_TITLE}}", html_escape.escape(f"{title_prefix} - {display_path}"))
    html = html.replace("{{CATEGORY_LIST_URL}}", html_escape.escape(back_url, quote=True))
    html = html.replace("{{MEDIA_JSON}}", "[]")
    html = html.replace("{{PLAYBACK_SESSION_ID}}", safe_json_dumps(session_id))
    html = html.replace("{{PLAYER_CATALOG_CONFIG_JSON}}", safe_json_dumps(catalog_config))
    html = inject_page_runtime(html)
    return HTMLResponse(html, headers=NO_STORE_HEADERS)


@router.get("/music/category", response_class=HTMLResponse)
async def get_music_player_page(
    path: str = Query(...),
    include_hidden: bool = False,
    direct: bool = False,
):
    return await _get_player_or_subcategories(
        path,
        "music",
        "audio",
        AUDIO_EXTS,
        "audio-player.html",
        "前沿音乐",
        include_hidden,
        direct,
    )


@router.get("/video/category", response_class=HTMLResponse)
async def get_video_player_page(
    path: str = Query(...),
    include_hidden: bool = False,
    direct: bool = False,
):
    return await _get_player_or_subcategories(
        path,
        "video",
        "video",
        VIDEO_EXTS,
        "video-player.html",
        "前沿视讯",
        include_hidden,
        direct,
    )


@router.get("/lyrics", response_class=HTMLResponse)
async def get_lyrics_page(track: str = Query(..., min_length=1, max_length=1024), resource_id: str | None = None, request: Request = None):
    try:
        if resource_id:
            require_https(request)
            entries = await node_routing.lyric_entries(resource_id, track)
        else:
            normalized_track, _track_path = lyrics.validate_track(track)
            _lyric_path, entries = await lyrics.load_for_track(normalized_track)
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="Lyrics not found")
    html = load_html_template("lyrics.html")
    lines = [entry["text"] for entry in entries]
    html = html.replace("{{LYRICS_JSON}}", safe_json_dumps(lines))
    html = html.replace("{{LINE_COUNT}}", str(len(lines)))
    return HTMLResponse(inject_page_runtime(html), headers=NO_STORE_HEADERS)


@router.get("/lyrics/content")
async def get_lyrics_content(track: str = Query(..., min_length=1, max_length=1024), resource_id: str | None = None, request: Request = None):
    try:
        if resource_id:
            require_https(request)
            entries = await node_routing.lyric_entries(resource_id, track)
        else:
            normalized_track, _track_path = lyrics.validate_track(track)
            _lyric_path, entries = await lyrics.load_for_track(normalized_track)
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="Lyrics not found")
    return JSONResponse({"entries": entries}, headers=NO_STORE_HEADERS)


@router.post("/playback")
async def report_playback(payload: PlaybackReport):
    try:
        if payload.resource_id:
            return await node_routing.mutate_stats(payload.resource_id, payload.media_path, session=payload.playback_session_id,
                played=payload.played_seconds, duration=payload.duration)
        return await playback.record_playback(
            MEDIA_ROOT,
            payload.media_path,
            payload.playback_session_id,
            payload.played_seconds,
            payload.duration,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/network-observation")
async def report_network_observation(request: Request, payload: NetworkObservation):
    try:
        return await network_observation.record_observation(request, payload.addresses, payload.failure)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
