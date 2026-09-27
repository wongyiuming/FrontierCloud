"""Directory priority and rename semantics for managed media folders.

Directories are logical media objects: their priority is stored in the existing
media_objects/media_playback_stats pair so it follows the same backup/recovery
path as media priorities without introducing a second preference system.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from sqlalchemy import select, text

from app.core.db import engine
from app.services import playback
from app.services.media_catalog_cache import invalidate_media_catalog
from app.services.media_manager import MEDIA_ROOT, MediaManager, ensure_media_mutations_ready, media_mutation_lock
from app.services.federation import schema as s
from app.services.federation.catalog import catalog as node_catalog
from app.services.federation.state import state as node_state
from app.services.federation.transport import transport


MIN_PREFERENCE = playback.MIN_PREFERENCE
MAX_PREFERENCE = playback.MAX_PREFERENCE


def _locator(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def normalize_directory_path(value: str, *, allow_type_root: bool = False) -> str:
    path = str(value or "").replace("\\", "/").strip().strip("/")
    parts = PurePosixPath(path).parts
    minimum = 1 if allow_type_root else 2
    if (
        not path
        or path != "/".join(parts)
        or len(parts) < minimum
        or len(parts) > 3
        or parts[0] not in {"music", "vido"}
        or any(not part or part in {".", ".."} or part.startswith(".") for part in parts)
    ):
        raise ValueError("目录必须位于 music/vido 的分类目录或其一层子目录")
    return path


def validate_directory_name(value: str) -> str:
    name = str(value or "").strip()
    if (
        not name
        or name in {".", ".."}
        or name.startswith(".")
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or len(name) > 255
    ):
        raise ValueError("文件夹名称无效")
    return name


def renamed_path(path: str, new_name: str) -> str:
    old = normalize_directory_path(path)
    name = validate_directory_name(new_name)
    parts = old.split("/")
    return "/".join([*parts[:-1], name])


def _replace_prefix(path: str, old: str, new: str) -> str:
    if path == old:
        return new
    prefix = old + "/"
    if path.startswith(prefix):
        return new + path[len(old):]
    return path


async def preferences_for_paths(paths: list[str], database=engine) -> dict[str, int]:
    normalized = []
    for value in paths:
        try:
            normalized.append(normalize_directory_path(value))
        except ValueError:
            continue
    if not normalized:
        return {}
    locators = [_locator(path) for path in normalized]
    result: dict[str, int] = {}
    async with database.connect() as conn:
        for offset in range(0, len(locators), 500):
            batch = locators[offset:offset + 500]
            placeholders = ",".join(f":p{index}" for index in range(len(batch)))
            params = {f"p{index}": value for index, value in enumerate(batch)}
            rows = (await conn.execute(text(f"""
                SELECT object.media_path, stats.preference
                FROM media_objects AS object
                INNER JOIN media_playback_stats AS stats ON stats.media_id=object.media_id
                WHERE object.object_kind='directory'
                  AND object.path_locator IN ({placeholders})
            """), params)).mappings().all()
            for row in rows:
                path = str(row["media_path"])
                if path in normalized:
                    result[path] = int(row["preference"])
    return result


async def immediate_preferences(scope: str, database=engine) -> list[dict]:
    scope = str(scope or "").replace("\\", "/").strip().strip("/")
    if scope:
        scope = normalize_directory_path(scope, allow_type_root=True)
    prefix = scope + "/" if scope else ""
    depth = len(scope.split("/")) + 1 if scope else 1
    async with database.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT object.media_path, stats.preference
            FROM media_objects AS object
            INNER JOIN media_playback_stats AS stats ON stats.media_id=object.media_id
            WHERE object.object_kind='directory'
        """))).mappings().all()
    result = []
    for row in rows:
        path = str(row["media_path"])
        if path.startswith(prefix) and len(path.split("/")) == depth:
            result.append({"path": path, "preference": int(row["preference"])})
    result.sort(key=lambda item: (-item["preference"], item["path"].casefold()))
    return result


