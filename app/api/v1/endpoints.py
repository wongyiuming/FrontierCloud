from fastapi import APIRouter

from app.api.v1.admin import router as admin_router
from app.api.v1.admin_cluster_integrity import router as cluster_admin_router
from app.api.v1.admin_masterlocal_recovery import router as masterlocal_recovery_router
from app.api.v1.admin_upload_guard import router as upload_guard_router
from app.api.v1.media import router as media_router
from app.api.v1.karaoke import router as karaoke_router
from app.api.v1.karaoke_users import router as karaoke_users_router
from app.api.v1.admin_nodes import router as nodes_router
from app.api.v1.admin_karaoke_users import router as admin_karaoke_users_router
from app.api.internal_storage_integrity import install as install_internal_storage_integrity
from app.services.health import readiness_response


install_internal_storage_integrity()

_ADMIN_OVERRIDE_PATHS = {
    "/tree",
    "/tree/search",
    "/storage-pool",
    "/upload/session",
    "/upload/session/{upload_id}/bytes",
    "/upload/session/{upload_id}/finalize",
    "/upload/item",
    "/hide",
    "/download",
}
_CLUSTER_OVERRIDE_PATHS = {"/storage-pool", "/upload/session", "/upload/item"}
_NODE_OVERRIDE_PATHS = {"/nodes/{identifier}/revoke"}


def _without_paths(source: APIRouter, paths: set[str]) -> APIRouter:
    filtered = APIRouter()
    filtered.routes.extend(
        route for route in source.routes
        if getattr(route, "path", None) not in paths
    )
    return filtered


router = APIRouter()
router.include_router(media_router, prefix="/media", tags=["MediaCenter"])
router.include_router(karaoke_router, prefix="/karaoke", tags=["Karaoke"])
router.include_router(karaoke_users_router, prefix="/karaoke", tags=["KaraokeUsers"])
router.include_router(upload_guard_router, prefix="/media/admin", tags=["MediaAdmin"])
router.include_router(masterlocal_recovery_router, prefix="/media/admin", tags=["MediaAdmin"])
router.include_router(_without_paths(cluster_admin_router, _CLUSTER_OVERRIDE_PATHS), prefix="/media/admin", tags=["MediaAdmin"])
router.include_router(_without_paths(admin_router, _ADMIN_OVERRIDE_PATHS), prefix="/media/admin", tags=["MediaAdmin"])
router.include_router(_without_paths(nodes_router, _NODE_OVERRIDE_PATHS), prefix="/media/admin", tags=["MediaAdmin"])
router.include_router(admin_karaoke_users_router, prefix="/media/admin", tags=["MediaAdmin"])


@router.get("/health")
async def health_check():
    return await readiness_response()
