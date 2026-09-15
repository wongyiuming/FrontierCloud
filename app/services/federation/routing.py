"""Resolve an owner once; media and existing attachments stay with that owner."""
from __future__ import annotations

import time
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert

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
        relation = await state.relationship(row["relationship_id"])
    except p.ProtocolError as exc:
        raise HTTPException(404, "Media object not found") from exc
    if relation["state"] != "active":
        raise HTTPException(404, "Media object not found")
    if relation["status"] == "offline" or int(time.time()) - relation["last_heartbeat"] >= p.OFFLINE_SECONDS:
        raise HTTPException(503, "Media temporarily unavailable", headers={"Retry-After": "30", "Cache-Control": "no-store"})
    return row, relation


async def stream(identifier: str, path=None):
    row, relation = await resolve(identifier, path)
    provenance = {"X-Media-Resource-ID": row["resource_id"], "X-Media-Owner-ID": row["owner_id"],
                  "X-Media-Object-ID": row["object_id"]}
    token = p.media_token(state.unseal(relation["credential"]), relation["relationship_id"],
        state.node["node_id"], row["owner_id"], row["object_id"], int(time.time()),
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
    row, relation = await resolve(identifier, path)
    # No path lookup, local fallback, or cross-owner Catalog attachment join.
    result = await runtime.call(relation, "/internal/v1/lyrics/" + row["object_id"])
    entries = result.get("entries")
    if not isinstance(entries, list) or len(entries) > 10000:
        raise HTTPException(502, "Invalid owner lyrics response")
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("text"), str) or len(entry["text"]) > 4000:
            raise HTTPException(502, "Invalid owner lyrics response")
    return entries


def item(row):
    payload = row["payload"]
    return {"media_id": row["resource_id"], "resource_id": row["resource_id"], "media_path": row["path"],
            "title": row["path"].rsplit("/", 1)[-1].rsplit(".", 1)[0], "artist": "前沿视界", "type": payload["type"],
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
    async with state.database.connect() as conn:
        rows = {row["resource_id"]: dict(row) for row in (await conn.execute(select(s.stats).where(
            s.stats.c.resource_id.in_([entry["resource_id"] for entry in items])))).mappings()}
    for entry in items:
        if entry["resource_id"] in rows:
            row = rows[entry["resource_id"]]
            entry.update(play_score=row["play_score"], preference=row["preference"])
    # If no Master record exists, the sole owner's catalog is the fallback.
    return items


async def mutate_stats(identifier, path, *, delta=None, session=None, played=None, duration=None):
    row, _relation = await resolve(identifier, path)
    if delta is not None and delta not in (-1, 1):
        raise p.ProtocolError("Preference delta must be -1 or 1")
    if session is not None:
        session = playback.normalize_session_id(session)
        if played + .05 < playback.valid_playback_threshold(duration):
            raise p.ProtocolError("Playback threshold not reached")
    now, counted = int(time.time()), False
    async with state.database.begin() as conn:
        # Shared trust protects revocation without serializing unrelated resources.
        current_relation = (await conn.execute(select(s.relationships.c.state).where(
            s.relationships.c.relationship_id == row["relationship_id"]).with_for_update(read=True))).scalar_one_or_none()
        if current_relation != "active":
            raise p.ProtocolError("Relationship revoked")
        owned = (await conn.execute(select(s.catalog.c.payload).where(s.catalog.c.resource_id == identifier,
            s.catalog.c.relationship_id == row["relationship_id"], s.catalog.c.deleted == 0).with_for_update(read=True))).scalar_one_or_none()
        if owned is None:
            raise p.ProtocolError("Media object no longer available")
        # A unique-key upsert acquires just this resource's row, including first use.
        await conn.execute(mysql_insert(s.stats).values(resource_id=identifier,
            play_score=owned["play_score"], preference=owned["preference"], updated_at=now)
            .on_duplicate_key_update(resource_id=identifier))
        values = dict((await conn.execute(select(s.stats).where(
            s.stats.c.resource_id == identifier).with_for_update())).mappings().one())
        if delta is not None:
            values["preference"] = max(-2, min(7, values["preference"] + delta))
        if session is not None:
            event = mysql_insert(s.events).values(session_id=session, resource_id=identifier,
                expires_at=now + 604800).prefix_with("IGNORE")
            inserted = await conn.execute(event)
            if inserted.rowcount != 1:
                expired = await conn.execute(delete(s.events).where(s.events.c.session_id == session,
                    s.events.c.resource_id == identifier, s.events.c.expires_at <= now))
                if expired.rowcount == 1:
                    inserted = await conn.execute(event)
            if inserted.rowcount == 1:
                values["play_score"] += 1
                counted = True
        values["updated_at"] = now
        await conn.execute(update(s.stats).where(s.stats.c.resource_id == identifier).values(**values))
    return {"media_id": identifier, "play_score": values["play_score"], "preference": values["preference"], "counted": counted}
