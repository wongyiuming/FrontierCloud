import asyncio
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from fastapi import HTTPException, UploadFile
from sqlalchemy import text
from zipstream import ZIP_STORED, ZipStream

from app.core.async_lock import LoopLocalAsyncLock
from app.core.config import settings
from app.core.db import engine
from app.core.admin_log import append_admin_log
from app.services.media_catalog_cache import invalidate_media_catalog

BASE_DIR = Path(__file__).resolve().parents[2]
MEDIA_ROOT = (BASE_DIR / "data" / "media").resolve()
AUDIO_EXTS = (".mp3", ".m4a", ".flac", ".wav")
VIDEO_EXTS = (".mp4", ".webm", ".mkv")
ALLOWED_EXTS = set(AUDIO_EXTS + VIDEO_EXTS)
MEDIA_TYPE_EXTS = {
    "music": set(AUDIO_EXTS),
    "vido": set(VIDEO_EXTS),
}

DELETE_QUARANTINE_PREFIX = ".delete-"
media_mutation_lock = LoopLocalAsyncLock()
_RECOVERY_REQUIRED_ROOTS: set[Path] = set()


def _sync_media_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_media_mutations_ready() -> None:
    if MEDIA_ROOT in _RECOVERY_REQUIRED_ROOTS:
        raise HTTPException(
            status_code=503,
            detail="媒体事务等待恢复，请在数据库恢复后重启 Web 服务",
        )


async def _finish_media_cleanup(task: asyncio.Task) -> None:
    cancellation = None
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            cancellation = exc
            if task.done():
                task.result()
                break
    if cancellation is not None:
        raise cancellation

def resolve_safe_path(base_dir: Path, sub_path: str) -> Path:
    clean = str(sub_path or "").replace("\\", "/").lstrip("/\\")
    target = (base_dir / clean).resolve()
    if not target.is_relative_to(base_dir.resolve()):
        raise ValueError("Invalid path")
    return target

SIGNATURES = {
    ".mp3": lambda b: b.startswith(b"ID3") or (len(b) >= 2 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0),
    ".flac": lambda b: b.startswith(b"fLaC"),
    ".wav": lambda b: len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WAVE",
    ".m4a": lambda b: len(b) >= 12 and b[4:8] == b"ftyp",
    ".mp4": lambda b: len(b) >= 12 and b[4:8] == b"ftyp",
    ".webm": lambda b: b.startswith(bytes.fromhex("1A45DFA3")),
    ".mkv": lambda b: b.startswith(bytes.fromhex("1A45DFA3")),
}


