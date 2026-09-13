"""Persistent, paginated object catalog. Paths never establish ownership."""
from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import shutil
import time
from pathlib import PurePosixPath

from sqlalchemy import delete, insert, select, update, text, func

from app.services import media_objects
from app.services.media_manager import media_mutation_lock, ensure_media_mutations_ready
from . import protocol as p
from . import schema as s
from .state import State, state


def valid_payload(payload: dict) -> dict:
    try:
        path = payload["path"]
        parts = PurePosixPath(path).parts
        if (not isinstance(path, str) or len(path) > 1024 or "\\" in path or path.startswith("/")
                or path != "/".join(parts) or any(part.startswith(".") for part in parts)
                or len(parts) not in (3, 4) or parts[0] not in ("music", "vido")):
            raise ValueError()
        extensions = {"music": {".mp3", ".m4a", ".flac", ".wav"}, "vido": {".mp4", ".webm", ".mkv"}}
        if PurePosixPath(path).suffix.lower() not in extensions[parts[0]]:
            raise ValueError()
        score, preference, size = int(payload["play_score"]), int(payload["preference"]), int(payload["size"])
        if score < 0 or score > 2**63 - 1 or not -2 <= preference <= 7 or not 0 <= size <= 2**63 - 1:
            raise ValueError()
        return dict(path=path, size=size, etag=str(payload["etag"])[:128], updated_at=int(payload["updated_at"]),
                    play_score=score, preference=preference, has_lyrics=bool(payload.get("has_lyrics")),
                    type="audio" if parts[0] == "music" else "video")
    except (ValueError, TypeError, KeyError) as exc:
        raise p.ProtocolError("Invalid media catalog entry") from exc


def _inventory(root, hidden):
    # Filesystem metadata only; no hashing or reading hosted media.
    for kind, extensions in (("music", {".mp3", ".m4a", ".flac", ".wav"}), ("vido", {".mp4", ".webm", ".mkv"})):
        base = root / kind
        if not base.exists():
            continue
        for directory in base.iterdir():
            if not directory.is_dir() or directory.is_symlink() or directory.name.startswith("."):
                continue
            for candidate in directory.iterdir():
                candidates = candidate.iterdir() if candidate.is_dir() and not candidate.is_symlink() else (candidate,)
                for file in candidates:
                    if file.is_symlink() or not file.is_file() or file.name.startswith(".") or file.suffix.lower() not in extensions:
                        continue
                    relative = file.relative_to(root).as_posix()
                    if any(relative == entry or relative.startswith(entry + "/") for entry in hidden):
                        continue
                    try:
                        info = file.stat()
                    except FileNotFoundError:
                        continue
                    yield {"media_path": relative, "size": info.st_size, "updated_at": info.st_mtime_ns,
                           "etag": f'"{int(info.st_mtime):x}-{info.st_size:x}"', "type": "audio" if kind == "music" else "video"}


def _next_batch(iterator):
    result = []
    for _ in range(p.PAGE_SIZE):
        try:
            result.append(next(iterator))
        except StopIteration:
            break
    return result


