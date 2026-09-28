from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import urllib.parse
import uuid
import weakref
from collections import OrderedDict
from pathlib import Path


logger = logging.getLogger("frontiercloud.media.audio_compatibility")

# Keep derived playback artifacts under MEDIA_ROOT so the existing internal
# Nginx media alias can serve them with sendfile/Range support. The leading dot
# keeps this cache outside every managed catalog/tree scan.
CACHE_DIRECTORY_NAME = ".browser-audio-cache"
SUPPORTED_VIDEO_SUFFIXES = frozenset({".mp4", ".webm", ".mkv"})
BROWSER_NATIVE_AUDIO_CODECS = {
    ".mp4": frozenset({"aac", "mp3"}),
    ".webm": frozenset({"opus", "vorbis"}),
    ".mkv": frozenset({"aac", "mp3", "opus", "vorbis"}),
}
_DECISION_CACHE_LIMIT = 1024
_decision_cache: OrderedDict[str, Path] = OrderedDict()
_loop_states: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class MediaCompatibilityError(RuntimeError):
    pass


def _source_key(source: Path) -> tuple[str, str]:
    stat = source.stat()
    relative_identity = str(source.resolve())
    path_hash = hashlib.sha256(relative_identity.encode("utf-8")).hexdigest()[:24]
    version = hashlib.sha256(
        f"{relative_identity}\0{stat.st_size}\0{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:24]
    return path_hash, version


def _cache_root(source: Path) -> Path:
    # Real FrontierCloud media is always at .../data/media/<managed path>.
    # Tests may use arbitrary temporary paths, so fall back to a sibling cache.
    parent = source.resolve().parent
    for candidate in (source.resolve(), *source.resolve().parents):
        if candidate.name == "media":
            parent = candidate
            break
    root = parent / CACHE_DIRECTORY_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _decision_get(key: str) -> Path | None:
    result = _decision_cache.get(key)
    if result is not None:
        _decision_cache.move_to_end(key)
    return result


def _decision_put(key: str, path: Path) -> None:
    _decision_cache[key] = path
    _decision_cache.move_to_end(key)
    while len(_decision_cache) > _DECISION_CACHE_LIMIT:
        _decision_cache.popitem(last=False)


def _transcode_gate() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    gate = _loop_states.get(loop)
    if gate is None:
        gate = asyncio.Semaphore(1)
        _loop_states[loop] = gate
    return gate


async def _run_process(*command: str) -> tuple[bytes, bytes]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise MediaCompatibilityError(f"Required media tool is unavailable: {command[0]}") from exc
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[-800:]
        raise MediaCompatibilityError(
            f"{command[0]} exited with {process.returncode}: {detail or 'unknown error'}"
        )
    return stdout, stderr


async def probe_audio_codecs(source: Path) -> tuple[str, ...]:
    stdout, _stderr = await _run_process(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "json",
        str(source),
    )
    try:
        payload = json.loads(stdout.decode("utf-8"))
        codecs = tuple(
            str(stream.get("codec_name") or "").strip().lower()
            for stream in payload.get("streams", [])
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, AttributeError) as exc:
        raise MediaCompatibilityError("ffprobe returned invalid audio metadata") from exc
    return tuple(codec for codec in codecs if codec)


def _transcode_command(source: Path, destination: Path) -> list[str]:
    suffix = source.suffix.lower()
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v?",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        "-c:v",
        "copy",
        "-sn",
        "-dn",
    ]
    if suffix == ".webm":
        command.extend(["-c:a", "libopus", "-b:a", "160k", "-ac", "2", "-ar", "48000"])
    else:
        command.extend(["-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000"])
        if suffix == ".mp4":
            command.extend(["-movflags", "+faststart"])
    command.extend(["-max_muxing_queue_size", "4096", str(destination)])
    return command


async def _build_compatible_copy(source: Path, destination: Path) -> None:
    temporary = destination.with_name(
        f".{destination.stem}.{uuid.uuid4().hex}.tmp{destination.suffix}"
    )
    temporary.unlink(missing_ok=True)
    try:
        await _run_process(*_transcode_command(source, temporary))
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise MediaCompatibilityError("ffmpeg did not create a playback artifact")
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _cleanup_stale_versions(root: Path, path_hash: str, keep: Path) -> None:
    for candidate in root.glob(f"{path_hash}-*"):
        if candidate == keep or not candidate.is_file():
            continue
        try:
            candidate.unlink()
        except OSError:
            logger.warning("Could not remove stale media compatibility cache file")


async def browser_compatible_video(source: Path) -> Path:
    source = source.resolve()
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_VIDEO_SUFFIXES or not source.is_file():
        return source

    try:
        path_hash, version = _source_key(source)
    except OSError:
        return source
    decision_key = f"{path_hash}:{version}"
    remembered = _decision_get(decision_key)
    if remembered is not None and (remembered == source or remembered.is_file()):
        return remembered

    root = _cache_root(source)
    destination = root / f"{path_hash}-{version}{suffix}"
    if destination.is_file():
        _decision_put(decision_key, destination)
        return destination

    async with _transcode_gate():
        remembered = _decision_get(decision_key)
        if remembered is not None and (remembered == source or remembered.is_file()):
            return remembered
        if destination.is_file():
            _decision_put(decision_key, destination)
            return destination
        try:
            codecs = await probe_audio_codecs(source)
            native = BROWSER_NATIVE_AUDIO_CODECS[suffix]
            if not codecs or all(codec in native for codec in codecs):
                _decision_put(decision_key, source)
                return source
            await _build_compatible_copy(source, destination)
            _cleanup_stale_versions(root, path_hash, destination)
            _decision_put(decision_key, destination)
            logger.info(
                "Generated browser audio compatibility artifact",
                extra={"context": {"source": source.name, "audio_codecs": list(codecs)}},
            )
            return destination
        except (MediaCompatibilityError, OSError) as exc:
            logger.warning(
                "Browser audio compatibility fallback failed; serving original media: %s",
                str(exc),
                extra={"context": {"source": source.name}},
            )
            _decision_put(decision_key, source)
            return source


def _rewrite_accel_redirect(response, source: Path, playback: Path, media_root: Path) -> None:
    if playback == source:
        return
    try:
        relative = playback.resolve().relative_to(media_root.resolve()).as_posix()
    except ValueError:
        logger.error("Compatibility artifact escaped media root; original stream retained")
        return
    response.headers["X-Accel-Redirect"] = "/_protected_media/" + urllib.parse.quote(relative, safe="/")
    response.headers["Content-Type"] = mimetypes.guess_type(playback.name)[0] or "application/octet-stream"


def install() -> None:
    from app.api.v1 import media

    original = media._local_stream_response
    if getattr(original, "_frontiercloud_audio_compatibility", False):
        return

    async def compatible_local_stream_response(file_path: str, request):
        response = await original(file_path, request)
        try:
            source = media.resolve_safe_path(media.MEDIA_ROOT, file_path)
            parts = source.relative_to(media.MEDIA_ROOT).parts
            if (
                source.is_file()
                and not source.is_symlink()
                and len(parts) in {3, 4}
                and parts[0] == "vido"
                and source.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES
            ):
                playback = await browser_compatible_video(source)
                _rewrite_accel_redirect(response, source, playback, media.MEDIA_ROOT)
        except (ValueError, OSError):
            # The original stream path remains authoritative. This layer must
            # never turn a valid raw stream into an application error.
            return response
        return response

    compatible_local_stream_response._frontiercloud_audio_compatibility = True
    media._local_stream_response = compatible_local_stream_response
