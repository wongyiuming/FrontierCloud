"""Converge Master media deletion and retire empty directory metadata.

Master media bytes can disappear immediately or after a pending-delete retry.
Directory-level preference/visibility metadata must disappear only after no
active or pending media remains below that logical directory. Cleanup is
idempotent and is also swept by the background retry loop so a transient
metadata failure cannot become a permanent ghost state.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select, text

from app.api.v1 import admin_delete_integrity as deletion
from app.services import resource_pool
from app.services.federation import schema as s


logger = logging.getLogger("frontiercloud.media-delete")


def directory_ancestors(media_path: str) -> list[str]:
    parts = str(media_path or "").replace("\\", "/").strip("/").split("/")
    if len(parts) not in {3, 4} or parts[0] not in {"music", "vido"}:
        return []
    ancestors = ["/".join(parts[:2])]
    if len(parts) == 4:
        ancestors.append("/".join(parts[:3]))
    return list(reversed(ancestors))


def _supported_directory(path: str) -> bool:
    parts = str(path or "").replace("\\", "/").strip("/").split("/")
    return len(parts) in {2, 3} and parts[0] in {"music", "vido"} and all(
        part and part not in {".", ".."} and not part.startswith(".") for part in parts
    )


def _like_prefix(path: str) -> str:
    prefix = path.rstrip("/") + "/"
    return prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"


async def _has_managed_media(conn, directory: str) -> bool:
    prefix = directory.rstrip("/") + "/"
    count = await conn.scalar(
        select(func.count()).select_from(s.global_media).where(
            s.global_media.c.state.in_(("active", "pending_delete")),
            s.global_media.c.media_path.startswith(prefix, autoescape=True),
        )
    )
    return bool(count)


async def _remove_directory_metadata(conn, directory: str) -> bool:
    if await _has_managed_media(conn, directory):
        return False

    ids = list((await conn.execute(text("""
        SELECT media_id
        FROM media_objects
        WHERE object_kind='directory' AND media_path=:path
    """), {"path": directory})).scalars())
    for media_id in ids:
        await conn.execute(text("DELETE FROM media_playback_events WHERE media_id=:id"), {"id": media_id})
        await conn.execute(text("DELETE FROM media_playback_stats WHERE media_id=:id"), {"id": media_id})
        await conn.execute(text("DELETE FROM media_lyric_links WHERE media_id=:id OR lyric_id=:id"), {"id": media_id})
        await conn.execute(text("DELETE FROM media_objects WHERE media_id=:id"), {"id": media_id})

    await conn.execute(text("""
        DELETE FROM media_visibility
        WHERE relative_path=:path
           OR relative_path LIKE :pattern ESCAPE '!'
    """), {"path": directory, "pattern": _like_prefix(directory)})
    return bool(ids)


async def cleanup_empty_ancestors(media_path: str, database) -> int:
    cleaned = 0
    async with database.begin() as conn:
        for directory in directory_ancestors(media_path):
            cleaned += int(await _remove_directory_metadata(conn, directory))
    return cleaned


async def sweep_orphan_directory_metadata(database) -> int:
    async with database.connect() as conn:
        directory_paths = list((await conn.execute(text("""
            SELECT media_path FROM media_objects WHERE object_kind='directory'
        """))).scalars())
        visibility_paths = list((await conn.execute(text("""
            SELECT relative_path FROM media_visibility WHERE hidden=1
        """))).scalars())
    candidates = sorted(
        {str(path) for path in [*directory_paths, *visibility_paths] if _supported_directory(str(path))},
        key=lambda path: (-len(path.split("/")), path.casefold()),
    )
    cleaned = 0
    async with database.begin() as conn:
        for directory in candidates:
            cleaned += int(await _remove_directory_metadata(conn, directory))
    return cleaned


def install() -> None:
    if getattr(deletion, "_directory_convergence_installed", False):
        return

    original_retry = deletion.retry_pending_media

    async def retry_pending_media(media_id: str) -> bool:
        row = await deletion._pending_media(media_id)
        completed = await original_retry(media_id)
        if completed and row:
            try:
                await cleanup_empty_ancestors(str(row["media_path"]), deletion.node_state.database)
            except Exception as exc:
                logger.warning(
                    "directory_metadata_cleanup_deferred media_id=%s path=%s error=%s",
                    media_id, row.get("media_path"), type(exc).__name__,
                )
        return completed

    async def retry_pending_deletes(store, limit: int = 20) -> int:
        if store.node.get("role") != "Master":
            return 0
        async with store.database.connect() as conn:
            ids = list((await conn.execute(select(s.global_media.c.media_id).where(
                s.global_media.c.state == "pending_delete"
            ).limit(limit))).scalars())
        completed = 0
        for media_id in ids:
            try:
                completed += int(await deletion.retry_pending_media(str(media_id)))
            except Exception as exc:
                logger.warning(
                    "pending_media_delete_deferred media_id=%s error=%s",
                    media_id, type(exc).__name__,
                )
        try:
            await sweep_orphan_directory_metadata(store.database)
        except Exception as exc:
            logger.warning("directory_metadata_sweep_deferred error=%s", type(exc).__name__)
        return completed

    deletion.retry_pending_media = retry_pending_media
    resource_pool.retry_pending_deletes = retry_pending_deletes
    deletion._directory_convergence_installed = True
