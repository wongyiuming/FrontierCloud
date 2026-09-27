from fastapi import APIRouter, Depends

from app.api.v1.admin import router as admin_router
from app.api.v1.admin_cluster_integrity import router as cluster_admin_router
from app.api.v1.admin_delete_integrity import router as delete_integrity_router
from app.api.v1.admin_directories import router as admin_directories_router
from app.api.v1.admin_karaoke_integrity import router as admin_karaoke_integrity_router
from app.api.v1.admin_master_mutation_integrity import (
    install as install_master_mutation_integrity,
    router as master_mutation_router,
)
from app.api.v1.admin_masterlocal_recovery import router as masterlocal_recovery_router
from app.api.v1.admin_node_observability import router as node_observability_router
from app.api.v1.admin_page_integrity import router as page_integrity_router
from app.api.v1.admin_transport import require_secure_admin_transport
from app.api.v1.admin_upload_guard import router as upload_guard_router
from app.api.v1.admin_site import router as site_admin_router
from app.api.v1.brand import (
    admin_router as brand_admin_router,
    public_router as brand_public_router,
    upload_router as brand_upload_router,
)
from app.api.v1.media import router as media_router
from app.api.v1.karaoke import router as karaoke_router
from app.api.v1.karaoke_integrity import router as karaoke_integrity_router
from app.api.v1.karaoke_users import router as karaoke_users_router
from app.api.v1.admin_nodes import router as nodes_router
from app.api.v1.admin_karaoke_users import router as admin_karaoke_users_router
from app.api.internal_storage_integrity import install as install_internal_storage_integrity
from app.services.federation_mode_integrity import install as install_federation_mode_integrity
from app.services.health import readiness_response
from app.services.lyrics_auto_link_integrity import install as install_lyrics_auto_link_integrity
from app.services.lyrics_directory_counts import install as install_lyrics_directory_counts
from app.services.lyrics_hierarchy_integrity import install as install_lyrics_hierarchy_integrity
from app.services.media_delete_convergence import install as install_media_delete_convergence
from app.services.media_directory_catalog import install_public_priority
from app.services.media_directory_rename_integrity import install as install_media_directory_rename_integrity
from app.services.media_visibility_integrity import install as install_media_visibility_integrity
from app.services.player_directory_label_integrity import install as install_player_directory_label_integrity


install_internal_storage_integrity()
install_federation_mode_integrity()
install_lyrics_hierarchy_integrity()
install_lyrics_directory_counts()
install_lyrics_auto_link_integrity()
install_media_delete_convergence()
install_media_directory_rename_integrity()
install_media_visibility_integrity()
install_master_mutation_integrity()
install_player_directory_label_integrity()
install_public_priority()

_ADMIN_OVERRIDE_PATHS = {
    "",
    "/",
    "/tree",
    "/tree/search",
    "/storage-pool",
    "/upload/session",
    "/upload/session/{upload_id}/bytes",
    "/upload/session/{upload_id}/finalize",
    "/upload/item",
    "/upload/lyric",
    "/delete",
    "/hide",
    "/download",
}
_CLUSTER_OVERRIDE_PATHS = {"/storage-pool", "/upload/session", "/upload/item", "/hide"}
_MASTERLOCAL_OVERRIDE_PATHS = {"/upload/session"}
_DELETE_OVERRIDE_PATHS = {"/delete"}
_NODE_OVERRIDE_PATHS = {"/nodes/{identifier}/revoke"}
_KARAOKE_OVERRIDE_PATHS = {
    "/account/status",
    "/account/recordings",
    "/account/recordings/ticket",
    "/account/recordings/{recording_id}/pending",
    "/account/recordings/{recording_id}",
    "/account",
}
_ADMIN_KARAOKE_OVERRIDE_PATHS = {"/users", "/users/{user_id}"}
_ADMIN_DEPENDENCIES = [Depends(require_secure_admin_transport)]


def _without_paths(source: APIRouter, paths: set[str]) -> APIRouter:
    filtered = APIRouter()
    filtered.routes.extend(
        route for route in source.routes
        if getattr(route, "path", None) not in paths
    )
    return filtered


def _include_admin(source: APIRouter, *, prefix: str = "/media/admin") -> None:
    router.include_router(
        source,
        prefix=prefix,
        tags=["MediaAdmin"],
        dependencies=_ADMIN_DEPENDENCIES,
    )


router = APIRouter()
router.include_router(brand_public_router, prefix="/media/brand", tags=["Brand"])
router.include_router(media_router, prefix="/media", tags=["MediaCenter"])
router.include_router(karaoke_router, prefix="/karaoke", tags=["Karaoke"])
router.include_router(karaoke_integrity_router, prefix="/karaoke", tags=["KaraokeUsers"])
router.include_router(_without_paths(karaoke_users_router, _KARAOKE_OVERRIDE_PATHS), prefix="/karaoke", tags=["KaraokeUsers"])
_include_admin(brand_upload_router, prefix="/media/admin/upload/brand")
_include_admin(brand_admin_router, prefix="/media/admin/brand")
_include_admin(site_admin_router)
_include_admin(upload_guard_router)
_include_admin(master_mutation_router)
_include_admin(_without_paths(masterlocal_recovery_router, _MASTERLOCAL_OVERRIDE_PATHS))
_include_admin(_without_paths(delete_integrity_router, _DELETE_OVERRIDE_PATHS))
_include_admin(page_integrity_router)
_include_admin(admin_directories_router)
_include_admin(_without_paths(cluster_admin_router, _CLUSTER_OVERRIDE_PATHS))
_include_admin(_without_paths(admin_router, _ADMIN_OVERRIDE_PATHS))
_include_admin(_without_paths(nodes_router, _NODE_OVERRIDE_PATHS))
_include_admin(node_observability_router)
_include_admin(admin_karaoke_integrity_router)
_include_admin(_without_paths(admin_karaoke_users_router, _ADMIN_KARAOKE_OVERRIDE_PATHS))


@router.get("/health")
async def health_check():
    return await readiness_response()
