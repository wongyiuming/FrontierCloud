from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from functools import lru_cache

from starlette.types import Scope

from app.core.config import settings

UNKNOWN_CLIENT_IP = ipaddress.ip_address(0).compressed


@dataclass(frozen=True)
class ClientIdentity:
    ip: str
    peer_ip: str
    from_trusted_proxy: bool
    trusted_proxy_header_missing: bool = False


@lru_cache(maxsize=8)
def _networks(value: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    result = []
    for item in value.split(","):
        item = item.strip()
        if item:
            result.append(ipaddress.ip_network(item, strict=False))
    return tuple(result)


def _canonical_ip(value: str) -> str | None:
    try:
        return ipaddress.ip_address(value.strip()).compressed
    except ValueError:
        return None


def _in_networks(ip: str, configured: str) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in network for network in _networks(configured))


def resolve_client_identity(scope: Scope) -> ClientIdentity:
    peer_raw = scope.get("client")[0] if scope.get("client") else UNKNOWN_CLIENT_IP
    peer_ip = _canonical_ip(str(peer_raw)) or UNKNOWN_CLIENT_IP
    trusted_peer = _in_networks(peer_ip, settings.TRUSTED_PROXY_NETWORKS)

    if not trusted_peer:
        return ClientIdentity(ip=peer_ip, peer_ip=peer_ip, from_trusted_proxy=False)

    values = [
        value.decode("ascii", errors="ignore").strip()
        for name, value in scope.get("headers", [])
        if name.lower() == b"x-real-ip"
    ]
    real_ip = _canonical_ip(values[0]) if len(values) == 1 and "," not in values[0] else None
    if real_ip is None:
        return ClientIdentity(
            ip=peer_ip,
            peer_ip=peer_ip,
            from_trusted_proxy=False,
            trusted_proxy_header_missing=True,
        )
    return ClientIdentity(ip=real_ip, peer_ip=peer_ip, from_trusted_proxy=True)


def client_ip(scope: Scope) -> str:
    return resolve_client_identity(scope).ip


def is_security_exempt(ip: str) -> bool:
    return _in_networks(ip, settings.SECURITY_EXEMPT_NETWORKS)


def normalize_ip(value: str) -> str:
    result = _canonical_ip(value)
    if result is None:
        raise ValueError("Invalid IP address")
    return result
