"""Make Admin media-tree visibility reflect inherited public hiding."""
from __future__ import annotations

from app.api.v1 import admin_cluster_integrity as cluster
from app.services.media_manager import MediaManager


def effective_hidden(path: str, hidden: set[str]) -> bool:
    parts = str(path or "").replace("\\", "/").strip("/").split("/")
    return any("/".join(parts[:index]) in hidden for index in range(1, len(parts) + 1))


def _decorate(items: list[dict], hidden: set[str]) -> None:
    for item in items:
        path = str(item.get("path") or "")
        item["hidden_direct"] = path in hidden
        item["hidden"] = effective_hidden(path, hidden)


def install() -> None:
    if getattr(MediaManager, "_visibility_inheritance_installed", False):
        return

    original_list_tree = MediaManager.list_tree
    original_global_tree = cluster._global_tree

    async def inherited_list_tree(relative_dir: str = "") -> dict:
        result = await original_list_tree(relative_dir)
        hidden = await MediaManager.hidden_paths()
        _decorate(result.get("items") or [], hidden)
        return result

    async def inherited_global_tree(path: str, query: str | None = None) -> dict:
        result = await original_global_tree(path, query)
        hidden = await cluster._hidden_paths()
        _decorate(result.get("items") or [], hidden)
        return result

    MediaManager.list_tree = staticmethod(inherited_list_tree)
    cluster._global_tree = inherited_global_tree
    MediaManager._visibility_inheritance_installed = True
