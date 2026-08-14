import asyncio
import html as html_escape
import json
import os
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

router = APIRouter()
BASE_DIR = Path(__file__).resolve().parents[3]
MEDIA_ROOT = (BASE_DIR / "data" / "media").resolve()
STATIC_MEDIA_DIR = BASE_DIR / "static" / "media"
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

AUDIO_EXTS = (".mp3", ".m4a", ".flac", ".wav")
VIDEO_EXTS = (".mp4", ".webm", ".mkv")


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
    hidden = await _hidden_set()
    return await asyncio.to_thread(_get_media_categories_sync, media_type, valid_exts, hidden)


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
                "title": file_path.stem,
                "artist": "前沿视界",
                "type": media_type,
                "url": f"/api/v1/media/stream?file_path={urllib.parse.quote(rel)}",
                "cover": "/favicon.ico",
            })
    return result


async def scan_media_files_by_category(category_subpath, valid_exts, media_type):
    hidden = await _hidden_set()
    return await asyncio.to_thread(_scan_media_files_by_category_sync, category_subpath, valid_exts, media_type, hidden)


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
    return HTMLResponse(load_html_template("index.html"), headers={"Cache-Control": "public, max-age=3600"})


@router.get("/music", response_class=HTMLResponse)
async def get_music_categories_page():
    html = load_html_template("category.html")
    html = html.replace("{{PAGE_TITLE}}", html_escape.escape("前沿媒体"))
    html = html.replace("{{BACK_URL}}", html_escape.escape("/api/v1/media"))
    html = html.replace("{{CATEGORIES_JSON}}", safe_json_dumps(await get_media_categories("music", AUDIO_EXTS)))
    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=600"})


@router.get("/video", response_class=HTMLResponse)
async def get_video_categories_page():
    html = load_html_template("category.html")
    html = html.replace("{{PAGE_TITLE}}", html_escape.escape("前沿视讯"))
    html = html.replace("{{BACK_URL}}", html_escape.escape("/api/v1/media"))
    html = html.replace("{{CATEGORIES_JSON}}", safe_json_dumps(await get_media_categories("video", VIDEO_EXTS)))
    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=600"})


@router.get("/music/category", response_class=HTMLResponse)
async def get_music_player_page(path: str = Query(...)):
    media_list = await scan_media_files_by_category(path, AUDIO_EXTS, "audio")
    html = load_html_template("player.html")
    html = html.replace("{{PAGE_TITLE}}", html_escape.escape(f"前沿音乐 - {path}"))
    html = html.replace("{{CATEGORY_LIST_URL}}", html_escape.escape("/api/v1/media/music"))
    html = html.replace("{{MEDIA_JSON}}", safe_json_dumps(media_list))
    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=600"})


@router.get("/video/category", response_class=HTMLResponse)
async def get_video_player_page(path: str = Query(...)):
    media_list = await scan_media_files_by_category(path, VIDEO_EXTS, "video")
    html = load_html_template("player.html")
    html = html.replace("{{PAGE_TITLE}}", html_escape.escape(f"前沿视讯 - {path}"))
    html = html.replace("{{CATEGORY_LIST_URL}}", html_escape.escape("/api/v1/media/video"))
    html = html.replace("{{MEDIA_JSON}}", safe_json_dumps(media_list))
    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=600"})