class MediaManager:
    @staticmethod
    def _layout_parts(path: Path) -> tuple[str, ...]:
        try:
            return path.resolve().relative_to(MEDIA_ROOT).parts
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="非法媒体路径") from exc

    @staticmethod
    def _validate_upload_directory(path: Path) -> tuple[str, ...]:
        parts = MediaManager._layout_parts(path)
        if len(parts) not in {2, 3} or parts[0] not in MEDIA_TYPE_EXTS:
            raise HTTPException(
                status_code=400,
                detail="上传目录必须是 music/vido 下的分类目录或其一层子目录",
            )
        return parts

    @staticmethod
    def _validate_media_destination(destination: Path) -> None:
        parts = MediaManager._layout_parts(destination)
        if len(parts) not in {3, 4} or parts[0] not in MEDIA_TYPE_EXTS:
            raise HTTPException(status_code=400, detail="媒体文件目录层级无效")
        if destination.suffix.lower() not in MEDIA_TYPE_EXTS[parts[0]]:
            expected = "音频" if parts[0] == "music" else "视频"
            raise HTTPException(status_code=400, detail=f"{parts[0]} 目录只允许上传{expected}文件")

        category_dir = MEDIA_ROOT / parts[0] / parts[1]
        allowed_exts = MEDIA_TYPE_EXTS[parts[0]]
        direct_media = any(
            child.is_file() and not child.is_symlink() and child.suffix.lower() in allowed_exts
            for child in category_dir.iterdir()
        ) if category_dir.is_dir() else False
        nested_media = any(
            nested.is_file() and not nested.is_symlink() and nested.suffix.lower() in allowed_exts
            for child in category_dir.iterdir()
            if child.is_dir() and not child.is_symlink()
            for nested in child.iterdir()
        ) if category_dir.is_dir() else False
        if len(parts) == 3 and nested_media:
            raise HTTPException(status_code=409, detail="该分类已使用子目录，禁止在分类目录直接上传媒体")
        if len(parts) == 4 and direct_media:
            raise HTTPException(status_code=409, detail="该分类已有直接媒体，禁止再使用子目录存放媒体")

    @staticmethod
    def normalize_relative(value: str) -> str:
        value = (value or "").replace("\\", "/").strip()
        if not value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
            raise HTTPException(status_code=400, detail="非法路径")
        parts = [p for p in value.split("/") if p not in ("", ".")]
        if any(p == ".." or "\x00" in p for p in parts):
            raise HTTPException(status_code=400, detail="非法路径")
        if len(parts) > settings.MEDIA_MAX_DEPTH:
            raise HTTPException(status_code=400, detail="目录层级过深")
        return "/".join(parts)

    @staticmethod
    def validate_name(name: str) -> str:
        name = os.path.basename(name or "").strip()
        if not name or name in {".", ".."} or "\x00" in name:
            raise HTTPException(status_code=400, detail="非法文件名")
        if len(name) > settings.ADMIN_MAX_FILENAME_LENGTH:
            raise HTTPException(status_code=400, detail="文件名过长")
        if name.startswith("."):
            raise HTTPException(status_code=400, detail="禁止使用隐藏文件名")
        return name

    @staticmethod
    def validate_destination_dir(relative_dir: str) -> Path:
        rel = MediaManager.normalize_relative(relative_dir)
        if not rel:
            raise HTTPException(status_code=400, detail="上传失败，媒体文件禁止直接存放在 data/media 根目录，请选择子目录")
        path = resolve_safe_path(MEDIA_ROOT, rel)
        if not path.exists() or not path.is_dir():
            raise HTTPException(status_code=404, detail="目标目录不存在")
        MediaManager._validate_upload_directory(path)
        return path

    @staticmethod
    def _signature_ok(ext: str, head: bytes) -> bool:
        checker = SIGNATURES.get(ext)
        return bool(checker and checker(head))

    @staticmethod
    async def upload_one(upload: UploadFile, target_dir: Path) -> str:
        name = MediaManager.validate_name(upload.filename or "")
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_EXTS:
            raise HTTPException(status_code=400, detail="上传失败，禁止上传非媒体文件")
        if target_dir == MEDIA_ROOT:
            raise HTTPException(status_code=400, detail="上传失败，媒体文件禁止直接存放在 data/media 根目录，请选择子目录")
        destination = target_dir / name
        try:
            destination.resolve().relative_to(MEDIA_ROOT)
        except ValueError:
            raise HTTPException(status_code=400, detail="非法目标路径")
        if destination.exists():
            raise HTTPException(status_code=409, detail="上传失败，目标位置已存在同名文件")
        MediaManager._validate_media_destination(destination)

        # Stage under MEDIA_ROOT so folder uploads do not create persistent
        # directories until their complete payload has passed validation.
        fd, tmp_name = tempfile.mkstemp(prefix=".upload-", suffix=".part", dir=str(MEDIA_ROOT))
        os.close(fd)
        tmp = Path(tmp_name)
        total = 0
        head = b""
        published = False
        created_dirs: list[Path] = []
        try:
            with tmp.open("wb") as out:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    if not head:
                        head = chunk[:4096]
                    total += len(chunk)
                    if total > settings.ADMIN_MAX_UPLOAD_FILE_SIZE:
                        raise HTTPException(status_code=413, detail="上传失败，文件过大")
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            if total == 0 or not MediaManager._signature_ok(ext, head):
                raise HTTPException(status_code=400, detail="上传失败，文件内容不是受支持的媒体格式")
            async with media_mutation_lock:
                ensure_media_mutations_ready()
                if destination.exists():
                    raise HTTPException(status_code=409, detail="上传失败，目标位置已存在同名文件")
                MediaManager._validate_media_destination(destination)
                missing = target_dir
                while missing != MEDIA_ROOT and not missing.exists():
                    created_dirs.append(missing)
                    missing = missing.parent
                target_dir.mkdir(parents=True, exist_ok=True)

                # mkstemp creates files as 0600. The Nginx container uses a
                # different unprivileged UID and must be able to read media.
                tmp.chmod(0o644)
                try:
                    os.link(tmp, destination)
                except FileExistsError as exc:
                    raise HTTPException(
                        status_code=409,
                        detail="上传失败，目标位置已存在同名文件",
                    ) from exc
                published = True
            return str(destination.relative_to(MEDIA_ROOT).as_posix())
        finally:
            tmp.unlink(missing_ok=True)
            if not published:
                for directory in created_dirs:
                    try:
                        directory.rmdir()
                    except (FileNotFoundError, OSError):
                        break

    @staticmethod
    def ensure_download_readable(path: Path) -> None:
        """Repair legacy 0600 uploads before delegating them to Nginx."""
        mode = path.stat().st_mode
        readable_mode = mode | 0o044
        if readable_mode != mode:
            path.chmod(readable_mode)

    @staticmethod
    def folder_upload_target(target_dir: str, relative_path: str) -> tuple[Path, str]:
        rel = MediaManager.normalize_relative(relative_path)
        parts = rel.split("/")
        name = MediaManager.validate_name(parts[-1])
        nested = "/".join(parts[:-1])

        try:
            base = resolve_safe_path(MEDIA_ROOT, target_dir) if target_dir else MEDIA_ROOT
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="非法上传目录") from exc
        if not base.exists() or not base.is_dir():
            raise HTTPException(status_code=404, detail="目标目录不存在")

        destination_dir = (base / nested).resolve() if nested else base.resolve()
        MediaManager._validate_upload_directory(destination_dir)
        MediaManager._validate_media_destination(destination_dir / name)
        return destination_dir, name

    @staticmethod
    async def list_tree(relative_dir: str = "") -> dict:
        rel = MediaManager.normalize_relative(relative_dir) if relative_dir else ""
        current = resolve_safe_path(MEDIA_ROOT, rel)
        if not current.exists() or not current.is_dir():
            raise HTTPException(status_code=404, detail="目录不存在")
        parts = current.relative_to(MEDIA_ROOT).parts
        if parts and (parts[0] not in MEDIA_TYPE_EXTS or len(parts) > 3):
            raise HTTPException(status_code=400, detail="目录不在受支持的媒体层级内")
        hidden = await MediaManager.hidden_paths()
        items = []
        for entry in sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
            if entry.is_symlink() or entry.name.startswith("."):
                continue
            if not parts and (not entry.is_dir() or entry.name not in MEDIA_TYPE_EXTS):
                continue
            if len(parts) == 3 and entry.is_dir():
                continue
            rel_path = entry.relative_to(MEDIA_ROOT).as_posix()
            items.append({
                "name": entry.name,
                "path": rel_path,
                "kind": "directory" if entry.is_dir() else "file",
                "size": entry.stat().st_size if entry.is_file() else None,
                "hidden": rel_path in hidden,
                "media": entry.suffix.lower() in ALLOWED_EXTS,
            })
        return {"path": rel, "items": items}

    @staticmethod
    async def hidden_paths() -> set[str]:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT relative_path FROM media_visibility WHERE hidden=1"))
            return {str(row[0]) for row in result.fetchall()}

    @staticmethod
    async def set_hidden(paths: list[str], hidden: bool) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with media_mutation_lock:
            ensure_media_mutations_ready()
            async with engine.begin() as conn:
                for path in paths:
                    rel = MediaManager.normalize_relative(path)
                    target = resolve_safe_path(MEDIA_ROOT, rel)
                    if not target.is_dir():
                        raise HTTPException(status_code=400, detail="隐藏操作只允许目录")
                    if hidden:
                        await conn.execute(
                            text("INSERT INTO media_visibility(relative_path,hidden,updated_at) VALUES(:p,1,:t) ON DUPLICATE KEY UPDATE hidden=1,updated_at=:t"),
                            {"p": rel, "t": now},
                        )
                    else:
                        prefix = rel + "/"
                        await conn.execute(text("""
                            DELETE FROM media_visibility
                            WHERE BINARY relative_path=BINARY :p
                               OR BINARY LEFT(relative_path, CHAR_LENGTH(:prefix))=BINARY :prefix
                        """), {"p": rel, "prefix": prefix})

    @staticmethod
    async def _collect(paths: Iterable[str]) -> list[tuple[str, Path]]:
        result = []
        seen = set()
        for raw in paths:
            rel = MediaManager.normalize_relative(raw)
            if rel in seen:
                continue
            seen.add(rel)
            p = resolve_safe_path(MEDIA_ROOT, rel)
            if not p.exists() or p.is_symlink():
                raise HTTPException(status_code=404, detail=f"对象不存在: {rel}")
            result.append((rel, p))
        return result

    @staticmethod
    def _deduplicate_objects(objects: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
        selected: list[tuple[str, Path]] = []
        for rel, path in sorted(objects, key=lambda item: len(item[1].parts)):
            if any(path.is_relative_to(parent) for _parent_rel, parent in selected):
                continue
            selected.append((rel, path))
        return selected

    @staticmethod
    async def _delete_metadata(conn, rel: str, is_directory: bool) -> None:
        params = {"p": rel, "prefix": rel + "/"}
        if is_directory:
            event_scope = """
                BINARY stats.media_path=BINARY :p
                OR BINARY LEFT(stats.media_path, CHAR_LENGTH(:prefix))=BINARY :prefix
            """
            stats_scope = """
                BINARY media_path=BINARY :p
                OR BINARY LEFT(media_path, CHAR_LENGTH(:prefix))=BINARY :prefix
            """
        else:
            event_scope = "BINARY stats.media_path=BINARY :p"
            stats_scope = "BINARY media_path=BINARY :p"

        await conn.execute(text(f"""
            DELETE events
            FROM media_playback_events AS events
            INNER JOIN media_playback_stats AS stats ON stats.media_id=events.media_id
            WHERE {event_scope}
        """), params)
        await conn.execute(
            text(f"DELETE FROM media_playback_stats WHERE {stats_scope}"),
            params,
        )
        if is_directory:
            await conn.execute(text("""
                DELETE FROM media_visibility
                WHERE BINARY relative_path=BINARY :p
                   OR BINARY LEFT(relative_path, CHAR_LENGTH(:prefix))=BINARY :prefix
            """), params)

    @staticmethod
    def _restore_staged(quarantine: Path, manifest: list[dict]) -> None:
        failures = []
        for item in reversed(manifest):
            staged = quarantine / item["slot"]
            original = resolve_safe_path(MEDIA_ROOT, item["relative_path"])
            if not staged.exists():
                if not original.exists():
                    failures.append(item["relative_path"])
                continue
            if original.exists():
                failures.append(item["relative_path"])
                continue
            original.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(staged, original)
                _sync_media_directory(original.parent)
                _sync_media_directory(quarantine)
            except OSError:
                failures.append(item["relative_path"])
        if failures:
            raise RuntimeError("Could not restore staged media: " + ", ".join(failures))
        shutil.rmtree(quarantine, ignore_errors=True)

    @staticmethod
    async def _delete_journal(operation_id: str) -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM media_delete_operations WHERE operation_id=:operation_id"),
                {"operation_id": operation_id},
            )

    @staticmethod
    async def _journal_state(operation_id: str) -> str | None:
        async with engine.connect() as conn:
            value = await conn.scalar(
                text("SELECT state FROM media_delete_operations WHERE operation_id=:operation_id"),
                {"operation_id": operation_id},
            )
        return str(value) if value is not None else None

    @staticmethod
    async def _reconcile_failed_delete(operation_id: str, quarantine: Path, manifest: list[dict]) -> None:
        try:
            state = await MediaManager._journal_state(operation_id)
            if state == "pending":
                MediaManager._restore_staged(quarantine, manifest)
                await MediaManager._delete_journal(operation_id)
            elif state == "committed":
                await MediaManager._cleanup_committed_delete(operation_id, quarantine)
            else:
                raise RuntimeError("Media delete commit state is unknown")
        except Exception as exc:
            _RECOVERY_REQUIRED_ROOTS.add(MEDIA_ROOT)
            append_admin_log(f"[MEDIA_DELETE] recovery required operation={operation_id}: {exc}")

    @staticmethod
    async def _cleanup_committed_delete(operation_id: str, quarantine: Path) -> None:
        await invalidate_media_catalog()
        try:
            if quarantine.exists():
                shutil.rmtree(quarantine)
            await MediaManager._delete_journal(operation_id)
        except Exception as exc:
            append_admin_log(f"[MEDIA_DELETE] committed cleanup deferred operation={operation_id}: {exc}")

    @staticmethod
    async def delete(paths: list[str]) -> int:
        async with media_mutation_lock:
            ensure_media_mutations_ready()
            objects = MediaManager._deduplicate_objects(await MediaManager._collect(paths))
            operation_id = uuid.uuid4().hex
            quarantine = MEDIA_ROOT / f"{DELETE_QUARANTINE_PREFIX}{operation_id}"
            manifest = [
                {
                    "relative_path": rel,
                    "slot": str(index),
                    "is_directory": path.is_dir(),
                }
                for index, (rel, path) in enumerate(objects)
            ]
            async with engine.begin() as conn:
                await conn.execute(text("""
                    INSERT INTO media_delete_operations
                    (operation_id, state, manifest, created_at)
                    VALUES (:operation_id, 'pending', :manifest, :created_at)
                """), {
                    "operation_id": operation_id,
                    "manifest": json.dumps(manifest, ensure_ascii=False),
                    "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
                })

            try:
                quarantine.mkdir(mode=0o700)
                _sync_media_directory(MEDIA_ROOT)
                for item, (_rel, original) in zip(manifest, objects, strict=True):
                    os.replace(original, quarantine / item["slot"])
                    _sync_media_directory(original.parent)
                    _sync_media_directory(quarantine)

                async with engine.begin() as conn:
                    for item in manifest:
                        await MediaManager._delete_metadata(
                            conn,
                            item["relative_path"],
                            bool(item["is_directory"]),
                        )
                    await conn.execute(text("""
                        UPDATE media_delete_operations
                        SET state='committed'
                        WHERE operation_id=:operation_id
                    """), {"operation_id": operation_id})
            except BaseException:
                cleanup_task = asyncio.create_task(
                    MediaManager._reconcile_failed_delete(operation_id, quarantine, manifest)
                )
                await _finish_media_cleanup(cleanup_task)
                raise

            cleanup_task = asyncio.create_task(
                MediaManager._cleanup_committed_delete(operation_id, quarantine)
            )
            await _finish_media_cleanup(cleanup_task)
            return len(objects)

    @staticmethod
    async def build_zip_stream(paths: list[str]) -> ZipStream:
        objects = await MediaManager._collect(paths)
        if len(objects) > settings.ADMIN_MAX_DOWNLOAD_ITEMS:
            raise HTTPException(status_code=413, detail="下载对象数量超过限制")

        # Media files are already compressed, so storing them avoids expensive
        # recompression. ZipStream emits members directly to the HTTP response;
        # no archive is materialized in /tmp or held in memory.
        archive = ZipStream(compress_type=ZIP_STORED)
        seen_names: set[str] = set()

        def add_file(path: Path, archive_name: str) -> None:
            if archive_name in seen_names or path.is_symlink() or not path.is_file():
                return
            seen_names.add(archive_name)
            archive.add_path(path, arcname=archive_name)

        def bounded_files(path: Path):
            if path.is_file():
                yield path
                return
            parts = path.relative_to(MEDIA_ROOT).parts
            if not parts or parts[0] not in MEDIA_TYPE_EXTS:
                return
            allowed_exts = MEDIA_TYPE_EXTS[parts[0]]
            max_file_parts = 4
            pending = [path]
            while pending:
                current = pending.pop()
                for child in current.iterdir():
                    if child.is_symlink():
                        continue
                    child_parts = child.relative_to(MEDIA_ROOT).parts
                    if child.is_dir() and len(child_parts) < max_file_parts:
                        pending.append(child)
                    elif (
                        child.is_file()
                        and len(child_parts) in {3, 4}
                        and child.suffix.lower() in allowed_exts
                    ):
                        yield child

        for rel, path in objects:
            if path.is_file():
                add_file(path, rel)
                continue
            for child in bounded_files(path):
                add_file(child, child.relative_to(MEDIA_ROOT).as_posix())

        return archive


