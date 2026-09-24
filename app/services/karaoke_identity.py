"""Opaque browser handles for standalone or Master-global media identities."""
from __future__ import annotations

import json

from cryptography.fernet import InvalidToken

from app.services.federation.state import state


class InvalidKaraokeIdentity(ValueError):
    pass


def issue(*, media_id: str | None = None, resource_id: str | None = None) -> str:
    if bool(media_id) == bool(resource_id):
        raise ValueError("Exactly one karaoke media identity is required")
    kind, identifier = ("global", resource_id) if resource_id else ("standalone", media_id)
    if not identifier or len(identifier) != 64:
        raise ValueError("Invalid karaoke media identity")
    payload = json.dumps({"v": 1, "kind": kind, "id": identifier}, separators=(",", ":"), sort_keys=True)
    return state.seal_client_identity(payload)


def resolve(token: str) -> tuple[str, str]:
    try:
        payload = json.loads(state.unseal_client_identity(token))
    except (InvalidToken, RuntimeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidKaraokeIdentity("Invalid karaoke media identity") from exc
    kind, identifier = payload.get("kind"), payload.get("id")
    if payload.get("v") != 1 or kind not in {"local", "remote", "standalone", "global"}:
        raise InvalidKaraokeIdentity("Invalid karaoke media identity")
    if not isinstance(identifier, str) or len(identifier) != 64 or any(
        character not in "0123456789abcdef" for character in identifier
    ):
        raise InvalidKaraokeIdentity("Invalid karaoke media identity")
    # Old encrypted handles remain valid during rolling deployment.
    return {"local": "standalone", "remote": "global"}.get(kind, kind), identifier


def attach(items: list[dict]) -> list[dict]:
    for item in items:
        try:
            item["karaoke_id"] = issue(
                resource_id=item.get("resource_id"),
                media_id=None if item.get("resource_id") else item.get("media_id"),
            )
        except (ValueError, RuntimeError):
            # One malformed catalog row must not take down the media browser.
            item["karaoke_id"] = None
    return items
