"""Initialize role-dependent business services after startup and role changes."""
from __future__ import annotations

from app.services import lyrics, media_crypto
from app.services.federation.state import state


async def initialize_business_services() -> None:
    if state.node.get("role") == "Follower":
        return
    await media_crypto.initialize()
    await lyrics.initialize_default_lyrics()
