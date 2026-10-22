"""Keep native control fixtures alive through an authenticated traffic pause."""
import asyncio

from app.services.resource_transfer_protocol import TransferError


async def low_traffic_step(step, keepalive, *, timeout=90):
    """Retry only the peer's verified traffic gate, within the fixture budget."""
    loop = asyncio.get_running_loop()
    renew_after = 0
    async with asyncio.timeout(timeout):
        while True:
            try:
                return await step()
            except TransferError as error:
                if str(error) != 'migration_transfer_waiting_low_traffic':
                    raise
            if loop.time() >= renew_after:
                await keepalive()
                renew_after = loop.time() + 20
            await asyncio.sleep(1)
