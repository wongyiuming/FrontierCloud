from __future__ import annotations

import asyncio

from app.core.logging_config import configure_logging
from app.services.admin_service import issue_admin_token


async def main() -> None:
    token = await issue_admin_token(announce=False)
    print(token)


if __name__ == "__main__":
    configure_logging()
    asyncio.run(main())
