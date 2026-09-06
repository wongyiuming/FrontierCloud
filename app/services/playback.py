from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.admin_log import append_admin_log
from app.core.db import engine
from app.services.media_manager import ensure_media_mutations_ready, media_mutation_lock


PLAYBACK_EVENT_TTL_DAYS = 7
MIN_PREFERENCE = -2
MAX_PREFERENCE = 7


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_session_id(value: str | None) -> str:
    if not value:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Invalid playback session") from exc


def media_id_for_path(relative_path: str) -> str:
    normalized = str(relative_path).replace("\\", "/").lstrip("/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def valid_playback_threshold(duration: float) -> float:
    if duration <= 0:
        raise ValueError("Invalid media duration")
    return max(5.0, min(30.0, duration * 0.5))


def stable_random_key(session_id: str, media_id: str) -> str:
    return hashlib.sha256(f"{session_id}:{media_id}".encode("utf-8")).hexdigest()


def sort_media(items: list[dict[str, Any]], session_id: str) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            -int(item.get("preference", 0)),
            int(item.get("play_score", 0)),
            stable_random_key(session_id, str(item["media_id"])),
        ),
    )


async def attach_stats_and_sort(items: Iterable[dict[str, Any]], session_id: str) -> list[dict[str, Any]]:
    enriched = [dict(item) for item in items]
    media_ids = [str(item["media_id"]) for item in enriched]
    stats: dict[str, dict[str, int]] = {}
    if media_ids:
        placeholders = ", ".join(f":media_{index}" for index in range(len(media_ids)))
        params = {f"media_{index}": media_id for index, media_id in enumerate(media_ids)}
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT media_id, play_score, preference FROM media_playback_stats "
                    f"WHERE media_id IN ({placeholders})"
                ),
                params,
            )
        stats = {
            str(row["media_id"]): {
                "play_score": int(row["play_score"]),
                "preference": int(row["preference"]),
            }
            for row in result.mappings().all()
        }
    for item in enriched:
        item.update(stats.get(str(item["media_id"]), {"play_score": 0, "preference": 0}))
    return sort_media(enriched, session_id)


def validate_media_path(media_root: Path, relative_path: str) -> tuple[str, str]:
    normalized = str(relative_path or "").replace("\\", "/").lstrip("/")
    target = (media_root / normalized).resolve()
    if not normalized or not target.is_relative_to(media_root.resolve()) or target.is_symlink() or not target.is_file():
        raise ValueError("Invalid media path")
    return normalized, media_id_for_path(normalized)


async def _cleanup_expired_events(now: datetime) -> None:
    """Keep housekeeping failures outside the playback accounting transaction."""
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM media_playback_events WHERE expires_at <= :now LIMIT 1000"),
                {"now": now},
            )
    except SQLAlchemyError as exc:
        append_admin_log(f"[PLAYBACK] expired-event cleanup deferred: {exc}")


async def record_playback(
    media_root: Path,
    relative_path: str,
    session_id: str,
    played_seconds: float,
    duration: float,
) -> dict[str, Any]:
    normalized_session = normalize_session_id(session_id)
    threshold = valid_playback_threshold(duration)
    if played_seconds + 0.05 < threshold:
        raise ValueError("Playback threshold not reached")
    now = _utcnow()
    expires_at = now + timedelta(days=PLAYBACK_EVENT_TTL_DAYS)
    counted = False
    await _cleanup_expired_events(now)
    async with media_mutation_lock:
        ensure_media_mutations_ready()
        normalized_path, media_id = validate_media_path(media_root, relative_path)
        async with engine.begin() as conn:
            await conn.execute(
                text("""
                    DELETE FROM media_playback_events
                    WHERE playback_session_id=:session_id
                      AND media_id=:media_id
                      AND expires_at <= :now
                """),
                {"session_id": normalized_session, "media_id": media_id, "now": now},
            )
            inserted = await conn.execute(
                text("""
                    INSERT IGNORE INTO media_playback_events
                    (playback_session_id, media_id, counted_at, expires_at)
                    VALUES (:session_id, :media_id, :counted_at, :expires_at)
                """),
                {
                    "session_id": normalized_session,
                    "media_id": media_id,
                    "counted_at": now,
                    "expires_at": expires_at,
                },
            )
            if inserted.rowcount == 1:
                counted = True
                await conn.execute(
                    text("""
                        INSERT INTO media_playback_stats
                        (media_id, media_path, play_score, preference, created_at, updated_at)
                        VALUES (:media_id, :media_path, 1, 0, :now, :now)
                        ON DUPLICATE KEY UPDATE
                            media_path=VALUES(media_path),
                            play_score=play_score + 1,
                            updated_at=VALUES(updated_at)
                    """),
                    {"media_id": media_id, "media_path": normalized_path, "now": now},
                )
            result = await conn.execute(
                text("SELECT play_score, preference FROM media_playback_stats WHERE media_id=:media_id"),
                {"media_id": media_id},
            )
            row = result.mappings().first()
    return {
        "media_id": media_id,
        "counted": counted,
        "play_score": int(row["play_score"]) if row else 0,
        "preference": int(row["preference"]) if row else 0,
        "threshold_seconds": threshold,
    }


async def change_preference(media_root: Path, relative_path: str, delta: int) -> dict[str, Any]:
    if delta not in {-1, 1}:
        raise ValueError("Preference delta must be -1 or 1")
    now = _utcnow()
    async with media_mutation_lock:
        ensure_media_mutations_ready()
        normalized_path, media_id = validate_media_path(media_root, relative_path)
        async with engine.begin() as conn:
            await conn.execute(
                text("""
                    INSERT INTO media_playback_stats
                    (media_id, media_path, play_score, preference, created_at, updated_at)
                    VALUES (:media_id, :media_path, 0, :delta, :now, :now)
                    ON DUPLICATE KEY UPDATE
                        media_path=VALUES(media_path),
                        preference=LEAST(:maximum, GREATEST(:minimum, preference + :delta)),
                        updated_at=VALUES(updated_at)
                """),
                {
                    "media_id": media_id,
                    "media_path": normalized_path,
                    "delta": delta,
                    "minimum": MIN_PREFERENCE,
                    "maximum": MAX_PREFERENCE,
                    "now": now,
                },
            )
            result = await conn.execute(
                text("SELECT play_score, preference FROM media_playback_stats WHERE media_id=:media_id"),
                {"media_id": media_id},
            )
            row = result.mappings().first()
    return {
        "media_id": media_id,
        "play_score": int(row["play_score"]),
        "preference": int(row["preference"]),
    }
