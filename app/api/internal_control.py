"""Current authenticated node controls; software replacement is operator-only."""
from fastapi import APIRouter

from app.api.internal_backup_control import router as backup_control_router
from app.api.internal_media_control import router as media_control_router
from app.api.internal_playback_diagnostics import router as playback_diagnostics_router
from app.api.internal_resource_inventory import router as resource_inventory_router
from app.api.internal_resource_observation import router as resource_observation_router
from app.api.internal_resource_registry import router as resource_registry_router
from app.api.internal_resource_reservations import router as resource_reservations_router
from app.api.internal_resource_transfer import router as resource_transfer_router
from app.api.internal_retirement import router as retirement_router

router = APIRouter(include_in_schema=False)
router.include_router(backup_control_router)
router.include_router(media_control_router)
router.include_router(playback_diagnostics_router)
router.include_router(resource_inventory_router)
router.include_router(resource_observation_router)
router.include_router(resource_registry_router)
router.include_router(resource_reservations_router)
router.include_router(resource_transfer_router)
router.include_router(retirement_router)
