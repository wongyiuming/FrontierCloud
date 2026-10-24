"""Actual independent MySQL stores for Master cancellation authority checks."""
import asyncio
import argparse

from tests.resource_cutover_mysql_native import run_cases
from tests.test_resource_cancel_authority import CancellationAuthorityTests
from tests.test_resource_cutover import CutoverTests
from tests.test_resource_cancel_source import CancellationSourceTests


def cases(*,cutover=False):
    methods=[(CancellationAuthorityTests,name) for name in sorted(dir(CancellationAuthorityTests)) if name.startswith('test_')]
    methods.extend((CancellationSourceTests,name) for name in sorted(dir(CancellationSourceTests)) if name.startswith('test_'))
    if cutover:methods.extend((CutoverTests,name) for name in (
        'test_atomic_owner_and_exact_master_quota_keep_live_business_and_crypto_facts',
        'test_recording_parent_rebind_preserves_opaque_footer_and_current_master_business',
        'test_lost_native_reply_and_new_runner_do_not_charge_target_twice',
        'test_final_audit_failure_rolls_back_all_owner_and_quota_updates',
        'test_native_audit_failure_rolls_back_capacity_then_retry_succeeds',
        'test_unrelated_target_reservations_and_usage_are_preserved'))
    return methods


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--with-cutover-regressions',action='store_true')
    args=parser.parse_args()
    asyncio.run(run_cases(cases(cutover=args.with_cutover_regressions),
        scope='master-uncommitted-parent-cancellation-authority'+('-and-cutover-regression' if args.with_cutover_regressions else '')))
