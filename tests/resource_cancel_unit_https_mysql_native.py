"""Guarded actual MySQL/TLS per-parent settlement with peer process recovery."""
import asyncio
import os
from unittest.mock import patch

from tests.resource_cancel_target_https_mysql_native import run


if __name__=='__main__':
    with patch.dict(os.environ,{'FRONTIERCLOUD_SOURCE_RETAIN_NATIVE':'1','FRONTIERCLOUD_UNIT_SETTLE_NATIVE':'1'}):asyncio.run(run())
