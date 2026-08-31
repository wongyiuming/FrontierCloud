from datetime import datetime

from fastapi import APIRouter

from app.api.v1.admin import router as admin_router
from app.api.v1.media import router as media_router

router = APIRouter()
router.include_router(media_router, prefix="/media", tags=["MediaCenter"])
router.include_router(admin_router, prefix="/media/admin", tags=["MediaAdmin"])

@router.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
