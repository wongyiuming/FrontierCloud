import asyncio
import hashlib
import html as html_escape
import json
import urllib.parse
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.services.media_catalog_cache import load_media_catalog, store_media_catalog
from app.services import playback
from app.services import network_observation
from app.core.config import settings

router = APIRouter()
BASE_DIR = Path(__file__).resolve().parents[3]
MEDIA_ROOT = (BASE_DIR / "data" / "media").resolve()
MUSIC_ROOT = (MEDIA_ROOT / "music").resolve()
VIDEO_ROOT = (MEDIA_ROOT / "vido").resolve()
STATIC_MEDIA_DIR = BASE_DIR / "static" / "media"
MUSIC_ROOT.mkdir(parents=True, exist_ok=True)
VIDEO_ROOT.mkdir(parents=True, exist_ok=True)

AUDIO_EXTS = (".mp3", ".m4a", ".flac", ".wav")
VIDEO_EXTS = (".mp4", ".webm", ".mkv")
NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


class PlaybackReport(BaseModel):
    media_path: str = Field(min_length=1, max_length=1024)
    playback_session_id: str = Field(min_length=1, max_length=64)
    played_seconds: float = Field(gt=0, le=86400)
    duration: float = Field(gt=0, le=86400)


class PreferenceChange(BaseModel):
    media_path: str = Field(min_length=1, max_length=1024)
    delta: int


class NetworkObservation(BaseModel):
    addresses: list[str] = Field(default_factory=list, max_length=8)
    failure: str | None = Field(None, max_length=32)


def static_asset_url(relative_path: str) -> str:
    path = (BASE_DIR / "static" / relative_path).resolve()
    if not path.is_relative_to((BASE_DIR / "static").resolve()) or not path.is_file():
        raise RuntimeError(f"Static asset is missing: {relative_path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return f"/static/{relative_path}?v={digest}"


def inject_page_runtime(html: str) -> str:
    replacements = {
        "{{STUN_URLS_JSON}}": safe_json_dumps(settings.webrtc_stun_urls()),
        "{{NETWORK_OBSERVATION_JS_URL}}": html_escape.escape(static_asset_url("js/network-observation.js"), quote=True),
        "{{PLAYER_JS_URL}}": html_escape.escape(static_asset_url("js/player.js"), quote=True),
        "{{PLAYER_CSS_URL}}": html_escape.escape(static_asset_url("css/player.css"), quote=True),
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


def _category_url(media_type: str, relative_path: str) -> str:
    query = urllib.parse.urlencode({"path": relative_path})
    return f"/api/v1/media/{media_type}/category?{query}"


def _has_visible_direct_media(directory: Path, valid_exts, hidden: set[str]) -> bool:
    return any(
        not _is_publicly_hidden(path.relative_to(MEDIA_ROOT).as_posix(), hidden)
        for path in _direct_media_files(directory, valid_exts)
    )


def _get_media_categories_sync(media_type, valid_exts, hidden: set[str]):
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
            categories.append({"name": entry.name, "url": _category_url(media_type, rel_entry)})
    return categories


async def get_media_categories(media_type, valid_exts):
    generation, cached = await load_media_catalog("categories", media_type)
    if cached is not None:
        return cached
    hidden = await _hidden_set()
    categories = await asyncio.to_thread(_get_media_categories_sync, media_type, valid_exts, hidden)
    await store_media_catalog(generation, "categories", media_type, categories)
    return categories


def _get_media_subcategories_sync(media_type, category_subpath, valid_exts, hidden):
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
                "url": _category_url(media_type, rel_child),
            })
    return subcategories


async def get_media_subcategories(media_type, category_subpath, valid_exts):
    identity = f"{media_type}:{category_subpath}"
    generation, cached = await load_media_catalog("subcategories", identity)
    if cached is not None:
        return cached
    hidden = await _hidden_set()
    subcategories = await asyncio.to_thread(
        _get_media_subcategories_sync,
        media_type,
        category_subpath,
        valid_exts,
        hidden,
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
            "media_id": playback.media_id_for_path(rel),
            "media_path": rel,
            "title": file_path.stem,
            "artist": "前沿视界",
            "type": media_type,
            "url": f"/api/v1/media/stream?file_path={urllib.parse.quote(rel)}",
            "cover": "/favicon.ico",
        })
    return result


async def scan_media_files_by_category(category_subpath, valid_exts, media_type):
    identity = f"{media_type}:{category_subpath}"
    generation, cached = await load_media_catalog("tracks", identity)
    if cached is not None:
        return cached
    hidden = await _hidden_set()
    media_list = await asyncio.to_thread(
        _scan_media_files_by_category_sync,
        category_subpath,
        valid_exts,
        media_type,
        hidden,
    )
    await store_media_catalog(generation, "tracks", identity, media_list)
    return media_list


def load_html_template(filename: str) -> str:
    path = STATIC_MEDIA_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Template {filename} not found")
    return path.read_text(encoding="utf-8")