async def recover_interrupted_media_deletions() -> None:
    """Finish or roll back journaled deletes left by a terminated worker."""
    async with media_mutation_lock:
        async with engine.connect() as conn:
            result = await conn.execute(text("""
                SELECT operation_id, state, manifest
                FROM media_delete_operations
                ORDER BY created_at, operation_id
            """))
            rows = result.mappings().all()

        for row in rows:
            operation_id = str(row["operation_id"])
            if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
                raise RuntimeError(f"Invalid media recovery operation id: {operation_id!r}")
            try:
                raw_manifest = row["manifest"]
                manifest = raw_manifest if isinstance(raw_manifest, list) else json.loads(str(raw_manifest))
                if not isinstance(manifest, list):
                    raise ValueError("manifest is not a list")
                for item in manifest:
                    if (
                        not isinstance(item, dict)
                        or not isinstance(item.get("relative_path"), str)
                        or not str(item.get("slot", "")).isdigit()
                    ):
                        raise ValueError("manifest entry is invalid")
                    MediaManager.normalize_relative(item["relative_path"])
            except (TypeError, ValueError, json.JSONDecodeError, HTTPException) as exc:
                append_admin_log(
                    f"[MEDIA_DELETE] invalid recovery manifest operation={operation_id}: {exc}"
                )
                raise RuntimeError(f"Invalid media recovery manifest: {operation_id}") from exc

            quarantine = MEDIA_ROOT / f"{DELETE_QUARANTINE_PREFIX}{operation_id}"
            try:
                if str(row["state"]) == "committed":
                    await invalidate_media_catalog()
                    if quarantine.exists():
                        shutil.rmtree(quarantine)
                elif str(row["state"]) == "pending":
                    MediaManager._restore_staged(quarantine, manifest)
                else:
                    raise RuntimeError("Unknown media recovery state")
                await MediaManager._delete_journal(operation_id)
            except Exception as exc:
                append_admin_log(
                    f"[MEDIA_DELETE] recovery deferred operation={operation_id}: {exc}"
                )
                if str(row["state"]) != "committed":
                    _RECOVERY_REQUIRED_ROOTS.add(MEDIA_ROOT)
                    raise RuntimeError(f"Media recovery incomplete: {operation_id}") from exc
        _RECOVERY_REQUIRED_ROOTS.discard(MEDIA_ROOT)
