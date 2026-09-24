"""Recoverable Karaoke account and recording mutation routes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from app.api.v1 import karaoke_users as legacy
from app.services import karaoke_accounts as accounts
from app.services import karaoke_delete_integrity as deletion
from app.services.federation.state import state


router = APIRouter(prefix="/account")


@router.get("/status")
async def status(request: Request):
    if state.node.get("role") == "Master":
        await deletion.recover_deleting_users()
    return await legacy.status(request)


@router.get("/recordings")
async def recordings(request: Request):
    user = await legacy._user(request)
    await deletion.recover_deleting_recordings(user["user_id"])
    return await legacy.recordings(request)


@router.post("/recordings/ticket")
async def create_ticket(request: Request, payload: legacy.TicketPayload):
    user = await legacy._user(request, True)
    await deletion.recover_deleting_recordings(user["user_id"])
    return await legacy.create_ticket(request, payload)


@router.delete("/recordings/{recording_id}/pending")
async def cancel_pending(recording_id: str, request: Request):
    user = await legacy._user(request, True)
    try:
        row = await deletion.stage_recording(user["user_id"], recording_id, pending=True)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if row is None:
        return {"status": "cancelled"}
    removed = await deletion.recover_recording(
        user["user_id"], recording_id, request=request, action="recording-cancel",
    )
    if not removed:
        raise HTTPException(503, "录音取消已进入恢复状态，请稍后刷新")
    return {"status": "cancelled"}


@router.delete("/recordings/{recording_id}")
async def delete_recording(recording_id: str, request: Request):
    user = await legacy._user(request, True)
    try:
        row = await deletion.stage_recording(user["user_id"], recording_id, pending=False)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if row is None:
        return {"status": "deleted"}
    removed = await deletion.recover_recording(
        user["user_id"], recording_id, request=request, action="recording-delete",
    )
    if not removed:
        raise HTTPException(503, "录音删除已进入恢复状态，请稍后刷新")
    return {"status": "deleted"}


@router.delete("")
async def delete_account(request: Request, response: Response):
    user = await legacy._user(request, True)
    staged = await deletion.stage_user_deletion(
        user["user_id"], request=request, action="account-delete",
    )
    accounts.clear_session_cookies(response)
    if not staged:
        return {"status": "deleted"}
    complete = await deletion.recover_user_deletion(
        user["user_id"], request=request, action="account-delete",
    )
    return {"status": "deleted" if complete else "deleting"}
