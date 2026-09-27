"""Safety boundary for bulk same-name lyric association.

Automatic association is a fallback helper. It may replace the system default
fallback, but it must never overwrite a user-selected business lyric. It also
ignores files outside the supported media/lyric hierarchy so historical orphan
files cannot re-enter managed business state through auto-link.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import text

from app.services import lyrics
from app.services.federation.state import state as node_state


TRACK_FILE_DEPTHS = frozenset({3, 4})
LYRIC_FILE_DEPTHS = frozenset({2, 3, 4})


def _supported_local_track_paths() -> list[str]:
    result: list[str] = []
    for path in lyrics.MUSIC_ROOT.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.name.startswith("."):
            continue
        relative = path.relative_to(lyrics.MEDIA_ROOT)
        if (
            len(relative.parts) in TRACK_FILE_DEPTHS
            and relative.parts[0] == "music"
            and path.suffix.lower() in lyrics.AUDIO_EXTS
        ):
            result.append(relative.as_posix())
    return sorted(result)


def _supported_lyric_paths() -> list[str]:
    result: list[str] = []
    for path in lyrics.LYRICS_ROOT.rglob("*.lrc"):
        if not path.is_file() or path.is_symlink() or path.name.startswith("."):
            continue
        relative = path.relative_to(lyrics.MEDIA_ROOT)
        normalized = relative.as_posix()
        if (
            len(relative.parts) in LYRIC_FILE_DEPTHS
            and relative.parts[0] == "lyrics"
            and normalized != lyrics.DEFAULT_LYRIC_PATH
        ):
            result.append(normalized)
    return sorted(result)


def _supported_track_path(path: str) -> bool:
    relative = Path(str(path or ""))
    return (
        len(relative.parts) in TRACK_FILE_DEPTHS
        and relative.parts[0] == "music"
        and relative.suffix.lower() in lyrics.AUDIO_EXTS
    )


async def auto_relate_matching_names(*, audit=None) -> dict[str, int]:
    """Fill missing/default lyric links without replacing explicit user links."""
    await asyncio.to_thread(lyrics.ensure_default_lyric_file)
    lyric_paths = _supported_lyric_paths()

    if node_state.node.get("role") == "Master":
        async with lyrics.engine.connect() as conn:
            result = await conn.execute(text("""
                SELECT media_path FROM global_media_objects
                WHERE object_kind='audio' AND state='active'
                ORDER BY media_path
            """))
            track_paths = [
                str(row["media_path"])
                for row in result.mappings().all()
                if _supported_track_path(str(row["media_path"]))
            ]
    else:
        track_paths = _supported_local_track_paths()

    pairs, ambiguous, unmatched = lyrics.matching_lyric_pairs(track_paths, lyric_paths)

    from app.services.media_manager import ensure_media_mutations_ready, media_mutation_lock

    linked = 0
    preserved = 0
    async with media_mutation_lock:
        ensure_media_mutations_ready()
        now = lyrics._utcnow()
        async with lyrics.engine.begin() as conn:
            for track_path, lyric_path in pairs:
                track_path, media_id = await lyrics._track_identity(conn, track_path)
                current = await conn.scalar(text("""
                    SELECT lyric_path FROM media_lyric_links
                    WHERE media_id=:media_id
                    FOR UPDATE
                """), {"media_id": media_id})
                # Any non-default relation is an explicit business choice. Even
                # if its file is temporarily unavailable, auto-link must not
                # silently reinterpret that state as permission to overwrite it.
                if current and str(current) != lyrics.DEFAULT_LYRIC_PATH:
                    preserved += 1
                    continue
                lyric_id = await lyrics.media_objects.ensure_object(conn, lyric_path, "lyric")
                await lyrics._upsert_relation(
                    conn, media_id, track_path, lyric_id, lyric_path, now,
                )
                linked += 1
            if audit is not None:
                await audit(conn, "success", linked, {
                    "preserved": preserved,
                    "ambiguous": ambiguous,
                    "unmatched": unmatched,
                })
    return {
        "linked": linked,
        "preserved": preserved,
        "ambiguous": ambiguous,
        "unmatched": unmatched,
    }


def install() -> None:
    if getattr(lyrics, "_safe_auto_link_installed", False):
        return
    lyrics.auto_relate_matching_names = auto_relate_matching_names
    lyrics._safe_auto_link_installed = True
