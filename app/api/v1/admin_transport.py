from __future__ import annotations

from fastapi import HTTPException, Request

from app.core.client_ip import resolve_client_identity


def secure_admin_transport(request: Request) -> bool:
    identity = resolve_client_identity(request.scope)
    if request.url.scheme.lower() == "https":
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "").strip().lower()
    if identity.from_trusted_proxy:
        return forwarded_proto == "https"
    # The recovery exception is for direct loopback, not a local reverse proxy.
    return (identity.peer_ip in {"127.0.0.1", "::1"}
            and not identity.trusted_proxy_header_missing
            and not any(name in request.headers for name in
                        ("forwarded", "x-forwarded-for", "x-forwarded-proto", "x-real-ip")))


async def require_secure_admin_transport(request: Request) -> None:
    if not secure_admin_transport(request):
        raise HTTPException(426, "安全控制台只允许通过 HTTPS 访问；仅本机回环 HTTP 例外")