async def set_preference(path: str, value: int, database=engine) -> dict:
    path = normalize_directory_path(path)
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_PREFERENCE <= value <= MAX_PREFERENCE:
        raise ValueError(f"Preference must be between {MIN_PREFERENCE} and {MAX_PREFERENCE}")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    locator = _locator(path)
    async with media_mutation_lock.shared():
        ensure_media_mutations_ready()
        async with database.begin() as conn:
            row = (await conn.execute(text("""
                SELECT media_id, media_path FROM media_objects
                WHERE path_locator=:locator
                FOR UPDATE
            """), {"locator": locator})).mappings().first()
            if row and str(row["media_path"]) != path:
                raise RuntimeError("Directory path locator collision")
            media_id = str(row["media_id"]) if row else secrets.token_hex(32)
            if not row:
                await conn.execute(text("""
                    INSERT INTO media_objects
                    (media_id, object_kind, media_path, path_locator, created_at, updated_at)
                    VALUES (:media_id, 'directory', :path, :locator, :now, :now)
                """), {"media_id": media_id, "path": path, "locator": locator, "now": now})
            await conn.execute(text("""
                INSERT INTO media_playback_stats
                (media_id, media_path, play_score, preference, created_at, updated_at)
                VALUES (:media_id, :path, 0, :preference, :now, :now)
                ON DUPLICATE KEY UPDATE
                    media_path=VALUES(media_path),
                    preference=VALUES(preference),
                    updated_at=VALUES(updated_at)
            """), {"media_id": media_id, "path": path, "preference": value, "now": now})
    await invalidate_media_catalog()
    return {"directory_path": path, "preference": value}


async def sort_directory_entries(entries: list[dict], path_for: Callable[[dict], str], database=engine) -> list[dict]:
    values = [dict(entry) for entry in entries]
    paths = [path_for(entry) for entry in values]
    preferences = await preferences_for_paths(paths, database)
    return sorted(
        values,
        key=lambda entry: (
            -preferences.get(path_for(entry), 0),
            str(entry.get("name") or "").casefold(),
            path_for(entry).casefold(),
        ),
    )


def install_public_priority() -> None:
    """Wrap the existing catalog builders so cached catalogs are still priority-sorted."""
    from app.api.v1 import media

    if getattr(media, "_directory_priority_installed", False):
        return
    original_categories = media.get_media_categories
    original_subcategories = media.get_media_subcategories

    async def prioritized_categories(media_type, valid_exts, *, include_hidden=False):
        entries = await original_categories(media_type, valid_exts, include_hidden=include_hidden)
        root = media._typed_media_root(media_type).name
        return await sort_directory_entries(entries, lambda entry: f"{root}/{entry['name']}")

    async def prioritized_subcategories(media_type, category_subpath, valid_exts, *, include_hidden=False):
        entries = await original_subcategories(
            media_type, category_subpath, valid_exts, include_hidden=include_hidden,
        )
        return await sort_directory_entries(
            entries, lambda entry: f"{category_subpath.rstrip('/')}/{entry['name']}",
        )

    media.get_media_categories = prioritized_categories
    media.get_media_subcategories = prioritized_subcategories
    media._directory_priority_installed = True


async def _target_metadata_conflict(conn, new_path: str) -> bool:
    params = MediaManager._path_params(new_path)
    count = await conn.scalar(text(
        "SELECT COUNT(*) FROM media_objects WHERE "
        + MediaManager._path_scope("media_path", True)
    ), params)
    return bool(count)


