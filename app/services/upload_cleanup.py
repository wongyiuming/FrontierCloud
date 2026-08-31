import asyncio
import os
import time
from pathlib import Path

from app.core.admin_log import append_admin_log
from app.core.config import settings
from app.services.media_manager import MEDIA_ROOT


UPLOAD_PART_PREFIX = ".upload-"
UPLOAD_PART_SUFFIX = ".part"
MAX_CLEANUP_INTERVAL_SECONDS = 60


def cleanup_stale_upload_parts(
    media_root: Path = MEDIA_ROOT,
    max_age_seconds: int | float | None = None,
) -> tuple[int, int]:
    """Remove abandoned named upload parts that have received no recent writes."""

    max_age = (
        settings.ADMIN_UPLOAD_INACTIVITY_TIMEOUT
        if max_age_seconds is None
        else max_age_seconds
    )
    cutoff = time.time() - max_age
    removed_count = 0
    removed_bytes = 0

    if not media_root.is_dir():
        return removed_count, removed_bytes

    for current, directories, filenames in os.walk(media_root, followlinks=False):
        current_path = Path(current)
        depth = len(current_path.relative_to(media_root).parts)
        directories[:] = [] if depth >= 3 else [
            name
            for name in directories
            if not (current_path / name).is_symlink()
        ]
        for filename in filenames:
            if not (
                filename.startswith(UPLOAD_PART_PREFIX)
                and filename.endswith(UPLOAD_PART_SUFFIX)
            ):
                continue
            candidate = current_path / filename
            try:
                stat_result = candidate.lstat()
                if candidate.is_symlink() or stat_result.st_mtime > cutoff:
                    continue
                candidate.unlink()
            except FileNotFoundError:
                continue
            removed_count += 1
            removed_bytes += stat_result.st_size

    return removed_count, removed_bytes


async def run_stale_upload_cleanup() -> None:
    """Periodically reclaim upload parts left behind by process termination."""

    interval = min(
        MAX_CLEANUP_INTERVAL_SECONDS,
        max(1, settings.ADMIN_UPLOAD_INACTIVITY_TIMEOUT // 2),
    )
    while True:
        await asyncio.sleep(interval)
        try:
            removed_count, removed_bytes = await asyncio.to_thread(
                cleanup_stale_upload_parts,
            )
            if removed_count:
                append_admin_log(
                    "[UPLOAD_CLEANUP] removed "
                    f"{removed_count} stale parts totaling {removed_bytes} bytes"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            append_admin_log(f"[UPLOAD_CLEANUP] cleanup failed: {exc}")
