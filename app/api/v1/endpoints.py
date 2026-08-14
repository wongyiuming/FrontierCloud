import io
import zipfile
from datetime import datetime
from typing import List

from fastapi import APIRouter, UploadFile, File, Response, Query

from app.core import utils
from app.api.v1.wall import router as wall_router
from app.api.v1.media import router as media_router
from app.api.v1.admin import router as admin_router

router = APIRouter()
router.include_router(wall_router, prefix="/wall", tags=["AnonymousWall"])
router.include_router(media_router, prefix="/media", tags=["MediaCenter"])
router.include_router(admin_router, prefix="/media/admin", tags=["MediaAdmin"])

ARCHIVE_EXTS = ('.zip', '.7z', '.tar', '.gz', '.bz2', '.xz', '.tgz')


@router.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')}


@router.post("/watermark")
async def apply_watermark(files: List[UploadFile] = File(...), text: str = Query("内部资源_请勿外泄")):
    if len(files) == 1:
        file = files[0]
        name = file.filename.lower()
        content = await file.read()
        is_archive = any(name.endswith(ext) for ext in ARCHIVE_EXTS)
        if is_archive:
            current_ext = next(ext for ext in ARCHIVE_EXTS if name.endswith(ext))
            result_zip = utils.process_any_archive(content, text, current_ext)
            return Response(content=result_zip, media_type="application/zip", headers={"Content-Disposition": f"attachment; filename=marked_{file.filename}"})
        _, result_content = utils.dispatch_task((file.filename, content), text)
        if name.endswith(".pdf"):
            m_type = "application/pdf"
        elif name.endswith((".docx", ".doc")):
            m_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        else:
            m_type = "image/jpeg"
        return Response(content=result_content, media_type=m_type, headers={"Content-Disposition": f"attachment; filename=marked_{file.filename}"})

    files_to_process = [(f.filename, await f.read()) for f in files]
    processed_results = utils.run_batch_task(files_to_process, text)
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED) as out_zip:
        for name, data in processed_results:
            out_zip.writestr(name, data)
    return Response(content=zip_buffer.getvalue(), media_type="application/zip", headers={"Content-Disposition": "attachment; filename=batch_results.zip"})
