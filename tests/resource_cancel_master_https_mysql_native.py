"""Guarded actual MySQL/TLS coordinator and independent peer recovery profile."""
import asyncio
import os
from unittest.mock import patch

from tests.resource_cancel_target_https_mysql_native import run


if __name__=='__main__':
    with patch.dict(os.environ,{'FRONTIERCLOUD_CANCEL_MASTER_NATIVE':'1'}):asyncio.run(run())
