"""Actual three-node MySQL scheduling, native bytes and virtual cleanup grace."""
import asyncio

from tests.resource_cutover_mysql_native import run_cases
from tests.test_resource_scheduler import SchedulerTests


def cases():
    return [(SchedulerTests,name) for name in sorted(dir(SchedulerTests)) if name.startswith('test_')]


if __name__=='__main__':
    asyncio.run(run_cases(cases(),scope='serial-scheduler-restart-network-flow-control-virtual-grace',finalization=True))
