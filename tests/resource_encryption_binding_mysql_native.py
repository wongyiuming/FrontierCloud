"""Actual three-store MySQL legacy ciphertext bindings; grace is virtual."""
import asyncio

from tests.resource_cutover_mysql_native import run_cases
from tests.test_resource_migration_planning import MigrationPlanningTests
from tests.test_resource_scheduler import SchedulerTests
from tests.test_resource_cancel_partial import PartialCancellationTests
from tests.test_resource_source_cleanup import SourceCleanupTests
from tests.test_resource_transfer import ResourceTransferTests


def cases():
    methods=[(MigrationPlanningTests,name) for name in (
        'test_master_only_descriptor_binds_source_ciphertext_without_backfill_or_decryption',
        'test_master_only_descriptor_requires_original_ciphertext_checksum',
        'test_master_only_descriptor_refuses_same_length_changed_ciphertext',
        'test_different_nonempty_source_descriptor_is_never_legacy_compatibility')]
    return methods+[
        (SchedulerTests,'test_master_only_descriptor_survives_publication_cleanup_and_no_source_backfill'),
        (PartialCancellationTests,'test_master_only_descriptors_preserve_retained_source_census_and_readonly_history'),
        (SourceCleanupTests,'test_new_runner_refreshes_authentic_old_target_proof_but_refuses_tampering'),
        (ResourceTransferTests,'test_previous_plan_shape_resumes_existing_partial_ciphertext_with_identical_grants')]


if __name__=='__main__':
    asyncio.run(run_cases(cases(),scope='master-only-ciphertext-binding-cutover-cancel-virtual-grace',finalization=True))
