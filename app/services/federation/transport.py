"""TLS-verified control client. Never used for media bytes."""
from __future__ import annotations

import json
import ssl
import uuid

import httpx

from . import protocol as p


class Transport:
    def __init__(self):
        self.client: httpx.AsyncClient | None = None

    def open(self):
        if self.client is None:
            self.client = httpx.AsyncClient(verify=ssl.create_default_context(), trust_env=False,
                follow_redirects=False, timeout=httpx.Timeout(10, connect=8),
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
                headers={"Accept-Encoding": "identity", "Accept": "application/json"})

    async def close(self):
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def request(self, origin: str, path: str, *, method="GET", value=None, relation=None, credential=None):
        origin = p.endpoint(origin)
        if not path.startswith("/internal/v1/"):
            raise p.ProtocolError("Only versioned control endpoints are permitted")
        body = p.canonical(value) if value is not None else b""
        if len(body) > p.MAX_CONTROL_BYTES:
            raise p.ProtocolError("Control message too large")
        headers = {"Content-Type": "application/json"}
        if relation:
            headers.update(p.auth_headers(credential, relation, method, path, body))
        self.open()
        async with self.client.stream(method, origin + path, content=body, headers=headers) as response:
            if response.status_code != 200:
                # Do not log pairing tokens, credentials, or arbitrary upstream bodies.
                raise p.ProtocolError(f"Node control HTTP {response.status_code}")
            chunks, length = [], 0
            async for chunk in response.aiter_bytes():
                length += len(chunk)
                if length > p.MAX_CONTROL_BYTES:
                    raise p.ProtocolError("Node control response too large")
                chunks.append(chunk)
            try:
                result = json.loads(b"".join(chunks))
                if not isinstance(result, dict):
                    raise ValueError()
                return result
            except ValueError as exc:
                raise p.ProtocolError("Invalid node control response") from exc

    async def identity(self, origin: str, *, expected_id=None, expected_key=None, role=None):
        challenge = uuid.uuid4().hex
        envelope = await self.request(origin, "/internal/v1/identity?challenge=" + challenge)
        try:
            public = envelope["payload"]["public_key"]
            identity = p.verify(expected_key or public, envelope)
            if (identity["challenge"] != challenge or identity["protocol"] != p.PROTOCOL_VERSION
                    or not p.IDENTIFIER.fullmatch(identity["node_id"])
                    or (expected_id and identity["node_id"] != expected_id)
                    or (expected_key and public != expected_key)
                    or (role and identity["role"] != role)):
                raise ValueError()
            if identity["role"] != "Standalone" and p.endpoint(identity["endpoint"]) != p.endpoint(origin):
                raise ValueError()
            return identity
        except (KeyError, ValueError) as exc:
            raise p.ProtocolError("Node identity, endpoint or protocol mismatch") from exc


transport = Transport()
