"""Authenticated Master control for atomic media-directory rename on Followers."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from app.api.internal_nodes import authenticated
from app.services.federation.state import state
from app.services.media_directories import rename_follower_directory


router = APIRouter(prefix="/internal/v1/storage-control", include_in_schema=False)


@router.post("/directory-rename")
async def directory_rename(request: Request):
    relation = await authenticated(request)
    if state.node.get("role") != "Follower" or relation.get("direction") != "upstream":
        raise HTTPException(403, "Only the paired Master can rename Follower media directories")
    try:
        value = json.loads(request.state.node_control_body or b"{}")
        old_path = str(value["old_path"])
        new_path = str(value["new_path"])
        return await rename_follower_directory(old_path, new_path, state.database)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Media directory not found") from exc
    except FileExistsError as exc:
        raise HTTPException(409, "Target media directory already exists") from exc
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, str(exc) or "Invalid directory rename request") from exc
