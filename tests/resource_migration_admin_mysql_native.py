"""Actual three-node MySQL Admin API inputs; virtual time and service transport."""
import asyncio
import unittest

from tests.resource_cutover_mysql_native import run_cases
from tests import test_resource_migration_admin as admin


if __name__=='__main__':
    methods=[(admin.MigrationAdminTests,name) for name in unittest.defaultTestLoader.getTestCaseNames(admin.MigrationAdminTests)]
    asyncio.run(run_cases(methods,scope='admin-api-durable-start-confirmation-control-restart'))
