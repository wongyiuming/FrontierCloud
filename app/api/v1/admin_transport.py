from __future__ import annotations

from fastapi import HTTPException, Request

from app.core.client_ip import resolve_client_identity


def secure_admin_transport(request: Request) -> bool:
    identity = resolve_client_identity(request.scope)
    if request.url.scheme.lower() == "https":
        return True
    if identity.peer_ip in {"127.0.0.1", "::1"}:
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "").strip().lower()
    return identity.from_trusted_proxy and forwarded_proto == "https"


async def require_secure_admin_transport(request: Request) -> None:
    if not secure_admin_transport(request):
        raise HTTPException(426, "安全控制台只允许通过 HTTPS 访问；仅本机回环 HTTP 例外")
