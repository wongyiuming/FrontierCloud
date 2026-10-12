"""Initialize role-dependent business services after startup and role changes."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from app.services import lyrics, media_crypto
from app.services.federation.state import state


BUSINESS_TABLES = (
    "media_visibility", "media_delete_operations", "media_playback_stats",
    "media_playback_events", "media_lyric_links", "media_encryption",
    "global_media_objects", "cluster_upload_sessions", "cluster_worker_jobs",
    "cluster_business_backups", "cluster_business_backup_chunks",
    "karaoke_users", "karaoke_recordings", "karaoke_audit_log",
    "karaoke_registration_daily",
)


def ensure_follower_business_files_empty(media_root=None, recordings_root=None) -> None:
    """Only an actual immutable system fallback LRC is exempt from admission."""
    from app.services import karaoke_storage
    from app.services.federation import protocol as p

    media_root = Path(media_root if media_root is not None else lyrics.MEDIA_ROOT)
    recordings_root = Path(recordings_root if recordings_root is not None else karaoke_storage.ROOT)
    from app.services import media_publication_recovery
    if media_publication_recovery.pending(media_root):
        raise p.ProtocolError("Standalone 仍有待恢复的媒体发布；请先恢复业务部署，不能固定为 Follower")
    for root in (media_root, recordings_root):
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise p.ProtocolError("Standalone 业务目录无法安全检查；禁止固定为 Follower")
        if not root.exists():
            continue
        for item in root.rglob("*"):
            if item.is_symlink():
                raise p.ProtocolError("Standalone 仍有业务符号链接；禁止固定为 Follower")
            if not item.is_file():
                if item.is_dir():
                    continue
                raise p.ProtocolError("Standalone 业务目录存在未知文件类型；禁止固定为 Follower")
            if root == media_root and item.relative_to(root).as_posix() == lyrics.DEFAULT_LYRIC_PATH:
                expected = lyrics.DEFAULT_LYRIC_CONTENT.encode("utf-8")
                if item.stat().st_size == len(expected) and item.read_bytes() == expected:
                    continue
            raise p.ProtocolError("Standalone 仍有媒体、歌词或录音；请先迁移，不能直接固定为 Follower")


async def ensure_follower_business_empty(database=None, *, conn=None, media_root=None,
                                       recordings_root=None) -> None:
    """Use one read-only business-data contract for UI and native role admission.

    Node identity, key caches and ordinary control/security audits are retained.
    Business metadata and audit history remain business data even without files.
    Schema errors are never evidence of an empty deployment.
    """
    from app.core import db
    from app.services.federation import protocol as p

    ensure_follower_business_files_empty(media_root, recordings_root)
    if conn is None:
        async with (database if database is not None else state.database).connect() as connection:
            await ensure_follower_business_empty(conn=connection, media_root=media_root,
                                                 recordings_root=recordings_root)
        return
    await db._validate_initialized_schema(conn, await db._table_names(conn))
    for table in BUSINESS_TABLES:
        if int(await conn.scalar(text(f"SELECT COUNT(*) FROM {table}")) or 0):
            raise p.ProtocolError("Standalone 仍有业务数据；请先迁移，不能直接固定为 Follower")
    sqlite = conn.dialect.name == "sqlite"
    equal = "media_path=:fallback" if sqlite else "BINARY media_path=BINARY :fallback"
    kind = "object_kind='lyric'" if sqlite else "BINARY object_kind=BINARY 'lyric'"
    if int(await conn.scalar(text(
        f"SELECT COUNT(*) FROM media_objects WHERE NOT ({kind} AND {equal})"
    ), {"fallback": lyrics.DEFAULT_LYRIC_PATH}) or 0):
        raise p.ProtocolError("Standalone 仍有媒体或歌词元数据；请先迁移，不能直接固定为 Follower")


async def initialize_business_services() -> None:
    if state.node.get("role") == "Follower":
        return
    await media_crypto.initialize()
    await lyrics.initialize_default_lyrics()
