"""Cache-aware public catalog ordering for media directories."""
from __future__ import annotations

from app.services import media_directories


_CATEGORY_NAMESPACE = "categories-directory-priority-v1"
_SUBCATEGORY_NAMESPACE = "subcategories-directory-priority-v1"


def install_public_priority() -> None:
    """Install folder ordering without turning a catalog cache hit into a MySQL read."""
    from app.api.v1 import media

    if getattr(media, "_directory_priority_installed", False):
        return
    original_categories = media.get_media_categories
    original_subcategories = media.get_media_subcategories

    async def prioritized_categories(media_type, valid_exts, *, include_hidden=False):
        identity = f"{media_type}:{'all' if include_hidden else 'public'}"
        generation, cached = await media.load_media_catalog(_CATEGORY_NAMESPACE, identity)
        if cached is not None:
            return cached

        entries = await original_categories(media_type, valid_exts, include_hidden=include_hidden)
        root = media._typed_media_root(media_type).name
        ordered = await media_directories.sort_directory_entries(
            entries,
            lambda entry: f"{root}/{entry['name']}",
        )
        current_generation, _ = await media.load_media_catalog(_CATEGORY_NAMESPACE, identity)
        if current_generation != generation:
            return await prioritized_categories(
                media_type, valid_exts, include_hidden=include_hidden,
            )
        await media.store_media_catalog(generation, _CATEGORY_NAMESPACE, identity, ordered)
        return ordered

    async def prioritized_subcategories(
        media_type, category_subpath, valid_exts, *, include_hidden=False,
    ):
        identity = (
            f"{media_type}:{category_subpath}:"
            f"{'all' if include_hidden else 'public'}"
        )
        generation, cached = await media.load_media_catalog(_SUBCATEGORY_NAMESPACE, identity)
        if cached is not None:
            return cached

        entries = await original_subcategories(
            media_type, category_subpath, valid_exts, include_hidden=include_hidden,
        )
        ordered = await media_directories.sort_directory_entries(
            entries,
            lambda entry: f"{category_subpath.rstrip('/')}/{entry['name']}",
        )
        current_generation, _ = await media.load_media_catalog(_SUBCATEGORY_NAMESPACE, identity)
        if current_generation != generation:
            return await prioritized_subcategories(
                media_type,
                category_subpath,
                valid_exts,
                include_hidden=include_hidden,
            )
        await media.store_media_catalog(
            generation, _SUBCATEGORY_NAMESPACE, identity, ordered,
        )
        return ordered

    media.get_media_categories = prioritized_categories
    media.get_media_subcategories = prioritized_subcategories
    media._directory_priority_installed = True