@router.get("/stream")
async def stream_media_file(file_path: str = Query(...)):
    try:
        safe_path = resolve_safe_path(MEDIA_ROOT, file_path)
    except ValueError:
        raise HTTPException(status_code=403, detail="Forbidden path access")
    if safe_path.is_symlink() or not safe_path.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    rel_parts = safe_path.relative_to(MEDIA_ROOT).parts
    if len(rel_parts) not in {3, 4} or rel_parts[0] not in {"music", "vido"}:
        raise HTTPException(status_code=403, detail="Forbidden media layout")
    allowed_exts = AUDIO_EXTS if rel_parts[0] == "music" else VIDEO_EXTS
    if safe_path.suffix.lower() not in allowed_exts:
        raise HTTPException(status_code=403, detail="Forbidden media type")
    hidden = await _hidden_set()
    rel = safe_path.relative_to(MEDIA_ROOT).as_posix()
    if _is_publicly_hidden(rel, hidden):
        raise HTTPException(status_code=404, detail="Media file not found")
    return FileResponse(safe_path, headers={"Cache-Control": "public, max-age=86400"})


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
async def get_music_categories_page():
    html = load_html_template("category.html")
    html = html.replace("{{PAGE_TITLE}}", html_escape.escape("前沿音乐"))
    html = html.replace("{{BACK_URL}}", html_escape.escape("/api/v1/media"))
    html = html.replace("{{CATEGORIES_JSON}}", safe_json_dumps(await get_media_categories("music", AUDIO_EXTS)))
    html = inject_page_runtime(html)
    return HTMLResponse(html, headers=NO_STORE_HEADERS)


@router.get("/video", response_class=HTMLResponse)
async def get_video_categories_page():
    html = load_html_template("category.html")
    html = html.replace("{{PAGE_TITLE}}", html_escape.escape("前沿视讯"))
    html = html.replace("{{BACK_URL}}", html_escape.escape("/api/v1/media"))
    html = html.replace("{{CATEGORIES_JSON}}", safe_json_dumps(await get_media_categories("video", VIDEO_EXTS)))
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
    html = load_html_template("category.html")
    html = html.replace("{{PAGE_TITLE}}", html_escape.escape(page_title))
    html = html.replace("{{BACK_URL}}", html_escape.escape(back_url, quote=True))
    html = html.replace("{{CATEGORIES_JSON}}", safe_json_dumps(categories))
    return HTMLResponse(inject_page_runtime(html), headers=NO_STORE_HEADERS)


async def _get_player_or_subcategories(
    path: str,
    media_type: str,
    playback_type: str,
    valid_exts,
    player_template: str,
    title_prefix: str,
) -> HTMLResponse:
    _, parts = _validated_public_directory(path, media_type)
    type_list_url = f"/api/v1/media/{media_type}"
    display_path = "/".join(parts[1:])
    if len(parts) == 2:
        subcategories = await get_media_subcategories(media_type, path, valid_exts)
        if subcategories:
            return _render_subcategory_page(
                f"{title_prefix} - {display_path}",
                type_list_url,
                subcategories,
            )
        back_url = type_list_url
    else:
        parent_path = "/".join(parts[:2])
        back_url = _category_url(media_type, parent_path)

    session_id = str(uuid.uuid4())
    media_list = await playback.attach_stats_and_sort(
        await scan_media_files_by_category(path, valid_exts, playback_type),
        session_id,
    )
    html = load_html_template(player_template)
    html = html.replace("{{PAGE_TITLE}}", html_escape.escape(f"{title_prefix} - {display_path}"))
    html = html.replace("{{CATEGORY_LIST_URL}}", html_escape.escape(back_url, quote=True))
    html = html.replace("{{MEDIA_JSON}}", safe_json_dumps(media_list))
    html = html.replace("{{PLAYBACK_SESSION_ID}}", safe_json_dumps(session_id))
    html = inject_page_runtime(html)
    return HTMLResponse(html, headers=NO_STORE_HEADERS)


@router.get("/music/category", response_class=HTMLResponse)
async def get_music_player_page(path: str = Query(...)):
    return await _get_player_or_subcategories(
        path,
        "music",
        "audio",
        AUDIO_EXTS,
        "audio-player.html",
        "前沿音乐",
    )


@router.get("/video/category", response_class=HTMLResponse)
async def get_video_player_page(path: str = Query(...)):
    return await _get_player_or_subcategories(
        path,
        "video",
        "video",
        VIDEO_EXTS,
        "video-player.html",
        "前沿视讯",
    )


@router.post("/playback")
async def report_playback(payload: PlaybackReport):
    try:
        return await playback.record_playback(
            MEDIA_ROOT,
            payload.media_path,
            payload.playback_session_id,
            payload.played_seconds,
            payload.duration,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/preference")
async def update_preference(payload: PreferenceChange):
    try:
        return await playback.change_preference(MEDIA_ROOT, payload.media_path, payload.delta)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/network-observation")
async def report_network_observation(request: Request, payload: NetworkObservation):
    try:
        return await network_observation.record_observation(request, payload.addresses, payload.failure)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
