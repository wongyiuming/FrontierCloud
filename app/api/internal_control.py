"""Current authenticated node controls; software replacement is operator-only."""
from fastapi import APIRouter

from app.api.internal_backup_control import router as backup_control_router
from app.api.internal_media_control import router as media_control_router
from app.api.internal_playback_diagnostics import router as playback_diagnostics_router
from app.api.internal_worker_retirement import install as install_internal_worker_retirement

install_internal_worker_retirement()

router = APIRouter(include_in_schema=False)
router.include_router(backup_control_router)
router.include_router(media_control_router)
router.include_router(playback_diagnostics_router)
