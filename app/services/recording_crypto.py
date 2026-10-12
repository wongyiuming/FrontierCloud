"""Opaque, owner-bound recording lyric snapshots and immutable receipt checks."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from app.services import media_crypto as crypto

MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_ENCRYPTED_LYRIC_PLAINTEXT = 1400 * 1024
TRAILER_MAGIC = b"FRONTIERCLOUD-KARAOKE-V1"


def encrypted_snapshot(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"encryption", "ciphertext"}:
        raise crypto.MetadataError("Invalid encrypted recording lyric snapshot")
    descriptor = crypto.Metadata.from_dict(value["encryption"])
    ciphertext = value["ciphertext"]
    if (descriptor.plaintext_size > MAX_ENCRYPTED_LYRIC_PLAINTEXT or not isinstance(ciphertext, str)
            or len(ciphertext) != 4 * ((descriptor.ciphertext_size + 2) // 3)):
        raise crypto.MetadataError("Encrypted recording lyric snapshot exceeds its bound")
    try:
        if len(crypto._decode64(ciphertext)) != descriptor.ciphertext_size:
            raise ValueError("Ciphertext length mismatch")
    except (ValueError, TypeError) as exc:
        raise crypto.MetadataError("Invalid encrypted recording lyric bytes") from exc
    return {"encryption": descriptor.to_dict(), "ciphertext": ciphertext}


def lyric_array(value) -> list[dict]:
    if not isinstance(value, list) or len(value) > 10000:
        raise crypto.MetadataError("Invalid recording lyrics")
    result = []
    for entry in value:
        if (not isinstance(entry, dict) or set(entry) != {"time", "text"} or type(entry["time"]) not in {int, float}
                or not math.isfinite(entry["time"]) or entry["time"] < 0 or not isinstance(entry["text"], str)
                or len(entry["text"]) > 4000):
            raise crypto.MetadataError("Invalid recording lyric entry")
        result.append({"time": entry["time"], "text": entry["text"]})
    try:
        if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) > MAX_METADATA_BYTES:
            raise crypto.MetadataError("Recording lyric snapshot exceeds its bound")
    except UnicodeError as exc:
        raise crypto.MetadataError("Invalid recording lyric encoding") from exc
    return result


def metadata(value: dict, *, with_version: bool = False) -> dict:
    allowed = {"title", "lyrics", "encrypted_lyrics"} | ({"version"} if with_version else set())
    if not isinstance(value, dict) or not set(value).issubset(allowed):
        raise crypto.MetadataError("Invalid recording metadata")
    if with_version and (type(value.get("version")) is not int or value["version"] != 1):
        raise crypto.MetadataError("Unsupported recording metadata version")
    title = value.get("title", "")
    if not isinstance(title, str) or len(title) > 255:
        raise crypto.MetadataError("Invalid recording title")
    result = {"title": title, "lyrics": lyric_array(value.get("lyrics", []))}
    if value.get("encrypted_lyrics") is not None:
        if result["lyrics"]:
            raise crypto.MetadataError("Encrypted lyric snapshots must not contain plaintext lyrics")
        result["encrypted_lyrics"] = encrypted_snapshot(value["encrypted_lyrics"])
    try:
        if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) > MAX_METADATA_BYTES:
            raise crypto.MetadataError("Recording metadata exceeds its bound")
    except UnicodeError as exc:
        raise crypto.MetadataError("Invalid recording metadata encoding") from exc
    return result


def metadata_sha256(value: dict) -> str:
    from app.services.federation.protocol import canonical
    return hashlib.sha256(canonical(metadata(value))).hexdigest()


def reservation(snapshot: dict, title: str, size: int) -> dict:
    valid = metadata({"title": title, "lyrics": [], "encrypted_lyrics": snapshot})
    footer = json.dumps({"version": 1, **valid}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # Audio container header and complete V1 footer are charged upload bytes.
    if type(size) is not int or len(footer) + len(TRAILER_MAGIC) + 8 + 12 > size:
        raise crypto.MetadataError("Recording reservation is smaller than its encrypted footer")
    return {"expected_encrypted_lyrics": valid["encrypted_lyrics"]["encryption"],
            "encrypted_lyrics_sha256": metadata_sha256(valid)}


def decode_lyrics(value) -> tuple[list[dict], dict | None, dict | None]:
    """The existing JSON column discriminates legacy, pending and ready state."""
    if value is None:
        return [], None, None
    if isinstance(value, list):
        return lyric_array(value), None, None
    if isinstance(value, dict) and set(value) == {"encrypted_lyrics"}:
        return [], encrypted_snapshot(value["encrypted_lyrics"]), None
    if isinstance(value, dict) and set(value) == {"expected_encrypted_lyrics", "encrypted_lyrics_sha256"}:
        descriptor = crypto.Metadata.from_dict(value["expected_encrypted_lyrics"])
        if descriptor.plaintext_size > MAX_ENCRYPTED_LYRIC_PLAINTEXT or not isinstance(value["encrypted_lyrics_sha256"], str) or not crypto._OBJECT_ID.fullmatch(value["encrypted_lyrics_sha256"]):
            raise crypto.MetadataError("Invalid pending encrypted recording metadata")
        return [], None, {"expected_encrypted_lyrics": descriptor.to_dict(), "encrypted_lyrics_sha256": value["encrypted_lyrics_sha256"]}
    raise crypto.MetadataError("Stored recording lyrics are corrupt")


def receipt_matches(row: dict, value: dict | None) -> bool:
    try:
        _lyrics, encrypted, expected = decode_lyrics(row["lyrics"])
        if expected:
            if not value:
                return False
            valid = metadata(value)
            return (valid.get("encrypted_lyrics", {}).get("encryption") == expected["expected_encrypted_lyrics"]
                    and metadata_sha256(valid) == expected["encrypted_lyrics_sha256"])
        if encrypted:
            return bool(value and metadata(value) == {"title": row["title"], "lyrics": [], "encrypted_lyrics": encrypted})
        return not value or metadata(value).get("encrypted_lyrics") is None
    except (crypto.MetadataError, TypeError, ValueError):
        return False


def preparation_binding(user_id: str, browser: str, source: str, source_file_id: str) -> str:
    digest = hashlib.sha256((browser + "\x00" + source + "\x00" + source_file_id).encode("utf-8")).hexdigest()
    return "recording-lyric:" + user_id + ":" + digest


async def source_object(token: str) -> dict:
    from app.services.media_manager import ensure_media_mutations_ready, media_mutation_lock
    async with media_mutation_lock.shared():
        ensure_media_mutations_ready()
        return await _source_object(token)


async def _source_object(token: str) -> dict:
    """Caller holds the shared mutation fence through authorization/commit."""
    from fastapi import HTTPException
    from app.services import karaoke_identity, media_objects
    try:
        kind, identifier = karaoke_identity.resolve(token)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(404, "Karaoke media not found") from exc
    if kind == "global":
        result = await crypto._crypto_object("", identifier, admin=False)
    else:
        row = await media_objects.object_by_id(identifier)
        if row is None:
            raise HTTPException(404, "Karaoke media not found")
        result = await crypto._crypto_object(row["media_path"], admin=False)
        if result["media_id"] != identifier:
            raise HTTPException(404, "Karaoke media identity changed")
    return {**result, "identity_kind": kind, "identifier": identifier}


async def source_lyric(token: str) -> tuple[dict, str]:
    """Original signed source must remain visible even if another track shares its lyric."""
    from app.services.media_manager import ensure_media_mutations_ready, media_mutation_lock
    async with media_mutation_lock.shared():
        ensure_media_mutations_ready()
        return await _source_lyric(token)


async def _source_lyric(token: str) -> tuple[dict, str]:
    from fastapi import HTTPException
    source = await _source_object(token)
    if source["kind"] != "audio":
        raise HTTPException(409, "当前媒体没有加密歌词")
    lyric = await crypto._encrypted_lyric(source["path"], source["resource_id"])
    if lyric is None or lyric["encryption"] is None:
        raise HTTPException(409, "当前媒体没有加密歌词")
    return source, lyric["encryption"].file_id


def token_snapshot(value: dict) -> tuple[crypto.Metadata | None, str]:
    encoded = value.get("encrypted_lyrics_encryption")
    digest = value.get("encrypted_lyrics_sha256", "")
    if encoded is None and not digest:
        return None, ""
    descriptor = crypto.Metadata.from_dict(encoded)
    if descriptor.plaintext_size > MAX_ENCRYPTED_LYRIC_PLAINTEXT or not isinstance(digest, str) or not crypto._OBJECT_ID.fullmatch(digest):
        raise crypto.MetadataError("Invalid recording snapshot capability")
    return descriptor, digest
