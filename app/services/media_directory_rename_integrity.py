"""Allow a deleted folder name to be reused after only stale directory metadata remains."""
from __future__ import annotations

from sqlalchemy import text

from app.services import media_directories
from app.services.media_manager import MediaManager


async def _target_rows(conn, new_path: str) -> list[dict]:
    params = MediaManager._path_params(new_path)
    result = await conn.execute(text(
        "SELECT media_id, object_kind, media_path FROM media_objects WHERE "
        + MediaManager._path_scope("media_path", True)
    ), params)
    return [dict(row) for row in result.mappings().all()]


async def target_metadata_conflict(conn, new_path: str) -> bool:
    rows = await _target_rows(conn, new_path)
    return any(str(row.get("object_kind")) != "directory" for row in rows)


async def clear_stale_target_directory_metadata(conn, new_path: str) -> None:
    rows = await _target_rows(conn, new_path)
    if any(str(row.get("object_kind")) != "directory" for row in rows):
        raise FileExistsError(new_path)

    params = MediaManager._path_params(new_path)
    for row in rows:
        media_id = str(row["media_id"])
        await conn.execute(text("DELETE FROM media_playback_events WHERE media_id=:id"), {"id": media_id})
        await conn.execute(text("DELETE FROM media_playback_stats WHERE media_id=:id"), {"id": media_id})
        await conn.execute(
            text("DELETE FROM media_lyric_links WHERE media_id=:id OR lyric_id=:id"),
            {"id": media_id},
        )

    await conn.execute(
        text("DELETE FROM media_playback_stats WHERE " + MediaManager._path_scope("media_path", True)),
        params,
    )
    for column in ("media_path", "lyric_path"):
        await conn.execute(
            text("DELETE FROM media_lyric_links WHERE " + MediaManager._path_scope(column, True)),
            params,
        )
    await conn.execute(
        text("DELETE FROM media_visibility WHERE " + MediaManager._path_scope("relative_path", True)),
        params,
    )
    await conn.execute(text(
        "DELETE FROM media_objects WHERE object_kind='directory' AND "
        + MediaManager._path_scope("media_path", True)
    ), params)


def install() -> None:
    if getattr(media_directories, "_rename_reuse_integrity_installed", False):
        return

    original_rewrite = media_directories._rewrite_local_metadata

    async def rewrite_local_metadata(conn, old_path: str, new_path: str) -> None:
        await clear_stale_target_directory_metadata(conn, new_path)
        await original_rewrite(conn, old_path, new_path)

    media_directories._target_metadata_conflict = target_metadata_conflict
    media_directories._rewrite_local_metadata = rewrite_local_metadata
    media_directories._rename_reuse_integrity_installed = True
