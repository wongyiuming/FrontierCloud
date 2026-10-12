"""The public network contract, shared by the controller and Web runner."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

RETIRED = ("SSL_CERT_PATH", "SSL_KEY_PATH", "STORAGE_TLS_CERT", "STORAGE_TLS_KEY", "ACME_WEBROOT")


def hostname(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 253 or "." not in value or not re.fullmatch(r"[a-z0-9.-]+", value):
        raise ValueError("automatic TLS requires one public DNS hostname in SERVER_NAME")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError("SERVER_NAME must be a DNS hostname, not an IP address")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in value.split(".")):
        raise ValueError("invalid SERVER_NAME DNS label")
    return value


def validate_environment(environment) -> None:
    for name in RETIRED:
        if environment.get(name) is not None and str(environment[name]).strip():
            raise ValueError(f"{name} is retired; migrate verified existing material with the offline migration command, then remove this setting")
    for name in ("HTTP_PORT", "HTTPS_PORT"):
        if environment.get(name) is not None and str(environment[name]).strip():
            raise ValueError(f"{name} is retired; public HTTP/HTTPS ports are fixed at 80/443")


def validate_settings(settings, environment) -> None:
    validate_environment(environment)
    if settings.TLS_ENABLED:
        hostname(settings.SERVER_NAME)
    if settings.DEPLOYMENT_MODE not in ("", "only_stroge"):
        raise ValueError("DEPLOYMENT_MODE must be empty or only_stroge")
    if settings.DEPLOYMENT_MODE != "only_stroge":
        return
    if not settings.TLS_ENABLED:
        raise ValueError("storage appliances require certificate-verified TLS")
    if settings.STORAGE_TRANSPORT not in ("direct", "relay"):
        raise ValueError("STORAGE_TRANSPORT must be direct or relay")
    port = settings.STORAGE_PORT
    if settings.STORAGE_TRANSPORT == "direct" and port != 443:
        raise ValueError("Direct storage HTTPS must use public port 443")
    if settings.STORAGE_TRANSPORT == "relay" and not 1024 <= port <= 65535:
        raise ValueError("Relay uses a restricted high port; public HTTP/HTTPS ports 80/443 are reserved for Direct")
    parsed = urlsplit(settings.STORAGE_ENDPOINT)
    try:
        endpoint_port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("invalid STORAGE_ENDPOINT port") from exc
    if (parsed.scheme != "https" or parsed.hostname != hostname(settings.SERVER_NAME)
            or endpoint_port != port or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("STORAGE_ENDPOINT must be a verified HTTPS root URL matching SERVER_NAME and the storage port")


def profile(settings) -> str:
    return settings.STORAGE_TRANSPORT if settings.DEPLOYMENT_MODE == "only_stroge" else "business"
