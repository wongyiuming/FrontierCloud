"""Master-owned global media catalog.

Follower files are placements inside this catalog. Followers never publish or
own an independent business catalog.
"""
from __future__ import annotations

import hashlib

from sqlalchemy import func, select, text

from . import protocol as p
from . import schema as s
from .state import State, state
from app.services import s3_schema


class Catalog:
    def __init__(self, store: State = state):
        self.store = store

    def _query(self):
        return select(s.global_media,s.storage_members.c.relationship_id,s.storage_members.c.health,s.storage_members.c.transport,
            s.storage_members.c.member_id.label('_native_id'),s3_schema.members.c.member_id.label('_s3_id'),
            s3_schema.members.c.master_id.label('_s3_master'),s3_schema.objects.c.member_id.label('_s3_owner'),
            s3_schema.objects.c.state.label('_s3_state'),s3_schema.objects.c.object_id.label('_s3_object'),
            s3_schema.objects.c.size_bytes.label('_s3_bytes'),s3_schema.objects.c.etag.label('_s3_etag'))\
            .outerjoin(s.storage_members,s.global_media.c.storage_member_id == s.storage_members.c.member_id)\
            .outerjoin(s3_schema.members,s.global_media.c.storage_member_id == s3_schema.members.c.member_id)\
            .outerjoin(s3_schema.objects,s.global_media.c.media_id == s3_schema.objects.c.media_id)\
            .where(s.global_media.c.state == 'active')

    def _placement(self,row):
        row = dict(row)
        if row['_s3_id'] is not None:
            if (row['_native_id'] is not None or row['_s3_state'] != 'active' or row['_s3_object'] != row['object_id'] or
                row['_s3_owner'] != row['storage_member_id'] or row['_s3_master'] != self.store.node.get('node_id') or
                row['_s3_bytes'] != row['size_bytes'] or row['_s3_etag'] != row['etag']):
                raise p.ProtocolError('S3 资源归属或对象记录不一致')
            row.update(relationship_id=None,health='external',transport='S3')
        elif row['_native_id'] is None:
            return None
        return row

    async def resources(self, directory: str | None = None, root: str | None = None,
                        path: str | None = None) -> list[dict]:
        if self.store.node["role"] != "Master":
            return []
        query = self._query()
        if path is not None:
            query = query.where(
                s.global_media.c.path_locator == hashlib.sha256(path.encode("utf-8")).hexdigest(),
                s.global_media.c.media_path == path,
            )
        elif directory is not None or root is not None:
            prefix = (directory or root).rstrip("/") + "/"
            query = query.where(s.global_media.c.media_path.startswith(prefix, autoescape=True))

        async with self.store.database.connect() as conn:
            media_rows = [placed for row in (await conn.execute(query.order_by(
                s.global_media.c.media_path, s.global_media.c.media_id))).mappings() if (placed := self._placement(row)) is not None]
            identifiers = [row["media_id"] for row in media_rows]
            stats: dict[str, dict] = {}
            linked: set[str] = set()
            for offset in range(0, len(identifiers), 500):
                batch = identifiers[offset:offset + 500]
                placeholders = ",".join(f":i{index}" for index in range(len(batch)))
                parameters = {f"i{index}": value for index, value in enumerate(batch)}
                result = await conn.execute(text(
                    "SELECT media_id, play_score, preference FROM media_playback_stats "
                    f"WHERE media_id IN ({placeholders})"
                ), parameters)
                stats.update({str(row["media_id"]): dict(row) for row in result.mappings()})
                result = await conn.execute(text(
                    "SELECT media_id FROM media_lyric_links "
                    f"WHERE media_id IN ({placeholders})"
                ), parameters)
                linked.update(str(value) for value in result.scalars())

        rows = []
        for row in media_rows:
            score = stats.get(row["media_id"], {})
            rows.append({
                "resource_id": row["media_id"],
                "owner_id": row["storage_member_id"],
                "object_id": row["object_id"],
                "relationship_id": row["relationship_id"],
                "path": row["media_path"],
                "health": row["health"],
                "transport": row["transport"],
                "payload": {
                    "path": row["media_path"], "size": row["size_bytes"], "etag": row["etag"],
                    "updated_at": row["updated_at"], "play_score": int(score.get("play_score", 0)),
                    "preference": int(score.get("preference", 0)),
                    "has_lyrics": row["media_id"] in linked, "type": row["object_kind"],
                },
            })
        return rows

    async def resource(self, identifier: str) -> dict:
        if self.store.node["role"] != "Master" or not p.OBJECT_ID.fullmatch(identifier):
            raise p.ProtocolError("Invalid resource identity")
        async with self.store.database.connect() as conn:
            row = (await conn.execute(self._query().where(
                s.global_media.c.media_id == identifier,
            ))).mappings().first()
        if not row:
            raise p.ProtocolError("Resource not found")
        row = self._placement(row)
        if row is None:
            raise p.ProtocolError('Resource placement not found')
        return {
            "resource_id": row["media_id"], "owner_id": row["storage_member_id"],
            "object_id": row["object_id"], "relationship_id": row["relationship_id"],
            "path": row["media_path"], "health": row["health"], "transport": row["transport"],
            "payload": {"path": row["media_path"], "size": row["size_bytes"], "etag": row["etag"],
                        "updated_at": row["updated_at"], "type": row["object_kind"]},
        }

    async def summary(self) -> dict:
        async with self.store.database.connect() as conn:
            count = int(await conn.scalar(select(func.count()).select_from(s.global_media).where(
                s.global_media.c.state == "active")) or 0)
        return {"media_count": count, "app_version": p.APP_VERSION,
                "protocol": p.PROTOCOL_VERSION}


catalog = Catalog()
