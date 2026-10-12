"""Run inside the actual storage Web container after verified HTTPS is ready."""
import asyncio
import json
import socket
import ssl
import time

from app.core.config import settings
from app.services.certmanager import certificate_control
from app.services.certmanager.pair import run as create_pair


async def verify():
    status = await certificate_control()
    assert status["state"] == "ACTIVE" and status["https_ready"] and status["serving_matches"], status
    with socket.create_connection(("127.0.0.1", settings.STORAGE_PORT), timeout=3) as raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname=settings.SERVER_NAME) as stream:
            stream.sendall(b"GET /internal/v1/identity?challenge=0123456789abcdef0123456789abcdef HTTP/1.1\r\nHost: node.4399.com\r\nConnection: close\r\n\r\n")
            response = bytearray()
            while chunk := stream.recv(65536): response.extend(chunk)
            # Actual ASGI transport must be HTTPS, not a plaintext app behind a TLS socket.
            assert response.startswith(b"HTTP/1.1 200"), response[:128]
    import contextlib
    import io
    output = io.StringIO()
    started = int(time.time())
    with contextlib.redirect_stdout(output):
        await create_pair()
    package = json.loads(output.getvalue())
    payload = package["payload"]
    from app.services.federation import protocol
    verified = protocol.verify(payload["public_key"], package)
    assert verified["endpoint"] == protocol.endpoint(settings.STORAGE_ENDPOINT)
    assert started+295 <= verified["expires_at"] <= int(time.time())+300
    assert len(verified["token"]) >= 48
    return {"storage_native_https": True, "identity_https_verified": True, "pair_signature_verified": True,
            "pair_lifetime_seconds": verified["expires_at"]-started, "profile": settings.STORAGE_TRANSPORT,
            "storage_port": settings.STORAGE_PORT, "certificate_state": status["state"]}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify())))
