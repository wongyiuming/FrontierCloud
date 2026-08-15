import asyncio
import hashlib
import mimetypes
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable

from fastapi import HTTPException, UploadFile
from sqlalchemy import text
from zipstream import ZIP_STORED, ZipStream

from app.core.config import settings
from app.core.db import engine
BASE_DIR = Path(__file__).resolve().parents[2]
MEDIA_ROOT = (BASE_DIR / "data" / "media").resolve()
AUDIO_EXTS = (".mp3", ".m4a", ".flac", ".wav")
VIDEO_EXTS = (".mp4", ".webm", ".mkv")
ALLOWED_EXTS = set(AUDIO_EXTS + VIDEO_EXTS)

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

        fd, tmp_name = tempfile.mkstemp(prefix=".upload-", suffix=".part", dir=str(target_dir))
        os.close(fd)
        tmp = Path(tmp_name)
        total = 0
        head = b""
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
            # mkstemp creates files as 0600. The Nginx container uses a
            # different unprivileged UID and must be able to read media after
            # FastAPI authorizes an X-Accel-Redirect download.
            tmp.chmod(0o644)
            os.replace(tmp, destination)
            return str(destination.relative_to(MEDIA_ROOT).as_posix())
        finally:
            tmp.unlink(missing_ok=True)

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
        if not destination_dir.is_relative_to(MEDIA_ROOT) or destination_dir == MEDIA_ROOT:
            raise HTTPException(status_code=400, detail="禁止直接向 data/media 根目录上传媒体文件")
        destination_dir.mkdir(parents=True, exist_ok=True)
        return destination_dir, name

    @staticmethod
    async def list_tree(relative_dir: str = "") -> dict:
        rel = MediaManager.normalize_relative(relative_dir) if relative_dir else ""
        current = resolve_safe_path(MEDIA_ROOT, rel)
        if not current.exists() or not current.is_dir():
            raise HTTPException(status_code=404, detail="目录不存在")
        hidden = await MediaManager.hidden_paths()
        items = []
        for entry in sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
            if entry.is_symlink():
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
        now = datetime.utcnow()
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
                    await conn.execute(text("DELETE FROM media_visibility WHERE relative_path=:p OR relative_path LIKE :prefix"), {"p": rel, "prefix": prefix + "%"})

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
    async def delete(paths: list[str]) -> int:
        objects = await MediaManager._collect(paths)
        count = 0
        for rel, p in objects:
            if p.is_dir():
                shutil.rmtree(p)
                async with engine.begin() as conn:
                    await conn.execute(text("DELETE FROM media_visibility WHERE relative_path=:p OR relative_path LIKE :prefix"), {"p": rel, "prefix": rel + "/%"})
            else:
                p.unlink()
            count += 1
        return count

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

        for rel, path in objects:
            if path.is_file():
                add_file(path, rel)
                continue

            for current, directories, filenames in os.walk(path, followlinks=False):
                current_path = Path(current)
                directories[:] = [
                    name
                    for name in directories
                    if not (current_path / name).is_symlink()
                ]
                for filename in filenames:
                    child = current_path / filename
                    add_file(child, child.relative_to(MEDIA_ROOT).as_posix())

        return archive
