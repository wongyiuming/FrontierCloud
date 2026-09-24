"""Bounded recording-file operations on a configured Follower."""
from __future__ import annotations

import hashlib
import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import HTTPException, Request

from app.services.federation import protocol as p
from app.services.federation.state import state

ROOT = Path("data/recordings").resolve()
TRAILER_MAGIC = b"FRONTIERCLOUD-KARAOKE-V1"
MAX_METADATA_BYTES = 2 * 1024 * 1024
CHUNK_LIMIT = 1024 * 1024
_write_lock = asyncio.Lock()
_usage_cache: dict[str, int] = {}


def _path(relationship: str, user_id: str, recording_id: str) -> Path:
    if not all(p.IDENTIFIER.fullmatch(value) for value in (relationship, user_id, recording_id)):
        raise HTTPException(404, "Recording not found")
    target = (ROOT / relationship / user_id / f"{recording_id}.bin").resolve()
    if ROOT not in target.parents:
        raise HTTPException(404, "Recording not found")
    return target


def storage_usage(relationship: str) -> int:
    if relationship in _usage_cache:
        return _usage_cache[relationship]
    root = (ROOT / relationship).resolve()
    if not root.exists():
        _usage_cache[relationship] = 0
        return 0
    usage = sum(item.stat().st_size for item in root.rglob("*") if item.is_file())
    _usage_cache[relationship] = usage
    return usage


def parse_trailer(path: Path) -> dict:
    size = path.stat().st_size
    if size < len(TRAILER_MAGIC) + 8:
        return {}
    with path.open("rb") as source:
        source.seek(-(len(TRAILER_MAGIC) + 8), os.SEEK_END)
        suffix = source.read(len(TRAILER_MAGIC) + 8)
        if suffix[8:] != TRAILER_MAGIC:
            return {}
        length = int.from_bytes(suffix[:8], "big")
        if length > MAX_METADATA_BYTES or length + len(TRAILER_MAGIC) + 8 > size:
            return {}
        source.seek(-(len(TRAILER_MAGIC) + 8 + length), os.SEEK_END)
        value = json.loads(source.read(length))
    if not isinstance(value, dict) or value.get("version") != 1:
        return {}
    lyrics = value.get("lyrics")
    if not isinstance(lyrics, list) or len(lyrics) > 10000:
        return {}
    cleaned = []
    for entry in lyrics:
        if (not isinstance(entry, dict) or not isinstance(entry.get("text"), str)
                or len(entry["text"]) > 4000 or not isinstance(entry.get("time"), (int, float))):
            return {}
        cleaned.append({"time": max(0, float(entry["time"])), "text": entry["text"]})
    return {"title": str(value.get("title") or "")[:255], "lyrics": cleaned}


async def capability(token: str, operation: str, recording_id: str) -> tuple[dict, dict]:
    try:
        hint = json.loads(p.decode(token.split(".", 1)[0]))
        relation = await state.relationship(hint["r"])
        value = p.verify_recording_token(state.unseal(relation["credential"]), token, int(time.time()))
        if (state.node["role"] != "Follower" or relation["direction"] != "upstream"
                or relation["state"] != "active" or value["m"] != relation["peer_id"]
                or value["i"] != recording_id
                or (operation == "upload" and value["op"] != "upload")
                or (operation == "read" and value["op"] not in {"stream", "download"})):
            raise p.ProtocolError("Recording relationship mismatch")
        from app.services.federation import schema as fs
        from sqlalchemy import select
        async with state.database.connect() as conn:
            member = (await conn.execute(select(fs.storage_members).where(
                fs.storage_members.c.member_id == state.node["node_id"]))).mappings().first()
        if not member or not member["storage_enabled"]:
            raise p.ProtocolError("Recording storage is disabled")
        relation = {**relation, "allocated_capacity_bytes": int(member["allocated_bytes"])}
        return relation, value
    except (KeyError, TypeError, ValueError, p.ProtocolError) as exc:
        raise HTTPException(401, "Recording capability invalid or expired") from exc


