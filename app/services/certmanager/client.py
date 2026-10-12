"""Read-only bounded Unix RPC; Web has no issuer account or private-cache mount."""
from __future__ import annotations

import httpx

from .assets import RUNTIME


async def certificate_control(*, action="status", browser_public_key="", browser_nonce="") -> dict:
    if action not in ("status", "download"):
        raise ValueError("certificate lifecycle is automatic; only status/download are available")
    request = {"action": action, "browser_public_key": browser_public_key, "browser_nonce": browser_nonce}
    transport = httpx.AsyncHTTPTransport(uds=str(RUNTIME / "control.sock"), retries=0)
    async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=4) as client:
        async with client.stream("POST", "http://certificate/control", json=request) as response:
            if response.status_code != 200:
                raise ValueError(f"certificate controller HTTP {response.status_code}")
            result = bytearray()
            async for chunk in response.aiter_bytes():
                result.extend(chunk)
                if len(result) > 131072:
                    raise ValueError("certificate response too large")
    import json
    value = json.loads(result)
    if not isinstance(value, dict):
        raise ValueError("invalid certificate controller response")
    return value