class Catalog:
    def __init__(self, store: State = state):
        self.store = store
        self.scan_lock = asyncio.Lock()
        self.last_scan = 0.0
        self.storage = {}

    async def scan(self, force=False):
        if self.store.node["role"] == "Standalone":
            return
        async with self.scan_lock:
            if not force and time.monotonic() - self.last_scan < 30:
                return
            from app.api.v1.media import MEDIA_ROOT, _hidden_set
            hidden = await _hidden_set()
            seen = set()
            async with media_mutation_lock:
                ensure_media_mutations_ready()
                iterator = _inventory(MEDIA_ROOT, hidden)
                while batch := await asyncio.to_thread(_next_batch, iterator):
                    objects = []
                    for kind in ("audio", "video"):
                        objects.extend(await media_objects.bind_items([item for item in batch if item["type"] == kind], kind))
                    identifiers = [item["media_id"] for item in objects]
                    params = {f"i{n}": value for n, value in enumerate(identifiers)}
                    placeholders = ",".join(":" + name for name in params)
                    async with self.store.database.begin() as conn:
                        identity = await self.store.lock(conn)
                        scores = {row["media_id"]: dict(row) for row in (await conn.execute(text(
                            f"SELECT media_id, play_score, preference FROM media_playback_stats WHERE media_id IN ({placeholders})"), params)).mappings()}
                        lyric_ids = set((await conn.execute(text(
                            f"SELECT media_id FROM media_lyric_links WHERE media_id IN ({placeholders})"), params)).scalars())
                        existing = {row["object_id"]: dict(row) for row in (await conn.execute(select(s.exports).where(s.exports.c.object_id.in_(identifiers)))).mappings()}
                        version = identity["catalog_version"]
                        for item in objects:
                            original = item["media_id"]
                            seen.add(original)
                            score = scores.get(original, {})
                            payload = valid_payload(dict(path=item["media_path"], size=item["size"], etag=item["etag"], updated_at=item["updated_at"],
                                play_score=score.get("play_score", 0), preference=score.get("preference", 0), has_lyrics=original in lyric_ids))
                            fingerprint = hashlib.sha256(p.canonical(payload)).hexdigest()
                            previous = existing.get(original)
                            if previous and previous["fingerprint"] == fingerprint and not previous["deleted"]:
                                continue
                            version += 1
                            values = dict(path=payload["path"], version=version, deleted=0, fingerprint=fingerprint, payload=payload)
                            if previous:
                                await conn.execute(update(s.exports).where(s.exports.c.object_id == original).values(**values))
                            else:
                                await conn.execute(insert(s.exports).values(object_id=original, **values))
                        await conn.execute(update(s.identity).where(s.identity.c.singleton == 1).values(catalog_version=version))
                # Tombstones retain IDs so retries and offline nodes cannot resurrect deletion.
                async with self.store.database.begin() as conn:
                    identity = await self.store.lock(conn)
                    version = identity["catalog_version"]
                    rows = (await conn.execute(select(s.exports.c.object_id).where(s.exports.c.deleted == 0))).scalars()
                    for original in rows:
                        if original not in seen:
                            version += 1
                            await conn.execute(update(s.exports).where(s.exports.c.object_id == original).values(deleted=1, version=version))
                    await conn.execute(update(s.identity).where(s.identity.c.singleton == 1).values(catalog_version=version))
            self.last_scan = time.monotonic()
            disk = await asyncio.to_thread(shutil.disk_usage, MEDIA_ROOT)
            self.storage = {"storage_total": disk.total, "storage_used": disk.used, "storage_free": disk.free}

    async def page(self, cursor: int, head: int | None):
        if cursor < 0 or (head is not None and head < cursor):
            raise p.ProtocolError("Invalid sync cursor")
        await self.scan()
        async with self.store.database.connect() as conn:
            current = (await conn.execute(select(s.identity.c.catalog_version))).scalar_one()
            if head is None:
                head = current
            if head > current:
                raise p.ProtocolError("Catalog head ahead of owner")
            rows = [dict(row) for row in (await conn.execute(select(s.exports).where(
                s.exports.c.version > cursor, s.exports.c.version <= head).order_by(s.exports.c.version).limit(p.PAGE_SIZE))).mappings()]
        next_cursor = rows[-1]["version"] if len(rows) == p.PAGE_SIZE else head
        return {"owner_id": self.store.node["node_id"], "head": head, "cursor": next_cursor,
                "complete": next_cursor == head, "items": [{"object_id": row["object_id"], "version": row["version"],
                    "deleted": bool(row["deleted"]), "payload": row["payload"]} for row in rows]}

    async def apply(self, relation: dict, page: dict, requested_cursor: int):
        try:
            head, cursor = int(page["head"]), int(page["cursor"])
            items = page["items"]
            if (page["owner_id"] != relation["peer_id"] or not requested_cursor <= cursor <= head
                    or not isinstance(items, list) or len(items) > p.PAGE_SIZE
                    or bool(page["complete"]) != (cursor == head)):
                raise ValueError()
            parsed, previous_version = [], requested_cursor
            for item in items:
                version = int(item["version"])
                if not previous_version < version <= cursor or type(item["deleted"]) is not bool:
                    raise ValueError()
                previous_version = version
                parsed.append((p.resource_id(relation["peer_id"], item["object_id"]), item, valid_payload(item["payload"])))
            if not page["complete"] and (not items or cursor == requested_cursor):
                raise ValueError()
        except (KeyError, TypeError, ValueError) as exc:
            raise p.ProtocolError("Invalid, unordered or mismatched catalog page") from exc
        async with self.store.database.begin() as conn:
            await self.store.lock(conn)
            current = (await conn.execute(select(s.relationships).where(s.relationships.c.relationship_id == relation["relationship_id"]))).mappings().one()
            if current["state"] != "active" or current["cursor"] != requested_cursor:
                raise p.ProtocolError("Catalog relationship revoked or cursor changed")
            for identifier, item, payload in parsed:
                existing = (await conn.execute(select(s.catalog.c.version).where(s.catalog.c.resource_id == identifier))).scalar_one_or_none()
                if existing is not None and existing >= item["version"]:
                    continue
                values = dict(owner_id=relation["peer_id"], object_id=item["object_id"], relationship_id=relation["relationship_id"],
                    path=payload["path"], version=item["version"], deleted=int(bool(item["deleted"])), payload=payload)
                if existing is None:
                    await conn.execute(insert(s.catalog).values(resource_id=identifier, **values))
                else:
                    await conn.execute(update(s.catalog).where(s.catalog.c.resource_id == identifier).values(**values))
            await conn.execute(update(s.relationships).where(s.relationships.c.relationship_id == relation["relationship_id"]).values(cursor=cursor))
        return cursor

    async def resources(self, directory: str | None = None, root: str | None = None):
        if self.store.node["role"] != "Master":
            return []
        query = select(s.catalog).join(s.relationships, s.catalog.c.relationship_id == s.relationships.c.relationship_id).where(
            s.catalog.c.deleted == 0, s.relationships.c.state == "active")
        if directory is not None or root is not None:
            prefix = (directory or root).rstrip("/") + "/"
            query = query.where(s.catalog.c.path.startswith(prefix, autoescape=True))
        async with self.store.database.connect() as conn:
            return [dict(row) for row in (await conn.execute(query.order_by(s.catalog.c.path, s.catalog.c.resource_id))).mappings()]

    async def resource(self, identifier: str):
        if self.store.node["role"] != "Master" or not p.OBJECT_ID.fullmatch(identifier):
            raise p.ProtocolError("Invalid resource identity")
        async with self.store.database.connect() as conn:
            row = (await conn.execute(select(s.catalog).where(s.catalog.c.resource_id == identifier, s.catalog.c.deleted == 0))).mappings().first()
        if not row:
            raise p.ProtocolError("Resource not found")
        return dict(row)

    async def summary(self):
        async with self.store.database.connect() as conn:
            count = (await conn.execute(select(func.count()).select_from(s.exports).where(s.exports.c.deleted == 0))).scalar_one()
            # Inventory metadata, not directory traversal per heartbeat.
            version = (await conn.execute(select(s.identity.c.catalog_version))).scalar_one()
        return {"media_count": count, "catalog_version": version, "app_version": p.APP_VERSION,
                "protocol": p.PROTOCOL_VERSION, **self.storage}


catalog = Catalog()
