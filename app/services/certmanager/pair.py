"""Local operator pairing command; the five-minute window starts after TLS proof."""
import asyncio
import json

from app.core.config import settings
from app.core.db import close_db, init_db
from app.services.federation.state import state
from app.services.federation.protocol import endpoint
from .client import certificate_control


async def run():
    if settings.DEPLOYMENT_MODE != "only_stroge":
        raise RuntimeError("storage pairing command requires a storage appliance")
    status = await certificate_control()
    if not status.get("https_ready") or not status.get("serving_matches"):
        raise RuntimeError("Storage HTTPS is still pending or unverified; no pairing credential was created")
    await init_db()
    try:
        await state.initialize()
        if state.node["role"] != "Follower" or state.node["endpoint"] != endpoint(settings.STORAGE_ENDPOINT):
            raise RuntimeError("Storage identity is not ready")
        print(json.dumps(await state.create_pair("storage-deployment"), ensure_ascii=False))
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(run())
