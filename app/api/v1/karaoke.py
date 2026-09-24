"""Karaoke business contract. FastAPI remains the only media authority."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from app.api.v1 import media as media_api
from app.api.v1.karaoke_contract import KaraokeContext
from app.services import karaoke_identity, lyrics, media_objects
from app.services.federation import routing as node_routing


router = APIRouter()


async def _resolve(token: str, request: Request | None) -> dict:
    try:
        kind, identifier = karaoke_identity.resolve(token)
    except karaoke_identity.InvalidKaraokeIdentity as exc:
        raise HTTPException(404, "Karaoke media not found") from exc

    if kind == "global":
        media_api.require_https(request)
        row, _relationship = await node_routing.resolve(identifier)
        payload = row["payload"]
        media_type = payload.get("type")
        if media_type not in {"audio", "video"}:
            raise HTTPException(404, "Karaoke media not found")
        has_lyrics = False
        if media_type == "audio":
            # Master lyric relations are authoritative and independent of placement.
            has_lyrics = bool(await node_routing.lyric_entries(identifier))
        return {
            "kind": kind,
            "identifier": identifier,
            "path": row["path"],
            "type": media_type,
            "has_lyrics": has_lyrics,
        }

    row = await media_objects.object_by_id(identifier)
    if not row or row["object_kind"] not in {"audio", "video"}:
        raise HTTPException(404, "Karaoke media not found")
    media_path, media_type = str(row["media_path"]), str(row["object_kind"])
    async with media_api.media_mutation_lock.shared():
        media_api.ensure_media_mutations_ready()
        try:
            path = media_api.resolve_safe_path(media_api.MEDIA_ROOT, media_path)
        except ValueError as exc:
            raise HTTPException(404, "Karaoke media not found") from exc
        root = "music" if media_type == "audio" else "vido"
        allowed = media_api.AUDIO_EXTS if media_type == "audio" else media_api.VIDEO_EXTS
        parts = path.relative_to(media_api.MEDIA_ROOT).parts if path.exists() else ()
        if (path.is_symlink() or not path.is_file() or len(parts) not in {3, 4}
                or parts[0] != root or path.suffix.lower() not in allowed):
            raise HTTPException(404, "Karaoke media not found")
        if media_type == "audio":
            linked = await lyrics.attach_links([{"media_id": identifier}])
            has_lyrics = bool(linked[0]["has_lyrics"])
        else:
            has_lyrics = False
    return {"kind": kind, "identifier": identifier, "path": media_path,
            "type": media_type, "has_lyrics": has_lyrics}


@router.get("/context", response_model=KaraokeContext)
async def context(
    media: str = Query(..., min_length=80, max_length=512, pattern=r"^[A-Za-z0-9_-]+={0,2}$"),
    request: Request = None,
):
    resolved = await _resolve(media, request)
    query = urlencode({"media": media})
    return KaraokeContext(
        id=media,
        title=Path(resolved["path"]).stem,
        type=resolved["type"],
        stream_url=f"/api/v1/karaoke/stream?{query}",
        has_lyrics=resolved["has_lyrics"],
        lyrics_url=f"/api/v1/karaoke/lyrics?{query}" if resolved["has_lyrics"] else None,
        cover_url="/favicon.ico",
    )


@router.api_route("/stream", methods=["GET", "HEAD"], include_in_schema=False)
async def stream(media: str = Query(..., min_length=80, max_length=512), request: Request = None):
    resolved = await _resolve(media, request)
    if resolved["kind"] == "global":
        response = await node_routing.stream(resolved["identifier"])
        media_api._bind_response_media_audit(request, response)
        return response
    async with media_api.media_mutation_lock.shared():
        media_api.ensure_media_mutations_ready()
        return await media_api._local_stream_response(resolved["path"], request)


@router.get("/lyrics", include_in_schema=False)
async def lyric_entries(media: str = Query(..., min_length=80, max_length=512), request: Request = None):
    resolved = await _resolve(media, request)
    if not resolved["has_lyrics"]:
        raise HTTPException(404, "Lyrics not found")
    try:
        if resolved["kind"] == "global":
            entries = await node_routing.lyric_entries(resolved["identifier"])
        else:
            _lyric_path, entries = await lyrics.load_for_track(resolved["path"])
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, "Lyrics not found") from exc
    return JSONResponse({"entries": entries}, headers=media_api.NO_STORE_HEADERS)
