"""Resolve one global media identity to its transparent storage placement."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select, text

from app.core.logging_config import request_id_context, trace_id_context
from app.services import playback
from . import protocol as p
from . import schema as s
from .catalog import catalog
from .runtime import runtime
from .state import state


async def resolve(identifier: str, path: str | None = None):
    try:
        row = await catalog.resource(identifier)
        if path is not None and path != row["path"]:
            raise p.ProtocolError("Media path does not match object")
        relation = await state.relationship(row["relationship_id"]) if row["relationship_id"] else None
    except p.ProtocolError as exc:
        raise HTTPException(404, "Media object not found") from exc
    if relation is not None and relation["state"] != "active":
        raise HTTPException(404, "Media object not found")
    if row.get("health") != "online" or (relation is not None and (
            relation["status"] == "offline" or int(time.time()) - relation["last_heartbeat"] >= p.OFFLINE_SECONDS)):
        raise HTTPException(503, "Media temporarily unavailable", headers={"Retry-After": "30", "Cache-Control": "no-store"})
    return row, relation


async def stream(identifier: str, path=None):
    row, relation = await resolve(identifier, path)
    provenance = {"X-Media-Resource-ID": row["resource_id"], "X-Media-Owner-ID": row["owner_id"],
                  "X-Media-Object-ID": row["object_id"]}
    if relation is None:
        from app.api.v1.media import stream_media_file
        response = await stream_media_file(row["path"])
        response.headers.update(provenance)
        return response
    token = p.media_token(state.unseal(relation["credential"]), relation["relationship_id"],
        state.node["node_id"], row["owner_id"], row["object_id"], row["resource_id"], int(time.time()),
        request_id=request_id_context.get(), trace_id=trace_id_context.get())
    upstream_path = "/internal/v1/media/" + row["object_id"] + "?" + urlencode({"token": token})
    if relation["mode"] == "Direct":
        return RedirectResponse(relation["peer_endpoint"] + upstream_path, status_code=307,
                                headers={**provenance, "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    parsed = urlsplit(p.endpoint(relation["peer_endpoint"]))
    # Only a validated catalog relationship creates this internal Nginx destination.
    internal = f"/_relay_media/{parsed.hostname}/{parsed.port or 443}/{row['object_id']}/{token}"
    return Response(headers={**provenance, "X-Accel-Redirect": internal, "Cache-Control": "no-store"})


async def lyric_entries(identifier: str, path=None):
    row, _relation = await resolve(identifier, path)
    from app.services import lyrics
    try:
        _lyric_path, entries = await lyrics.load_for_media(row["resource_id"])
        return entries
    except FileNotFoundError:
        return []


def item(row):
    payload = row["payload"]
    return {"media_id": row["resource_id"], "resource_id": row["resource_id"], "media_path": row["path"],
            "title": row["path"].rsplit("/", 1)[-1].rsplit(".", 1)[0], "artist": "前沿娱乐", "type": payload["type"],
            "url": "/api/v1/media/stream?" + urlencode({"file_path": row["path"], "resource_id": row["resource_id"]}),
            # The existing cover is a static product icon, not a media attachment.
            "cover": "/favicon.ico", "has_lyrics": payload["has_lyrics"],
            "play_score": payload["play_score"], "preference": payload["preference"]}


async def directory_items(path: str):
    return [item(row) for row in await catalog.resources(directory=path)
            if row["path"].rsplit("/", 1)[0] == path]


async def attach_master_stats(items):
    if not items:
        return items
    rows = {}
    identifiers = [entry["resource_id"] for entry in items]
    async with state.database.connect() as conn:
        for offset in range(0, len(identifiers), 500):
            batch = identifiers[offset:offset + 500]
            placeholders = ",".join(f":i{index}" for index in range(len(batch)))
            result = await conn.execute(text(
                "SELECT media_id, play_score, preference FROM media_playback_stats "
                f"WHERE media_id IN ({placeholders})"
            ), {f"i{index}": value for index, value in enumerate(batch)})
            rows.update({row["media_id"]: dict(row) for row in result.mappings()})
    for entry in items:
        if entry["resource_id"] in rows:
            row = rows[entry["resource_id"]]
            entry.update(play_score=row["play_score"], preference=row["preference"])
    return items


async def mutate_stats(identifier, path, *, preference=None, session=None, played=None, duration=None, audit=None):
    row, _relation = await resolve(identifier, path)
    if preference is not None and (
        isinstance(preference, bool) or not isinstance(preference, int)
        or not playback.MIN_PREFERENCE <= preference <= playback.MAX_PREFERENCE
    ):
        raise p.ProtocolError(
            f"Preference must be between {playback.MIN_PREFERENCE} and {playback.MAX_PREFERENCE}"
        )
    if session is not None:
        session = playback.normalize_session_id(session)
        if played + .05 < playback.valid_playback_threshold(duration):
            raise p.ProtocolError("Playback threshold not reached")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    counted = False
    async with state.database.begin() as conn:
        # Master owns these events; keep cleanup in the same configured DB.
        cleanup_sql = "DELETE FROM media_playback_events WHERE expires_at <= :now"
        if conn.dialect.name != "sqlite":
            cleanup_sql += " LIMIT 1000"
        await conn.execute(text(cleanup_sql), {"now": now})
        owned = (await conn.execute(select(s.global_media.c.media_id).where(
            s.global_media.c.media_id == identifier, s.global_media.c.state == "active").with_for_update(read=True))).scalar_one_or_none()
        if not owned:
            raise p.ProtocolError("Media object no longer available")
        insert_prefix = "INSERT OR IGNORE" if conn.dialect.name == "sqlite" else "INSERT"
        insert_suffix = "" if conn.dialect.name == "sqlite" else " ON DUPLICATE KEY UPDATE media_path=VALUES(media_path)"
        await conn.execute(text(f"""
            {insert_prefix} INTO media_playback_stats
            (media_id, media_path, play_score, preference, created_at, updated_at)
            VALUES (:id, :path, 0, 0, :now, :now)
            {insert_suffix}
        """), {"id": identifier, "path": row["path"], "now": now})
        if conn.dialect.name == "sqlite":
            await conn.execute(text("UPDATE media_playback_stats SET media_path=:path WHERE media_id=:id"),
                               {"path": row["path"], "id": identifier})
        if preference is not None:
            await conn.execute(text("""
                UPDATE media_playback_stats SET preference=:preference, updated_at=:now
                WHERE media_id=:id
            """), {"preference": preference, "now": now, "id": identifier})
        if session is not None:
            event_prefix = "INSERT OR IGNORE" if conn.dialect.name == "sqlite" else "INSERT IGNORE"
            event = text(f"""
                {event_prefix} INTO media_playback_events
                (playback_session_id, media_id, counted_at, expires_at)
                VALUES (:session, :id, :now, :expires)
            """)
            parameters = {"session": session, "id": identifier, "now": now,
                          "expires": now + timedelta(days=playback.PLAYBACK_EVENT_TTL_DAYS)}
            inserted = await conn.execute(event, parameters)
            if inserted.rowcount != 1:
                expired = await conn.execute(text("""
                    DELETE FROM media_playback_events
                    WHERE playback_session_id=:session AND media_id=:id AND expires_at <= :now
                """), parameters)
                if expired.rowcount == 1:
                    inserted = await conn.execute(event, parameters)
            if inserted.rowcount == 1:
                await conn.execute(text("""
                    UPDATE media_playback_stats SET play_score=play_score + 1, updated_at=:now
                    WHERE media_id=:id
                """), {"now": now, "id": identifier})
                counted = True
        lock_suffix = "" if conn.dialect.name == "sqlite" else " FOR UPDATE"
        values = dict((await conn.execute(text("""
            SELECT play_score, preference FROM media_playback_stats
            WHERE media_id=:id
        """ + lock_suffix), {"id": identifier})).mappings().one())
        if audit is not None:
            await audit(conn, "success", 1, {
                "resource_id": identifier,
                "preference": values["preference"],
                "play_score": values["play_score"],
            })
    return {"media_id": identifier, "play_score": values["play_score"], "preference": values["preference"], "counted": counted}
