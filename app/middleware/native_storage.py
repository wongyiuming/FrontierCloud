"""Serve authorized storage files when the appliance has no Nginx process.

Only an authenticated application response can request a whitelisted internal
media/recording path. Client headers never select a file. The file is opened
beneath the managed root without symlinks and pinned until the response closes.
Business nodes keep their existing Nginx delivery path.
"""
from __future__ import annotations

import os
import re
import stat
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote

from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings
from app.services.federation.state import state


def _open_beneath(root: Path, parts: list[str]) -> tuple[int, os.stat_result]:
    if not parts or any(not part or part in {".", ".."} or "\\" in part or "\x00" in part for part in parts):
        raise ValueError("Invalid storage path")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory);directory=child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("A regular storage file is required")
            return descriptor, info
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        os.close(directory)


def _open_internal(value: str) -> tuple[int, os.stat_result]:
    if len(value) > 8192 or "?" in value or "#" in value:
        raise ValueError("Invalid internal storage redirect")
    decoded = unquote(value, errors="strict")
    if decoded.startswith("/_protected_media/"):
        from app.services.media_crypto import managed_path
        from app.services.media_manager import MEDIA_ROOT
        relative = decoded.removeprefix("/_protected_media/")
        managed_path(relative)
        if relative.split("/",1)[0] not in {"music","vido"}:
            raise ValueError("Invalid storage media type")
        return _open_beneath(MEDIA_ROOT,relative.split("/"))
    if decoded.startswith("/_protected_recordings/"):
        from app.services.karaoke_storage import ROOT
        relative=decoded.removeprefix("/_protected_recordings/")
        if not re.fullmatch(r"[a-f0-9]{32}/[a-f0-9]{32}/[a-f0-9]{32}\.bin",relative):
            raise ValueError("Invalid storage recording identity")
        return _open_beneath(ROOT,relative.split("/"))
    raise ValueError("Only managed local storage redirects are permitted")


class NativeStorageDeliveryMiddleware:
    def __init__(self,app: ASGIApp):
        self.app=app

    async def __call__(self,scope: Scope,receive: Receive,send: Send):
        if (scope["type"]!="http" or settings.DEPLOYMENT_MODE!="only_stroge" or state.node.get("role")!="Follower"
                or scope.get("method") not in {"GET","HEAD"}):
            await self.app(scope,receive,send);return
        captured=None
        body_size=0
        async def intercept(message):
            nonlocal captured,body_size
            if message["type"]=="http.response.start" and any(name.lower()==b"x-accel-redirect" for name,_ in message.get("headers",[])):
                captured=message;return
            if captured is not None and message["type"]=="http.response.body":
                body_size+=len(message.get("body",b""));return
            await send(message)
        await self.app(scope,receive,intercept)
        if captured is None:return
        redirects=[value for name,value in captured.get("headers",[]) if name.lower()==b"x-accel-redirect"]
        if captured.get("status")!=200 or body_size or len(redirects)!=1:
            await Response(status_code=502,headers={"Cache-Control":"no-store"})(scope,receive,send);return
        try:
            descriptor,info=_open_internal(redirects[0].decode("ascii"))
        except (OSError,ValueError,UnicodeError):
            await Response(status_code=404,headers={"Cache-Control":"no-store"})(scope,receive,send);return
        try:
            retained=[(name,value) for name,value in captured.get("headers",[]) if name.lower() not in {
                b"x-accel-redirect",b"content-length",b"content-range",b"etag",b"last-modified",b"accept-ranges"}]
            content_type=next((value.decode("latin-1") for name,value in retained if name.lower()==b"content-type"),"application/octet-stream")
            # /proc is an internally created reference to this already pinned
            # inode, never a caller-supplied path. FileResponse owns standard
            # single/multipart Range and If-Range semantics without reopening a
            # mutable managed pathname after authorization.
            response=FileResponse(f"/proc/self/fd/{descriptor}",stat_result=info,media_type=content_type)
            replaced={name.lower() for name,_ in retained}
            response.raw_headers=[(name,value) for name,value in response.raw_headers if name.lower() not in replaced]+retained
            request=Request(scope)
            none_match=request.headers.get("if-none-match")
            not_modified=False
            if none_match:
                etag=response.headers["etag"]
                not_modified=any(value.strip()=="*" or value.strip().removeprefix("W/")==etag for value in none_match.split(","))
            elif request.headers.get("if-modified-since"):
                try:
                    not_modified=int(info.st_mtime)<=parsedate_to_datetime(request.headers["if-modified-since"]).timestamp()
                except (ValueError,TypeError,OverflowError):pass
            if not_modified:
                response=Response(status_code=304,headers={key:value for key,value in response.headers.items()
                    if key.lower() not in {"content-length","content-type","content-range"}})
            delivery_scope={**scope,"extensions":{key:value for key,value in scope.get("extensions",{}).items() if key!="http.response.pathsend"}}
            await response(delivery_scope,receive,send)
        finally:
            os.close(descriptor)
