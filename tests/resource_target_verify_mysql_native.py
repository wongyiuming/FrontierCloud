"""Actual MySQL Target proof checks after fully committed native accounting."""
import asyncio

from tests.resource_cutover_mysql_native import run_cases
from tests.test_resource_target_verify import TargetVerificationTests


def cases():
    return [(TargetVerificationTests,name) for name in (
        'test_fresh_plain_and_cipher_proof_preserves_bytes_keys_ownership_and_quota',
        'test_in_place_rewrite_even_with_restored_mtime_cannot_get_a_fresh_proof',
        'test_registry_and_encryption_changes_are_not_repaired_or_ignored',
        'test_unknown_sibling_is_preserved_and_blocks_the_proof',
        'test_replacement_transfer_service_restarts_scan_from_durable_committed_state',
        'test_native_audit_failure_retains_previous_state_and_retry_reobserves',
        'test_expired_reply_boolean_progress_and_wrong_signer_are_refused',
        'test_unacknowledged_native_commit_is_not_enough_for_target_verification',
        'test_lost_master_audit_after_native_success_requires_fresh_observation_on_retry',
        'test_recording_live_proof_keeps_target_relationship_namespace_and_opaque_footer')]


if __name__=='__main__':asyncio.run(run_cases(cases(),scope='post-commit-target-verification'))
