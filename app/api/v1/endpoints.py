import asyncio
import io
import unicodedata
import zipfile
from datetime import datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile

from app.api.v1.admin import router as admin_router
from app.api.v1.media import router as media_router
from app.core import utils
from app.core.config import settings

router = APIRouter()
router.include_router(media_router, prefix="/media", tags=["MediaCenter"])
router.include_router(admin_router, prefix="/media/admin", tags=["MediaAdmin"])

ARCHIVE_EXTS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tbz2", ".txz", ".tgz", ".zip", ".7z", ".tar")
SUPPORTED_FILE_EXTS = (".jpg", ".jpeg", ".png", ".pdf", ".docx", ".doc")
_WATERMARK_JOB_LIMIT = asyncio.Semaphore(settings.WATERMARK_MAX_CONCURRENT_JOBS)


def _archive_extension(filename: str) -> str | None:
    return next((ext for ext in ARCHIVE_EXTS if filename.endswith(ext)), None)


def _attachment_header(filename: str) -> str:
    clean = str(filename or "download").replace("\r", "").replace("\n", "").replace("\x00", "")
    encoded = quote(clean or "download", safe="")
    return f"attachment; filename=download; filename*=UTF-8''{encoded}"


def _validated_upload_name(filename: str | None) -> str:
    value = str(filename or "").strip()
    if (
        not value
        or len(value) > settings.ADMIN_MAX_FILENAME_LENGTH
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise HTTPException(status_code=400, detail="非法文件名")
    return value


async def _read_upload_limited(upload: UploadFile, remaining_total: int) -> bytes:
    allowed = min(settings.WATERMARK_MAX_UPLOAD_FILE_SIZE, remaining_total)
    if allowed <= 0:
        raise HTTPException(status_code=413, detail="上传文件总大小超过限制")
    declared_size = getattr(upload, "size", None)
    if isinstance(declared_size, int) and declared_size > allowed:
        raise HTTPException(status_code=413, detail="上传文件大小超过限制")

    chunks = []
    total = 0
    while True:
        chunk = await upload.read(min(1024 * 1024, allowed - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > allowed:
            raise HTTPException(status_code=413, detail="上传文件大小超过限制")
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')}


@router.post("/watermark")
async def apply_watermark(
    files: Annotated[list[UploadFile], File()],
    text: Annotated[
        str,
        Query(min_length=1, max_length=settings.WATERMARK_MAX_TEXT_LENGTH),
    ] = "内部资源_请勿外泄",
):
    if not files or len(files) > settings.WATERMARK_MAX_FILES:
        raise HTTPException(status_code=413, detail="上传文件数量超过限制")

    try:
        await asyncio.wait_for(_WATERMARK_JOB_LIMIT.acquire(), timeout=10)
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="水印任务繁忙，请稍后重试") from exc

    try:
        files_to_process = []
        total_size = 0
        for upload in files:
            filename = _validated_upload_name(upload.filename)
            lowered = filename.lower()
            if _archive_extension(lowered) is None and not lowered.endswith(SUPPORTED_FILE_EXTS):
                raise HTTPException(status_code=400, detail="不支持的文件类型")
            content = await _read_upload_limited(
                upload,
                settings.WATERMARK_MAX_UPLOAD_TOTAL_SIZE - total_size,
            )
            total_size += len(content)
            files_to_process.append((filename, content))

        if len(files_to_process) == 1:
            filename, content = files_to_process[0]
            lowered = filename.lower()
            archive_ext = _archive_extension(lowered)
            if archive_ext:
                result = await asyncio.to_thread(utils.process_any_archive, content, text, archive_ext)
                return Response(
                    content=result,
                    media_type="application/zip",
                    headers={"Content-Disposition": _attachment_header(f"marked_{filename}.zip")},
                )

            _, result = await asyncio.to_thread(utils.dispatch_task, (filename, content), text)
            if lowered.endswith(".pdf"):
                media_type = "application/pdf"
            elif lowered.endswith((".docx", ".doc")):
                media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            else:
                media_type = "image/jpeg"
            return Response(
                content=result,
                media_type=media_type,
                headers={"Content-Disposition": _attachment_header(f"marked_{filename}")},
            )

        processed_results = await asyncio.to_thread(utils.run_batch_task, files_to_process, text)
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as out_zip:
            for name, data in processed_results:
                out_zip.writestr(name, data)
        return Response(
            content=zip_buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": _attachment_header("batch_results.zip")},
        )
    except utils.ArchiveLimitError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except utils.ProcessingLimitError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except utils.UnsafeArchiveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        _WATERMARK_JOB_LIMIT.release()
