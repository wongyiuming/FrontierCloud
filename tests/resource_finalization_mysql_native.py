"""Actual separate MySQL stores, native census and virtual grace boundaries."""
import asyncio

from tests.resource_cutover_mysql_native import run_cases
from tests.test_resource_finalization import FinalizationTests


def cases():
    return [(FinalizationTests,name) for name in sorted(dir(FinalizationTests)) if name.startswith('test_')]


if __name__=='__main__':asyncio.run(run_cases(cases(),scope='finalization-virtual-clock-faults',finalization=True))