async def receive(request: Request, relation: dict, value: dict) -> dict:
    expected = int(value["size"])
    capacity = int(relation.get("allocated_capacity_bytes") or 0)
    if expected <= 0 or expected > capacity:
        raise HTTPException(413, "Recording exceeds storage allocation")
    local_reserved = False
    if state.node.get("role") == "Follower":
        from sqlalchemy import func, select, update
        from app.services.federation import schema as fs
        from app.services import resource_pool
        async with state.database.begin() as conn:
            member = (await conn.execute(select(fs.storage_members).where(
                fs.storage_members.c.member_id == state.node["node_id"]).with_for_update())).mappings().first()
            logical = (int(member["allocated_bytes"]) - int(member["used_bytes"])
                       - int(member["reserved_bytes"])) if member else 0
            physical = resource_pool.physical_free(ROOT) - resource_pool.PHYSICAL_RESERVE_BYTES
            if (not member or not member["storage_enabled"] or not member["writable"]
                    or min(logical, physical) < expected):
                raise HTTPException(507, "Recording storage node is full")
            await conn.execute(update(fs.storage_members).where(
                fs.storage_members.c.member_id == state.node["node_id"]
            ).values(reserved_bytes=fs.storage_members.c.reserved_bytes + expected,
                     physical_free_bytes=resource_pool.physical_free(ROOT), updated_at=int(time.time())))
            local_reserved = True
    async with _write_lock:
        target = _path(relation["relationship_id"], value["u"], value["i"])
        temporary = target.with_suffix(".part")
        if temporary.exists():
            temporary.unlink()
            _usage_cache.pop(relation["relationship_id"], None)
        if storage_usage(relation["relationship_id"]) + expected > capacity:
            raise HTTPException(507, "Recording storage node is full")
        if target.exists():
            raise HTTPException(409, "Recording already uploaded")
        target.parent.mkdir(parents=True, exist_ok=True)
        written, digest = 0, hashlib.sha256()
        try:
            with temporary.open("xb") as output:
                async for chunk in request.stream():
                    if len(chunk) > CHUNK_LIMIT:
                        for offset in range(0, len(chunk), CHUNK_LIMIT):
                            piece = chunk[offset:offset + CHUNK_LIMIT]
                            written += len(piece); digest.update(piece); output.write(piece)
                    else:
                        written += len(chunk); digest.update(chunk); output.write(chunk)
                    if written > expected:
                        raise HTTPException(413, "Recording body exceeds reserved size")
                output.flush(); os.fsync(output.fileno())
            if written != expected:
                raise HTTPException(400, "Recording body size does not match reservation")
            os.replace(temporary, target)
            _usage_cache[relation["relationship_id"]] = storage_usage(relation["relationship_id"]) + written
            if local_reserved:
                from sqlalchemy import func, update
                from app.services.federation import schema as fs
                from app.services import resource_pool
                async with state.database.begin() as conn:
                    await conn.execute(update(fs.storage_members).where(
                        fs.storage_members.c.member_id == state.node["node_id"]
                    ).values(reserved_bytes=func.greatest(
                        0, fs.storage_members.c.reserved_bytes - expected),
                        used_bytes=fs.storage_members.c.used_bytes + written,
                        physical_free_bytes=resource_pool.physical_free(ROOT), updated_at=int(time.time())))
                local_reserved = False
        except BaseException:
            temporary.unlink(missing_ok=True)
            if local_reserved:
                from sqlalchemy import func, update
                from app.services.federation import schema as fs
                async with state.database.begin() as conn:
                    await conn.execute(update(fs.storage_members).where(
                        fs.storage_members.c.member_id == state.node["node_id"]
                    ).values(reserved_bytes=func.greatest(
                        0, fs.storage_members.c.reserved_bytes - expected), updated_at=int(time.time())))
            raise
    return {"size_bytes": written, "sha256": digest.hexdigest(), "metadata": parse_trailer(target)}


def stat(relationship: str, user_id: str, recording_id: str) -> dict:
    target = _path(relationship, user_id, recording_id)
    if not target.is_file():
        raise HTTPException(404, "Recording not found")
    digest = hashlib.sha256()
    with target.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_LIMIT), b""):
            digest.update(chunk)
    return {"size_bytes": target.stat().st_size, "sha256": digest.hexdigest(), "metadata": parse_trailer(target)}


def remove(relationship: str, user_id: str, recording_id: str) -> int:
    target = _path(relationship, user_id, recording_id)
    size = target.stat().st_size if target.is_file() else 0
    target.unlink(missing_ok=True)
    _usage_cache[relationship] = max(0, storage_usage(relationship) - size)
    return size


def remove_user(relationship: str, user_id: str) -> int:
    root = (ROOT / relationship / user_id).resolve()
    expected_parent = (ROOT / relationship).resolve()
    if root.parent != expected_parent or not p.IDENTIFIER.fullmatch(user_id):
        raise HTTPException(400, "Invalid recording owner")
    removed = 0
    if root.exists():
        for item in root.iterdir():
            if not item.is_file():
                continue
            removed += item.stat().st_size
            item.unlink()
        root.rmdir()
    _usage_cache[relationship] = max(0, storage_usage(relationship) - removed)
    return removed


def protected_redirect(relationship: str, user_id: str, recording_id: str) -> str:
    target = _path(relationship, user_id, recording_id)
    if not target.is_file():
        raise HTTPException(404, "Recording not found")
    return f"/_protected_recordings/{relationship}/{user_id}/{recording_id}.bin"
