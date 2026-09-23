from fastapi import APIRouter

from app.api.v1.admin import router as admin_router
from app.api.v1.media import router as media_router
from app.api.v1.karaoke import router as karaoke_router
from app.api.v1.karaoke_users import router as karaoke_users_router
from app.api.v1.admin_nodes import router as nodes_router
from app.api.v1.admin_karaoke_users import router as admin_karaoke_users_router
from app.services.health import readiness_response

router = APIRouter()
router.include_router(media_router, prefix="/media", tags=["MediaCenter"])
router.include_router(karaoke_router, prefix="/karaoke", tags=["Karaoke"])
router.include_router(karaoke_users_router, prefix="/karaoke", tags=["KaraokeUsers"])
router.include_router(admin_router, prefix="/media/admin", tags=["MediaAdmin"])
router.include_router(nodes_router, prefix="/media/admin", tags=["MediaAdmin"])
router.include_router(admin_karaoke_users_router, prefix="/media/admin", tags=["MediaAdmin"])

@router.get("/health")
async def health_check():
    return await readiness_response()