async def _rewrite_local_metadata(conn, old_path: str, new_path: str) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    params = MediaManager._path_params(old_path)
    object_rows = (await conn.execute(text(
        "SELECT media_id, media_path FROM media_objects WHERE "
        + MediaManager._path_scope("media_path", True)
        + " ORDER BY media_path"
    ), params)).mappings().all()
    for row in object_rows:
        old_value = str(row["media_path"])
        new_value = _replace_prefix(old_value, old_path, new_path)
        await conn.execute(text("""
            UPDATE media_objects
            SET media_path=:path, path_locator=:locator, updated_at=:now
            WHERE media_id=:media_id
        """), {"path": new_value, "locator": _locator(new_value), "now": now,
                 "media_id": row["media_id"]})

    stat_rows = (await conn.execute(text(
        "SELECT media_id, media_path FROM media_playback_stats WHERE "
        + MediaManager._path_scope("media_path", True)
    ), params)).mappings().all()
    for row in stat_rows:
        new_value = _replace_prefix(str(row["media_path"]), old_path, new_path)
        await conn.execute(text("""
            UPDATE media_playback_stats SET media_path=:path, updated_at=:now
            WHERE media_id=:media_id
        """), {"path": new_value, "now": now, "media_id": row["media_id"]})

    link_rows = (await conn.execute(text(
        "SELECT media_id, media_path FROM media_lyric_links WHERE "
        + MediaManager._path_scope("media_path", True)
    ), params)).mappings().all()
    for row in link_rows:
        new_value = _replace_prefix(str(row["media_path"]), old_path, new_path)
        await conn.execute(text("""
            UPDATE media_lyric_links SET media_path=:path, updated_at=:now
            WHERE media_id=:media_id
        """), {"path": new_value, "now": now, "media_id": row["media_id"]})

    visibility_rows = (await conn.execute(text(
        "SELECT relative_path FROM media_visibility WHERE "
        + MediaManager._path_scope("relative_path", True)
    ), params)).scalars().all()
    for value in visibility_rows:
        old_value = str(value)
        new_value = _replace_prefix(old_value, old_path, new_path)
        await conn.execute(text("""
            UPDATE media_visibility SET relative_path=:new_path, updated_at=:now
            WHERE BINARY relative_path=BINARY :old_path
        """), {"new_path": new_value, "old_path": old_value, "now": now})


async def _rewrite_master_catalog(conn, old_path: str, new_path: str) -> None:
    rows = (await conn.execute(select(s.global_media).where(
        s.global_media.c.media_path.startswith(old_path.rstrip("/") + "/", autoescape=True)
    ).order_by(s.global_media.c.media_path))).mappings().all()
    now = int(time.time())
    for row in rows:
        old_value = str(row["media_path"])
        new_value = _replace_prefix(old_value, old_path, new_path)
        await conn.execute(
            s.global_media.update().where(s.global_media.c.media_id == row["media_id"]).values(
                media_path=new_value,
                path_locator=_locator(new_value),
                updated_at=now,
            )
        )


async def _preflight_master(old_path: str, new_path: str) -> list[dict]:
    rows = await node_catalog.resources(directory=old_path)
    if not rows:
        raise FileNotFoundError(old_path)
    if any(row.get("health") != "online" and row.get("owner_id") != node_state.node.get("node_id") for row in rows):
        raise RuntimeError("目录所在 Follower 当前离线，不能安全改名")
    async with node_state.database.connect() as conn:
        old_prefix = old_path.rstrip("/") + "/"
        new_prefix = new_path.rstrip("/") + "/"
        non_active = int(await conn.scalar(select(text("COUNT(*)")).select_from(s.global_media).where(
            s.global_media.c.media_path.startswith(old_prefix, autoescape=True),
            s.global_media.c.state != "active",
        )) or 0)
        if non_active:
            raise RuntimeError("目录仍有待删除媒体，完成收敛后才能改名")
        conflict = int(await conn.scalar(select(text("COUNT(*)")).select_from(s.global_media).where(
            s.global_media.c.media_path.startswith(new_prefix, autoescape=True),
        )) or 0)
        if conflict:
            raise FileExistsError(new_path)
        pending_upload = int(await conn.scalar(select(text("COUNT(*)")).select_from(s.upload_sessions).where(
            s.upload_sessions.c.state == "reserved",
            (s.upload_sessions.c.media_path.startswith(old_prefix, autoescape=True)
             | s.upload_sessions.c.media_path.startswith(new_prefix, autoescape=True)),
        )) or 0)
        if pending_upload:
            raise RuntimeError("目录存在进行中的上传，完成或取消上传后才能改名")
        if await _target_metadata_conflict(conn, new_path):
            raise FileExistsError(new_path)
    return rows


