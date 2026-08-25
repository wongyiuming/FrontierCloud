import asyncio
import hashlib
import html as html_escape
import json
import os
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
STATIC_MEDIA_DIR = BASE_DIR / "static" / "media"
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

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
        "{{KARAOKE_JS_URL}}": html_escape.escape(static_asset_url("js/karaoke.js"), quote=True),
        "{{KARAOKE_CSS_URL}}": html_escape.escape(static_asset_url("css/karaoke.css"), quote=True),
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


def _get_media_categories_sync(media_type, valid_exts, hidden: set[str]):
    categories = []
    if not MEDIA_ROOT.exists():
        return categories
    for entry in sorted(MEDIA_ROOT.iterdir(), key=lambda p: p.name.casefold()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        rel_entry = entry.relative_to(MEDIA_ROOT).as_posix()
        if _is_publicly_hidden(rel_entry, hidden):
            continue
        has_files = False
        for root, dirs, files in os.walk(entry, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
            for f in files:
                p = Path(root) / f
                rel = p.relative_to(MEDIA_ROOT).as_posix()
                if f.lower().endswith(valid_exts) and not _is_publicly_hidden(rel, hidden):
                    has_files = True
                    break
            if has_files:
                break
        if has_files:
            categories.append({"name": entry.name, "url": f"/api/v1/media/{media_type}/category?path={urllib.parse.quote(rel_entry)}"})
    return categories


async def get_media_categories(media_type, valid_exts):
    generation, cached = await load_media_catalog("categories", media_type)
    if cached is not None:
        return cached
    hidden = await _hidden_set()
    categories = await asyncio.to_thread(_get_media_categories_sync, media_type, valid_exts, hidden)
    await store_media_catalog(generation, "categories", media_type, categories)
    return categories


def _scan_media_files_by_category_sync(category_subpath, valid_exts, media_type, hidden):
    try:
        target_dir = resolve_safe_path(MEDIA_ROOT, category_subpath)
    except ValueError:
        return []
    if not target_dir.exists() or not target_dir.is_dir() or target_dir.is_symlink():
        return []
    result = []
    for root, dirs, files in os.walk(target_dir, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        for file in files:
            if not file.lower().endswith(valid_exts):
                continue
            file_path = Path(root) / file
            if file_path.is_symlink():
                continue
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
    if safe_path.parent == MEDIA_ROOT:
        raise HTTPException(status_code=403, detail="媒体文件禁止直接存放在 data/media 根目录")
    if safe_path.is_symlink() or not safe_path.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    if safe_path.suffix.lower() not in AUDIO_EXTS + VIDEO_EXTS:
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


@router.get("/karaoke", response_class=HTMLResponse)
async def get_karaoke_page():
    headers = {
        **NO_STORE_HEADERS,
        "Permissions-Policy": "microphone=(self), camera=()",
        "Feature-Policy": "microphone 'self'; camera 'none'",
    }
    return HTMLResponse(
        inject_page_runtime(load_html_template("karaoke.html")),
        headers=headers,
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
    html = html.replace("{{PAGE_TITLE}}", html_escape.escape("前沿媒体"))
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


@router.get("/music/category", response_class=HTMLResponse)
async def get_music_player_page(path: str = Query(...)):
    session_id = str(uuid.uuid4())
    media_list = await playback.attach_stats_and_sort(
        await scan_media_files_by_category(path, AUDIO_EXTS, "audio"),
        session_id,
    )
    html = load_html_template("audio-player.html")
    html = html.replace("{{PAGE_TITLE}}", html_escape.escape(f"前沿音乐 - {path}"))
    html = html.replace("{{CATEGORY_LIST_URL}}", html_escape.escape("/api/v1/media/music"))
    html = html.replace("{{MEDIA_JSON}}", safe_json_dumps(media_list))
    html = html.replace("{{PLAYBACK_SESSION_ID}}", safe_json_dumps(session_id))
    html = inject_page_runtime(html)
    return HTMLResponse(html, headers=NO_STORE_HEADERS)


@router.get("/video/category", response_class=HTMLResponse)
async def get_video_player_page(path: str = Query(...)):
    session_id = str(uuid.uuid4())
    media_list = await playback.attach_stats_and_sort(
        await scan_media_files_by_category(path, VIDEO_EXTS, "video"),
        session_id,
    )
    html = load_html_template("video-player.html")
    html = html.replace("{{PAGE_TITLE}}", html_escape.escape(f"前沿视讯 - {path}"))
    html = html.replace("{{CATEGORY_LIST_URL}}", html_escape.escape("/api/v1/media/video"))
    html = html.replace("{{MEDIA_JSON}}", safe_json_dumps(media_list))
    html = html.replace("{{PLAYBACK_SESSION_ID}}", safe_json_dumps(session_id))
    html = inject_page_runtime(html)
    return HTMLResponse(html, headers=NO_STORE_HEADERS)


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
