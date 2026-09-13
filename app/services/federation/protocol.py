"""Versioned, bounded control messages and short resource capabilities."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption

PROTOCOL_VERSION = 1
APP_VERSION = "1.2.0rc0"
HEARTBEAT_SECONDS = 30
OFFLINE_SECONDS = 120
PAIR_SECONDS = 300
TOKEN_SECONDS = 300
PAGE_SIZE = 100
MAX_CONTROL_BYTES = 512 * 1024
MAX_LYRIC_RESPONSE_BYTES = 5 * 1024 * 1024
IDENTIFIER = re.compile(r"^[a-f0-9]{32}$")
OBJECT_ID = re.compile(r"^[a-f0-9]{64}$")


class ProtocolError(ValueError):
    pass


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", value):
        raise ProtocolError("Invalid encoding")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ProtocolError("节点通信需要可验证证书的 HTTPS 根地址")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if not re.fullmatch(r"[a-z0-9.-]+", host) or host in ("localhost", "metadata.google.internal"):
        raise ProtocolError("节点地址必须是有效的 HTTPS 主机名或 IPv4 地址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified):
        raise ProtocolError("不能使用回环、链路本地或元数据地址")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProtocolError("Invalid endpoint port") from exc
    return "https://" + host + (f":{port}" if port and port != 443 else "")


def resource_id(owner: str, original: str) -> str:
    if not IDENTIFIER.fullmatch(owner) or not OBJECT_ID.fullmatch(original):
        raise ProtocolError("Invalid media identity")
    return hashlib.sha256(f"{owner}:{original}".encode()).hexdigest()


def new_key() -> str:
    return encode(Ed25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))


def public_key(private: str) -> str:
    return encode(Ed25519PrivateKey.from_private_bytes(decode(private)).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def sign(private: str, payload: dict) -> dict:
    return {"payload": payload, "signature": encode(Ed25519PrivateKey.from_private_bytes(decode(private)).sign(canonical(payload)))}


def verify(public: str, envelope: dict) -> dict:
    try:
        payload = envelope["payload"]
        Ed25519PublicKey.from_public_bytes(decode(public)).verify(decode(envelope["signature"]), canonical(payload))
        if not isinstance(payload, dict):
            raise ValueError()
        return payload
    except Exception as exc:
        raise ProtocolError("Node identity signature mismatch") from exc


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def auth_headers(credential: str, relationship: str, method: str, path: str, body: bytes = b"") -> dict:
    stamp, nonce = str(int(time.time())), secrets.token_hex(16)
    message = "\n".join((relationship, stamp, nonce, method.upper(), path, hashlib.sha256(body).hexdigest()))
    return {"X-Node-Relationship": relationship, "X-Node-Time": stamp, "X-Node-Nonce": nonce,
            "X-Node-Signature": hmac.new(decode(credential), message.encode(), hashlib.sha256).hexdigest()}


def verify_auth(credential: str, headers, method: str, path: str, body: bytes, now: int) -> str:
    try:
        relationship, stamp, nonce = (headers[k] for k in ("x-node-relationship", "x-node-time", "x-node-nonce"))
        if not IDENTIFIER.fullmatch(relationship) or not IDENTIFIER.fullmatch(nonce) or abs(now - int(stamp)) > 60:
            raise ValueError()
        message = "\n".join((relationship, stamp, nonce, method.upper(), path, hashlib.sha256(body).hexdigest()))
        expected = hmac.new(decode(credential), message.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, headers["x-node-signature"]):
            raise ValueError()
        return nonce
    except (ValueError, KeyError, TypeError) as exc:
        raise ProtocolError("Invalid or expired relationship authentication") from exc


def media_token(credential: str, relationship: str, master: str, owner: str, original: str, now: int) -> str:
    payload = encode(canonical({"r": relationship, "m": master, "o": owner, "i": original,
                                "e": now + TOKEN_SECONDS, "v": PROTOCOL_VERSION}))
    return payload + "." + encode(hmac.new(decode(credential), payload.encode(), hashlib.sha256).digest())


def verify_media_token(credential: str, token: str, now: int) -> dict:
    try:
        payload, signature = token.split(".")
        expected = hmac.new(decode(credential), payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, decode(signature)):
            raise ValueError()
        value = json.loads(decode(payload))
        if value["v"] != PROTOCOL_VERSION or value["e"] <= now or value["e"] > now + TOKEN_SECONDS + 60:
            raise ValueError()
        resource_id(value["o"], value["i"])
        if not IDENTIFIER.fullmatch(value["r"]) or not IDENTIFIER.fullmatch(value["m"]):
            raise ValueError()
        return value
    except Exception as exc:
        raise ProtocolError("Invalid or expired media capability") from exc