def _local_directory_paths(old_path: str, new_path: str) -> tuple[Path, Path]:
    source = (MEDIA_ROOT / old_path).resolve()
    target = (MEDIA_ROOT / new_path).resolve()
    if MEDIA_ROOT not in source.parents or MEDIA_ROOT not in target.parents:
        raise ValueError("目录超出媒体根目录")
    return source, target


def _rename_physical_directory(old_path: str, new_path: str) -> None:
    source, target = _local_directory_paths(old_path, new_path)
    if not source.is_dir() or source.is_symlink():
        raise FileNotFoundError(old_path)
    if target.exists():
        raise FileExistsError(new_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)


async def rename_follower_directory(old_path: str, new_path: str, database=None) -> dict:
    """Follower-side atomic directory move invoked only by its paired Master."""
    old_path = normalize_directory_path(old_path)
    new_path = normalize_directory_path(new_path)
    if old_path.rsplit("/", 1)[0] != new_path.rsplit("/", 1)[0]:
        raise ValueError("文件夹改名不能改变父目录")
    database = database or engine
    async with media_mutation_lock:
        ensure_media_mutations_ready()
        async with database.connect() as conn:
            if await _target_metadata_conflict(conn, new_path):
                raise FileExistsError(new_path)
        _rename_physical_directory(old_path, new_path)
        try:
            async with database.begin() as conn:
                await _rewrite_local_metadata(conn, old_path, new_path)
        except BaseException:
            try:
                _rename_physical_directory(new_path, old_path)
            finally:
                raise
    await invalidate_media_catalog()
    return {"status": "renamed", "old_path": old_path, "new_path": new_path}


async def _remote_rename(row: dict, old_path: str, new_path: str) -> None:
    relation = await node_state.relationship(str(row["relationship_id"]))
    if relation.get("state") != "active" or relation.get("status") == "offline":
        raise RuntimeError(f"Follower {row['owner_id']} 当前不可用")
    await transport.request(
        relation["peer_endpoint"],
        "/internal/v1/storage-control/directory-rename",
        method="POST",
        value={"old_path": old_path, "new_path": new_path},
        relation=relation["relationship_id"],
        credential=node_state.unseal(relation["credential"]),
    )


async def rename_directory(path: str, new_name: str) -> dict:
    old_path = normalize_directory_path(path)
    new_path = renamed_path(old_path, new_name)
    if new_path == old_path:
        return {"status": "unchanged", "old_path": old_path, "new_path": new_path}
    role = node_state.node.get("role", "Standalone")
    if role == "Follower":
        raise RuntimeError("Follower 的媒体目录由 Master 统一管理")
    if role != "Master":
        return await rename_follower_directory(old_path, new_path, engine)

    rows = await _preflight_master(old_path, new_path)
    owners: dict[str, dict] = {}
    for row in rows:
        owners.setdefault(str(row["owner_id"]), row)
    moved: list[dict] = []
    try:
        for owner_id, row in owners.items():
            if owner_id == node_state.node.get("node_id"):
                _rename_physical_directory(old_path, new_path)
            else:
                await _remote_rename(row, old_path, new_path)
            moved.append(row)
        async with node_state.database.begin() as conn:
            await _rewrite_master_catalog(conn, old_path, new_path)
            await _rewrite_local_metadata(conn, old_path, new_path)
    except BaseException as original:
        rollback_errors = []
        for row in reversed(moved):
            try:
                if str(row["owner_id"]) == node_state.node.get("node_id"):
                    _rename_physical_directory(new_path, old_path)
                else:
                    await _remote_rename(row, new_path, old_path)
            except Exception as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(
                "目录改名失败且部分存储节点回滚失败：" + "; ".join(rollback_errors)
            ) from original
        raise

    await invalidate_media_catalog()
    return {
        "status": "renamed",
        "old_path": old_path,
        "new_path": new_path,
        "storage_members": len(owners),
        "media_count": len(rows),
    }
